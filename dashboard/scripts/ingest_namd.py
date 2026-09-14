#!/usr/bin/env python3
"""Convierte salidas de NAMD en JSON compacto para el dashboard."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import struct
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

try:
    from .equilibration_agent import ensure_continuation_stage, evaluate_equilibration
except ImportError:
    from equilibration_agent import ensure_continuation_stage, evaluate_equilibration


ENERGY_FIELDS = {
    "TS": "step",
    "POTENTIAL": "energy",
    "ELECT": "electrostatic",
    "TEMP": "temperature",
    "PRESSURE": "pressure",
    "VOLUME": "volume",
}
DEFAULT_ENERGY_TITLE = [
    "TS", "BOND", "ANGLE", "DIHED", "IMPRP", "ELECT", "VDW", "BOUNDARY",
    "MISC", "KINETIC", "TOTAL", "TEMP", "POTENTIAL", "TOTALAVG", "TEMPAVG",
    "PRESSURE", "GPRESSURE", "VOLUME", "PRESSAVG", "GPRESSAVG",
]


def _new_output_state() -> dict:
    return {"title": DEFAULT_ENERGY_TITLE.copy(), "rows": [], "errors": [], "timestep_fs": 1.0, "seed": None, "finished": False}


def _consume_output_line(state: dict, raw: str) -> None:
    line = raw.strip()
    if line.startswith("ETITLE:"):
        state["title"] = line.split()[1:]
    elif line.startswith("ENERGY:"):
        values = line.split()[1:]
        if len(values) >= len(state["title"]):
            parsed = dict(zip(state["title"], values))
            row = {}
            try:
                for source, target in ENERGY_FIELDS.items():
                    if source in parsed:
                        row[target] = int(float(parsed[source])) if target == "step" else float(parsed[source])
            except ValueError:
                return
            if "step" in row:
                row["time"] = row["step"] * state["timestep_fs"] / 1000.0
                if not state["rows"] or state["rows"][-1]["step"] != row["step"]:
                    state["rows"].append(row)
    elif line.startswith("Info: TIMESTEP"):
        try:
            state["timestep_fs"] = float(line.split()[-1])
        except ValueError:
            pass
    elif "NAMD random seed:" in line:
        match = re.search(r"NAMD random seed:\s*(\d+)", line)
        if match:
            state["seed"] = int(match.group(1))
    elif line.startswith(("ERROR:", "FATAL ERROR:")):
        if line not in state["errors"]:
            state["errors"].append(line)
    elif "End of program" in line:
        state["finished"] = True


def _output_payload(path: Path, state: dict) -> dict:
    seed = state["seed"]
    if seed is None:
        inp = path.with_suffix(".inp")
        if inp.exists():
            match = re.search(r"(?mi)^\s*seed\s+(\d+)\s*;?", inp.read_text(encoding="utf-8", errors="replace"))
            if match:
                seed = int(match.group(1))
    rows = state["rows"]
    if len(rows) > 200:
        stride = max(1, (len(rows) + 198) // 199)
        rows = rows[::stride]
        if rows[-1] != state["rows"][-1]:
            rows.append(state["rows"][-1])
    return {
        "file": path.name, "stage": path.stem,
        "status": "error" if state["errors"] else ("done" if state["finished"] else "running"),
        "timestepFs": state["timestep_fs"], "seed": seed,
        "errors": state["errors"], "series": rows,
    }


def parse_output(path: Path) -> dict:
    state = _new_output_state()
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for raw in handle:
            _consume_output_line(state, raw)
    return _output_payload(path, state)


class IncrementalOutputCache:
    """Procesa únicamente los bytes añadidos a cada salida NAMD."""

    def __init__(self) -> None:
        self.files: dict[str, dict] = {}

    def parse(self, path: Path) -> dict:
        stat = path.stat()
        key = str(path.resolve())
        identity = (stat.st_dev, stat.st_ino)
        cached = self.files.get(key)
        if cached is None or cached["identity"] != identity or stat.st_size < cached["offset"]:
            cached = {"identity": identity, "offset": 0, "pending": b"", "state": _new_output_state()}
            self.files[key] = cached
        with path.open("rb") as handle:
            handle.seek(cached["offset"])
            chunk = cached["pending"] + handle.read()
            cached["offset"] = handle.tell()
        lines = chunk.split(b"\n")
        cached["pending"] = lines.pop() if lines else b""
        for raw in lines:
            _consume_output_line(cached["state"], raw.decode("utf-8", errors="replace"))
        return _output_payload(path, cached["state"])


def find_default_namd(root: Path) -> Path:
    candidates = []
    for out in root.rglob("step6*.out"):
        if "node_modules" not in out.parts:
            candidates.append(out.parent)
    if not candidates:
        raise SystemExit("No se encontraron salidas step6*.out")
    return max(set(candidates), key=lambda p: max(f.stat().st_mtime for f in p.glob("step6*.out")))


def _stage_number_for_sort(stage: str) -> tuple[int, int]:
    match = re.match(r"step(\d+)\.(\d+)", stage)
    return (int(match.group(1)), int(match.group(2))) if match else (-1, -1)


def read_previous_run(namd_dir: Path) -> list[dict]:
    history_root = namd_dir / "run_history"
    if not history_root.exists():
        return []
    for run_dir in sorted((path for path in history_root.iterdir() if path.is_dir()), reverse=True):
        stages = [parse_output(path) for path in sorted(run_dir.glob("step6*.out"))]
        if any(stage["series"] for stage in stages):
            return stages
    return []


def read_reference(pdb: Path) -> tuple[np.ndarray, np.ndarray, list[dict]]:
    coords, ca_indices, residues = [], [], []
    with pdb.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if not line.startswith(("ATOM  ", "HETATM")):
                continue
            try:
                xyz = [float(line[30:38]), float(line[38:46]), float(line[46:54])]
            except ValueError:
                continue
            index = len(coords)
            coords.append(xyz)
            if line.startswith("ATOM  ") and line[12:16].strip() == "CA":
                ca_indices.append(index)
                residues.append({"residue": line[22:26].strip(), "name": line[17:21].strip(), "segment": line[72:76].strip()})
    if not coords or not ca_indices:
        raise ValueError(f"No se encontraron coordenadas/Cα en {pdb.name}")
    return np.asarray(coords, dtype=np.float64), np.asarray(ca_indices, dtype=np.int64), residues


def read_namd_coor(path: Path, expected_atoms: int) -> np.ndarray:
    raw = path.read_bytes()
    if len(raw) < 4:
        raise ValueError("archivo incompleto")
    atoms = struct.unpack("<i", raw[:4])[0]
    if atoms != expected_atoms or len(raw) != 4 + atoms * 3 * 8:
        raise ValueError(f"coordenadas incompletas: {atoms}/{expected_atoms} átomos")
    return np.frombuffer(raw, dtype="<f8", offset=4).reshape(atoms, 3).copy()


def _read_dcd_record(handle, endian: str) -> bytes | None:
    start = handle.tell()
    marker = handle.read(4)
    if len(marker) < 4:
        return None
    length = struct.unpack(endian + "i", marker)[0]
    if length < 0 or length > 2_000_000_000:
        raise ValueError("registro DCD inválido")
    payload = handle.read(length)
    closing = handle.read(4)
    if len(payload) != length or len(closing) != 4:
        handle.seek(start)
        return None
    if struct.unpack(endian + "i", closing)[0] != length:
        raise ValueError("marcadores DCD inconsistentes")
    return payload


def iter_dcd_ca_frames(path: Path, ca_indices: np.ndarray, expected_atoms: int, start_frame: int = 0, start_offset: int | None = None):
    """Lee frames completos de un DCD NAMD; ignora el frame parcial mientras se escribe."""
    with path.open("rb") as handle:
        marker = handle.read(4)
        if len(marker) != 4:
            return
        little = struct.unpack("<i", marker)[0]
        big = struct.unpack(">i", marker)[0]
        endian = "<" if little == 84 else ">" if big == 84 else None
        if endian is None:
            raise ValueError("encabezado DCD no reconocido")
        handle.seek(0)
        header = _read_dcd_record(handle, endian)
        if header is None or len(header) < 84 or header[:4] not in {b"CORD", b"VELD"}:
            raise ValueError("encabezado DCD incompleto")
        control = struct.unpack(endian + "20i", header[4:84])
        first_step, stride, fixed_atoms = control[1], control[2], control[8]
        if fixed_atoms:
            raise ValueError("DCD con átomos fijos no soportado")
        if _read_dcd_record(handle, endian) is None:
            return
        atom_record = _read_dcd_record(handle, endian)
        if atom_record is None or len(atom_record) != 4:
            return
        atom_count = struct.unpack(endian + "i", atom_record)[0]
        if atom_count != expected_atoms:
            raise ValueError(f"DCD contiene {atom_count}/{expected_atoms} átomos")
        dtype = np.dtype(endian + "f4")
        data_start = handle.tell()
        uses_cursor = start_offset is not None and data_start <= start_offset <= path.stat().st_size
        if uses_cursor:
            handle.seek(start_offset)
        frame_index = start_frame if uses_cursor else 0
        coordinate_bytes = atom_count * 4
        while True:
            frame_start = handle.tell()
            x_record = _read_dcd_record(handle, endian)
            if x_record is None:
                break
            if len(x_record) in {24, 48, 56}:
                x_record = _read_dcd_record(handle, endian)
                if x_record is None:
                    handle.seek(frame_start)
                    break
            y_record = _read_dcd_record(handle, endian)
            z_record = _read_dcd_record(handle, endian)
            if x_record is None or y_record is None or z_record is None:
                handle.seek(frame_start)
                break
            if not all(len(record) == coordinate_bytes for record in (x_record, y_record, z_record)):
                raise ValueError("dimensiones DCD incompatibles")
            if frame_index >= start_frame:
                x = np.frombuffer(x_record, dtype=dtype)[ca_indices]
                y = np.frombuffer(y_record, dtype=dtype)[ca_indices]
                z = np.frombuffer(z_record, dtype=dtype)[ca_indices]
                coordinates = np.column_stack((x, y, z)).astype(np.float64, copy=False)
                yield frame_index, first_step + frame_index * stride, coordinates, handle.tell()
            frame_index += 1


def read_snapshot_step(coor: Path) -> int | None:
    """Lee el timestep exacto del XSC rotatorio asociado al COOR."""
    if coor.name.endswith(".coor.old"):
        xsc = coor.with_name(coor.name[:-len(".coor.old")] + ".xsc.old")
    elif coor.name.endswith(".coor.BAK"):
        xsc = coor.with_name(coor.name[:-len(".coor.BAK")] + ".xsc.BAK")
    else:
        return None
    try:
        for line in reversed(xsc.read_text(encoding="utf-8", errors="replace").splitlines()):
            if line.strip() and not line.lstrip().startswith("#"):
                return int(float(line.split()[0]))
    except (OSError, ValueError, IndexError):
        pass
    return None


def align_mobile(mobile: np.ndarray, reference: np.ndarray) -> np.ndarray:
    mob_center = mobile.mean(axis=0)
    ref_center = reference.mean(axis=0)
    covariance = (mobile - mob_center).T @ (reference - ref_center)
    left, _, right = np.linalg.svd(covariance)
    sign = np.sign(np.linalg.det(left @ right))
    rotation = left @ np.diag([1.0, 1.0, sign]) @ right
    return (mobile - mob_center) @ rotation + ref_center


def load_structural_state(path: Path, n_ca: int) -> dict:
    if path.exists():
        try:
            saved = np.load(path, allow_pickle=False)
            if saved["mean"].shape == (n_ca, 3):
                return {"count": int(saved["count"]), "mean": saved["mean"], "m2": saved["m2"], "seen": json.loads(str(saved["seen"])), "rmsd": json.loads(str(saved["rmsd"]))}
        except Exception:
            pass
    return {"count": 0, "mean": np.zeros((n_ca, 3)), "m2": np.zeros((n_ca, 3)), "seen": {}, "rmsd": []}


def save_structural_state(path: Path, state: dict) -> None:
    np.savez_compressed(path, count=state["count"], mean=state["mean"], m2=state["m2"], seen=json.dumps(state["seen"]), rmsd=json.dumps(state["rmsd"]))


def update_structures(namd_dir: Path, state_path: Path) -> tuple[list[dict], list[dict]]:
    pdb = namd_dir / "step5_input.pdb"
    reference, ca_indices, residues = read_reference(pdb)
    ref_ca = reference[ca_indices]
    state = load_structural_state(state_path, len(ca_indices))
    changed = False
    point_indices = {(point.get("stage"), point.get("step")): index for index, point in enumerate(state["rmsd"])}
    for dcd in sorted(namd_dir.glob("step6*.dcd")):
        stage = dcd.stem
        seen_key = str(dcd) + "#frames"
        seen_dcd = state["seen"].get(seen_key, 0)
        start_frame = int(seen_dcd.get("frames", 0)) if isinstance(seen_dcd, dict) else int(seen_dcd)
        start_offset = (int(seen_dcd.get("offset", 0)) or None) if isinstance(seen_dcd, dict) else None
        try:
            if start_offset is not None and start_offset > dcd.stat().st_size:
                start_frame, start_offset = 0, None
        except OSError:
            continue
        processed_frames = start_frame
        processed_offset = start_offset
        try:
            for frame_index, step, mobile_ca, frame_end in iter_dcd_ca_frames(dcd, ca_indices, len(reference), start_frame, start_offset):
                processed_frames = frame_index + 1
                processed_offset = frame_end
                aligned = align_mobile(mobile_ca, ref_ca)
                rmsd = float(np.sqrt(np.mean(np.sum((aligned - ref_ca) ** 2, axis=1))))
                point = {"stage": stage, "step": step, "value": rmsd, "source": f"{dcd.name}#frame={frame_index + 1}", "capturedAt": datetime.now(timezone.utc).isoformat()}
                duplicate = point_indices.get((stage, step))
                if duplicate is not None:
                    state["rmsd"][duplicate] = point
                else:
                    delta = aligned - state["mean"]
                    state["count"] += 1
                    state["mean"] += delta / state["count"]
                    state["m2"] += delta * (aligned - state["mean"])
                    state["rmsd"].append(point)
                    point_indices[(stage, step)] = len(state["rmsd"]) - 1
                changed = True
        except (OSError, ValueError, np.linalg.LinAlgError):
            continue
        if processed_frames != start_frame:
            state["seen"][seen_key] = {"frames": processed_frames, "offset": processed_offset}
            changed = True
    candidates = sorted(set(namd_dir.glob("step6*.coor.old")) | set(namd_dir.glob("step6*.coor.BAK")))
    for coor in candidates:
        try:
            snapshot_stat = coor.stat()
        except OSError:
            # NAMD rota .coor.old de forma atómica; puede desaparecer entre
            # glob() y stat() durante unos milisegundos.
            continue
        signature = f"{snapshot_stat.st_mtime_ns}:{snapshot_stat.st_size}"
        if state["seen"].get(str(coor)) == signature:
            continue
        stage = re.sub(r"(?:\.restart)?\.coor\.(?:old|BAK)$", "", coor.name)
        out = namd_dir / f"{stage}.out"
        parsed = parse_output(out) if out.exists() else {"series": []}
        step = read_snapshot_step(coor)
        if step is None:
            step = parsed["series"][-1]["step"] if parsed["series"] else None
        if (stage, step) in point_indices:
            state["seen"][str(coor)] = signature
            changed = True
            continue
        try:
            aligned = align_mobile(read_namd_coor(coor, len(reference))[ca_indices], ref_ca)
        except (OSError, ValueError, np.linalg.LinAlgError):
            continue
        delta = aligned - state["mean"]
        state["count"] += 1
        state["mean"] += delta / state["count"]
        state["m2"] += delta * (aligned - state["mean"])
        rmsd = float(np.sqrt(np.mean(np.sum((aligned - ref_ca) ** 2, axis=1))))
        state["rmsd"].append({"stage": stage, "step": step, "value": rmsd, "source": coor.name, "capturedAt": datetime.now(timezone.utc).isoformat()})
        point_indices[(stage, step)] = len(state["rmsd"]) - 1
        state["seen"][str(coor)] = signature
        changed = True
    state["rmsd"].sort(key=lambda point: (_stage_number_for_sort(point.get("stage", "")), point.get("step") if point.get("step") is not None else -1))
    state["rmsd"] = state["rmsd"][-2000:]
    if changed:
        save_structural_state(state_path, state)
    rmsf = []
    if state["count"] >= 2:
        values = np.sqrt(np.sum(state["m2"] / (state["count"] - 1), axis=1))
        rmsf = [{**residues[i], "value": float(value)} for i, value in enumerate(values)]
    return state["rmsd"], rmsf


def build_payload(namd_dir: Path, output: Path, output_cache: IncrementalOutputCache | None = None) -> None:
    outputs = sorted(namd_dir.glob("step6*.out"))
    stages = []
    for path in outputs:
        try:
            stages.append(output_cache.parse(path) if output_cache else parse_output(path))
        except OSError:
            # Un RUN nuevo puede archivar los .out mientras se construye este ciclo.
            continue
    source_key = hashlib.sha1(str(namd_dir.resolve()).encode("utf-8")).hexdigest()[:10]
    state_path = output.with_name(f"structural_state_{source_key}.npz")
    try:
        rmsd_series, rmsf_series = update_structures(namd_dir, state_path)
        structural_error = None
    except (OSError, ValueError) as exc:
        rmsd_series, rmsf_series, structural_error = [], [], str(exc)
    seed_registry_path = output.with_name("seeds.json")
    previous_seeds = {}
    if seed_registry_path.exists():
        try:
            saved_registry = json.loads(seed_registry_path.read_text(encoding="utf-8"))
            if saved_registry.get("namdDir") == str(namd_dir):
                previous_seeds = saved_registry.get("stages", {})
        except (json.JSONDecodeError, OSError):
            previous_seeds = {}
    seed_registry = dict(previous_seeds)
    for stage in stages:
        if stage["seed"] is not None:
            seed_registry[stage["stage"]] = {
                "seed": stage["seed"], "source": stage["file"], "recordedAt": datetime.now(timezone.utc).isoformat()
            }
    all_rows = [row for stage in stages for row in stage["series"]]
    previous_stages = read_previous_run(namd_dir) if not all_rows else []
    errors = [{"stage": stage["stage"], "message": message} for stage in stages for message in stage["errors"]]
    agent_result = evaluate_equilibration(namd_dir, stages, rmsd_series)
    continuation = ensure_continuation_stage(namd_dir, agent_result)
    if continuation is not None:
        agent_result["continuation"] = continuation
        agent_result["nextAction"] = f"Ejecutar {continuation['stage']} y volver a evaluar al terminar."
    payload = {
        "source": str(namd_dir),
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "stages": stages,
        "summary": {
            "outputs": len(stages),
            "points": len(all_rows),
            "errors": len(errors),
            "last": all_rows[-1] if all_rows else None,
            "seed": next((s["seed"] for s in reversed(stages) if s["seed"] is not None), None),
        },
        "errors": errors,
        "previousRun": {"stages": previous_stages},
        "rmsd": {"status": "ready" if rmsd_series else "pending", "reason": structural_error or "Esperando el primer frame .dcd o cambio de .coor.old.", "series": rmsd_series},
        "rmsf": {"status": "ready" if rmsf_series else "pending", "reason": structural_error or "Se necesitan al menos dos snapshots alineados.", "series": rmsf_series},
        "agent": agent_result,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(output)
    decision_path = namd_dir / "equilibration_decision.json"
    decision_temporary = decision_path.with_suffix(".json.tmp")
    decision_temporary.write_text(json.dumps(agent_result, ensure_ascii=False, indent=2), encoding="utf-8")
    decision_temporary.replace(decision_path)
    seed_registry_path.write_text(json.dumps({"namdDir": str(namd_dir), "stages": seed_registry}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"{output}: {len(stages)} salidas, {len(all_rows)} puntos, {len(rmsd_series)} RMSD, {len(errors)} errores", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--namd-dir", type=Path, help="Carpeta namd con archivos .out")
    parser.add_argument("--output", type=Path, default=Path(__file__).parents[1] / "public/data/dashboard.json")
    parser.add_argument("--watch", action="store_true", help="Actualiza continuamente mientras NAMD se ejecuta")
    parser.add_argument("--interval", type=float, default=2.0, help="Segundos entre actualizaciones")
    args = parser.parse_args()
    project_root = Path(__file__).parents[2]
    namd_dir = (args.namd_dir or find_default_namd(project_root)).resolve()
    output_cache = IncrementalOutputCache() if args.watch else None
    while True:
        try:
            build_payload(namd_dir, args.output, output_cache)
        except OSError as exc:
            if not args.watch:
                raise
            print(f"Aviso transitorio del lector: {exc}", flush=True)
        if not args.watch:
            break
        time.sleep(max(0.5, args.interval))


if __name__ == "__main__":
    main()
