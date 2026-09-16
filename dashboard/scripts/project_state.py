"""Portable INI state for a project and its NAMD simulations."""
from __future__ import annotations

import configparser
from datetime import datetime, timezone
from pathlib import Path
import re


SCHEMA_VERSION = "1"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _stage_key(name: str) -> tuple[int, int]:
    match = re.match(r"step(\d+)\.(\d+)", name)
    return (int(match.group(1)), int(match.group(2))) if match else (999, 999)


def _csv(values: list[str]) -> str:
    return ",".join(values)


def _split(value: str) -> list[str]:
    return [item for item in value.split(",") if item]


def _output_status(path: Path) -> str:
    """Read only the output tail; NAMD writes completion/errors at the end."""
    try:
        with path.open("rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            handle.seek(max(0, size - 65_536))
            tail = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return "pending"
    if "FATAL ERROR:" in tail or "\nERROR:" in tail or tail.startswith("ERROR:"):
        return "error"
    return "done" if "End of program" in tail else "running"


def config_path_for_namd(namd_dir: Path) -> Path | None:
    """Find the managed project's config.ini from a simulation namd folder."""
    resolved = namd_dir.resolve()
    for parent in resolved.parents:
        candidate = parent / "config.ini"
        if candidate.is_file():
            return candidate
        if parent.name == "simulations":
            relative = resolved.relative_to(parent).parts
            if len(relative) < 2 or relative[1] != "work":
                return None
            candidate = parent.parent / "config.ini"
            return candidate if candidate.parent.is_dir() else None
    return None


def simulation_id_for_namd(namd_dir: Path) -> str | None:
    parts = namd_dir.resolve().parts
    try:
        index = max(i for i, part in enumerate(parts) if part == "simulations")
        if parts[index + 2] != "work":
            return None
        return parts[index + 1]
    except (ValueError, IndexError):
        return None


def read_config(path: Path) -> configparser.ConfigParser:
    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str
    if path.is_file():
        parser.read(path, encoding="utf-8")
    return parser


def write_config(path: Path, parser: configparser.ConfigParser) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".ini.tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        parser.write(handle)
    temporary.replace(path)


def refresh_simulation_state(
    namd_dir: Path,
    stages: list[dict] | None = None,
    *,
    run_status: str | None = None,
    active: bool = False,
) -> dict | None:
    """Reconcile one simulation's INI state with its inputs and parsed outputs."""
    config_path = config_path_for_namd(namd_dir)
    simulation_id = simulation_id_for_namd(namd_dir)
    planned = sorted(
        {path.stem for path in namd_dir.glob("step6*.inp")}, key=_stage_key
    )
    if stages is None:
        parsed = {path.stem: _output_status(path) for path in namd_dir.glob("step6*.out")}
    else:
        parsed = {str(stage.get("stage")): str(stage.get("status", "pending"))
                  for stage in stages if stage.get("stage")}
    statuses = {stage: parsed.get(stage, "pending") for stage in planned}
    # Preserve dynamically generated/output-only stages.
    for stage, status in parsed.items():
        statuses.setdefault(stage, status)
    ordered = sorted(statuses, key=_stage_key)
    completed = [stage for stage in ordered if statuses[stage] == "done"]
    failed = [stage for stage in ordered if statuses[stage] == "error"]
    running = [stage for stage in ordered if statuses[stage] == "running"]
    pending = [stage for stage in ordered if statuses[stage] not in {"done", "error", "running"}]
    current = (running or failed or pending or completed[-1:])
    inferred_status = (
        "error" if failed else
        "running" if running else
        "completed" if ordered and len(completed) == len(ordered) else
        "pending"
    )
    status = run_status or inferred_status
    updated = _now()
    state = {
        "status": status,
        "currentStep": current[0] if current else None,
        "completedSteps": completed,
        "pendingSteps": pending,
        "failedSteps": failed,
        "progressPercent": round(100 * len(completed) / len(ordered)) if ordered else 0,
        "updatedAt": updated,
    }
    # Legacy simulations have no physical project/config.ini. They still get a
    # runtime state in the dashboard and use the same resumable execution path.
    if config_path is None or simulation_id is None:
        return state
    parser = read_config(config_path)
    section = f"simulation:{simulation_id}"
    if not parser.has_section(section):
        parser.add_section(section)
    parser[section].update({
        "run_status": status,
        "current_step": current[0] if current else "",
        "completed_steps": _csv(completed),
        "pending_steps": _csv(pending),
        "failed_steps": _csv(failed),
        "progress_percent": str(round(100 * len(completed) / len(ordered))) if ordered else "0",
        "updated_at": updated,
    })
    for stage in ordered:
        stage_section = f"simulation:{simulation_id}:stage:{stage}"
        if not parser.has_section(stage_section):
            parser.add_section(stage_section)
        parser[stage_section].update({"status": statuses[stage], "updated_at": updated})
    if not parser.has_section("dashboard"):
        parser.add_section("dashboard")
    if active:
        parser["dashboard"]["active_simulation_id"] = simulation_id
    parser["dashboard"]["updated_at"] = updated
    write_config(config_path, parser)
    return state


def state_from_config(project_directory: str | None, simulation_id: str) -> dict | None:
    if not project_directory:
        return None
    parser = read_config(Path(project_directory) / "config.ini")
    section = f"simulation:{simulation_id}"
    if not parser.has_section(section):
        return None
    values = parser[section]
    try:
        progress = int(values.get("progress_percent", "0"))
    except ValueError:
        progress = 0
    return {
        "status": values.get("run_status", "pending"),
        "currentStep": values.get("current_step") or None,
        "completedSteps": _split(values.get("completed_steps", "")),
        "pendingSteps": _split(values.get("pending_steps", "")),
        "failedSteps": _split(values.get("failed_steps", "")),
        "progressPercent": progress,
        "updatedAt": values.get("updated_at") or None,
    }


def successful_stages(namd_dir: Path) -> list[str]:
    """Return stages whose current output contains NAMD's success marker."""
    return sorted(
        [path.stem for path in namd_dir.glob("step6*.out") if _output_status(path) == "done"],
        key=_stage_key,
    )
