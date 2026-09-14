#!/usr/bin/env python3
"""Evaluador determinista y auditable del final de la preparación NAMD."""

from __future__ import annotations

import re
import statistics
import math
from pathlib import Path


MIN_EVIDENCE_PS = 100.0
REFERENCE_ATOM_COUNT = 100_000
EXTRA_PS_PER_100K_ATOMS = 50.0
MAX_SIZE_WINDOW_PS = 500.0
TARGET_TIMESTEP_FS = 4.0
TIMESTEP_TRANSITION_WINDOW_PS = 100.0
DCD_RMSD_INTERVAL_PS = 5.0
MIN_ENERGY_SAMPLES = 20
MIN_RMSD_SAMPLES = 5
TEMPERATURE_MEAN_TOLERANCE_K = 5.0
TEMPERATURE_STD_TOLERANCE_K = 15.0
ENERGY_DRIFT_NOISE_RATIO = 1.0
VOLUME_DRIFT_PERCENT = 2.0
RMSD_WINDOW_DRIFT_ANGSTROM = 0.5
PRESSURE_MEAN_TOLERANCE_BAR = 100.0


def _stage_number(stage_name: str) -> int | None:
    match = re.fullmatch(r"step6\.(\d+)_equilibration", stage_name)
    return int(match.group(1)) if match else None


def _linear_change(values: list[float], x_values: list[float]) -> float | None:
    if len(values) < 2 or len(values) != len(x_values):
        return None
    x_mean = statistics.fmean(x_values)
    y_mean = statistics.fmean(values)
    denominator = sum((x - x_mean) ** 2 for x in x_values)
    if denominator == 0:
        return 0.0
    slope = sum((x - x_mean) * (y - y_mean) for x, y in zip(x_values, values)) / denominator
    return slope * (x_values[-1] - x_values[0])


def _number_from_input(text: str, pattern: str) -> float | None:
    match = re.search(pattern, text, flags=re.MULTILINE | re.IGNORECASE)
    return float(match.group(1)) if match else None


def _atom_count(namd_dir: Path) -> int | None:
    psf = namd_dir / "step5_input.psf"
    if not psf.exists():
        return None
    try:
        with psf.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if "!NATOM" in line:
                    return int(line.split()[0])
    except (OSError, ValueError, IndexError):
        return None
    return None


def _minimum_window_ps(atom_count: int | None) -> float:
    """Piso heurístico transparente; la aprobación sigue dependiendo de las métricas."""
    if atom_count is None or atom_count <= REFERENCE_ATOM_COUNT:
        return MIN_EVIDENCE_PS
    raw = MIN_EVIDENCE_PS + ((atom_count - REFERENCE_ATOM_COUNT) / 100_000) * EXTRA_PS_PER_100K_ATOMS
    return min(MAX_SIZE_WINDOW_PS, math.ceil(raw / 50.0) * 50.0)


def evaluate_equilibration(namd_dir: Path, stages: list[dict], rmsd_series: list[dict]) -> dict:
    candidates = [stage for stage in stages if (_stage_number(stage["stage"]) or -1) >= 7]
    final = max(candidates, key=lambda stage: _stage_number(stage["stage"]) or -1, default=None)
    if final is None or final["status"] not in {"done", "error"}:
        return {
            "status": "waiting", "decision": "Esperando step6.7",
            "confidence": "sin evidencia", "reasons": ["La etapa step6.7 todavía no ha terminado."],
            "nextAction": "Completar la preparación hasta step6.7.", "metrics": {},
        }
    if final["status"] == "error" or final["errors"]:
        return {
            "status": "error", "decision": "Detener y corregir",
            "confidence": "alta", "reasons": final["errors"] or ["NAMD reportó un error en step6.7."],
            "nextAction": "Corregir el error de NAMD antes de extender la equilibración.", "metrics": {},
        }

    evaluated_stage = final["stage"]
    inp = namd_dir / f"{evaluated_stage}.inp"
    text = inp.read_text(encoding="utf-8", errors="replace") if inp.exists() else ""
    atom_count = _atom_count(namd_dir)
    size_adjusted_window_ps = _minimum_window_ps(atom_count)
    target_temperature = _number_from_input(text, r"^\s*set\s+temp\s+([0-9.eE+-]+)") or 303.15
    restraint_scale = _number_from_input(text, r"^\s*constraintScaling\s+([0-9.eE+-]+)")
    constraints_on = bool(re.search(r"^\s*constraints\s+(?:on|yes)\b", text, flags=re.MULTILINE | re.IGNORECASE))

    rows = final["series"]
    half = rows[len(rows) // 2:] if rows else []
    timestep_fs = float(final.get("timestepFs") or 1.0)
    unrestrained = not constraints_on or restraint_scale == 0
    minimum_window_ps = size_adjusted_window_ps if timestep_fs >= TARGET_TIMESTEP_FS or not unrestrained else min(size_adjusted_window_ps, TIMESTEP_TRANSITION_WINDOW_PS)
    configured_steps = _number_from_input(text, r"^\s*run\s+([0-9.eE+-]+)")
    duration_ps = configured_steps * timestep_fs / 1000.0 if configured_steps is not None else (((rows[-1]["step"] - rows[0]["step"]) * timestep_fs / 1000.0) if len(rows) >= 2 else 0.0)
    times = [float(row["step"]) for row in half]
    temperatures = [row["temperature"] for row in half if row.get("temperature") is not None]
    energies = [row["energy"] for row in half if row.get("energy") is not None]
    volumes = [row["volume"] for row in half if row.get("volume") is not None]
    pressures = [row["pressure"] for row in half if row.get("pressure") is not None]
    temperature_mean = statistics.fmean(temperatures) if temperatures else None
    temperature_std = statistics.pstdev(temperatures) if len(temperatures) >= 2 else None
    energy_change = _linear_change(energies, times[-len(energies):]) if energies else None
    energy_noise = statistics.pstdev(energies) if len(energies) >= 2 else None
    energy_ratio = abs(energy_change) / max(energy_noise, 1.0) if energy_change is not None and energy_noise is not None else None
    volume_change = _linear_change(volumes, times[-len(volumes):]) if volumes else None
    volume_drift = abs(volume_change) / abs(statistics.fmean(volumes)) * 100 if volume_change is not None and volumes else None
    pressure_mean = statistics.fmean(pressures) if pressures else None
    pressure_std = statistics.pstdev(pressures) if len(pressures) >= 2 else None

    rmsd = [point for point in rmsd_series if point.get("stage") == evaluated_stage and point.get("step") is not None]
    rmsd_values = [float(point["value"]) for point in rmsd]
    rmsd_steps = [float(point["step"]) for point in rmsd]
    rmsd_change = _linear_change(rmsd_values, rmsd_steps)

    reasons: list[str] = []
    if duration_ps < minimum_window_ps:
        size_note = f" en un sistema de {atom_count:,} átomos" if atom_count is not None else ""
        reasons.append(f"{evaluated_stage} aporta {duration_ps:.3f} ps; se requieren al menos {minimum_window_ps:.0f} ps{size_note} para evaluar estabilidad.")
    if len(rows) < MIN_ENERGY_SAMPLES:
        reasons.append(f"Solo hay {len(rows)} muestras ENERGY; se requieren al menos {MIN_ENERGY_SAMPLES}.")
    if constraints_on and (restraint_scale is None or restraint_scale > 0):
        value = "desconocida" if restraint_scale is None else f"{restraint_scale:g}"
        reasons.append(f"{evaluated_stage} todavía conserva restricciones posicionales con escala {value}.")
    if temperature_mean is None or abs(temperature_mean - target_temperature) > TEMPERATURE_MEAN_TOLERANCE_K:
        reasons.append("La temperatura media no está suficientemente cerca del objetivo.")
    if temperature_std is not None and temperature_std > TEMPERATURE_STD_TOLERANCE_K:
        reasons.append(f"La fluctuación térmica es alta (σ={temperature_std:.2f} K).")
    if energy_ratio is None or energy_ratio > ENERGY_DRIFT_NOISE_RATIO:
        reasons.append("La energía potencial todavía presenta una deriva comparable o superior a sus fluctuaciones.")
    if volume_drift is not None and volume_drift > VOLUME_DRIFT_PERCENT:
        reasons.append(f"El volumen deriva {volume_drift:.2f}% en la mitad final de la etapa.")
    if len(rmsd) < MIN_RMSD_SAMPLES:
        reasons.append(f"Solo hay {len(rmsd)} checkpoints RMSD de {evaluated_stage}; se requieren al menos {MIN_RMSD_SAMPLES}.")
    elif rmsd_change is not None and abs(rmsd_change) > RMSD_WINDOW_DRIFT_ANGSTROM:
        reasons.append(f"El RMSD cambia {rmsd_change:+.3f} Å dentro de la ventana observada.")
    if duration_ps >= minimum_window_ps and pressure_mean is not None and abs(pressure_mean - 1.01325) > PRESSURE_MEAN_TOLERANCE_BAR:
        reasons.append(f"La presión media ({pressure_mean:.1f} bar) permanece lejos del objetivo tras promediar la ventana.")

    stability_ready = not reasons
    if timestep_fs < TARGET_TIMESTEP_FS:
        reasons.append(f"La etapa usa {timestep_fs:g} fs; el objetivo HMR debe validarse a {TARGET_TIMESTEP_FS:g} fs.")
    elif timestep_fs > TARGET_TIMESTEP_FS:
        reasons.append(f"El timestep de {timestep_fs:g} fs supera el objetivo HMR validado de {TARGET_TIMESTEP_FS:g} fs.")
    ready = stability_ready and abs(timestep_fs - TARGET_TIMESTEP_FS) < 1e-9
    if ready:
        next_action = "La preparación puede avanzar a producción; conservar este informe y las semillas."
    elif constraints_on and (restraint_scale is None or restraint_scale > 0):
        next_action = "Crear una etapa sin restricciones y volver a evaluar antes de aumentar el timestep."
    elif stability_ready and timestep_fs < TARGET_TIMESTEP_FS:
        next_action = f"Crear el siguiente escalón HMR por encima de {timestep_fs:g} fs y validarlo."
    else:
        next_action = "Extender la equilibración y volver a evaluar cuando exista una ventana estable de al menos 100 ps."
    confidence = "alta" if duration_ps >= minimum_window_ps and len(rmsd) >= MIN_RMSD_SAMPLES else "baja"
    return {
        "status": "ready" if ready else "continue",
        "decision": "Equilibración suficiente" if ready else "Continuar equilibrando",
        "confidence": confidence,
        "evaluatedStage": evaluated_stage,
        "stabilityReady": stability_ready,
        "reasons": reasons or ["Temperatura, energía, volumen, presión y RMSD cumplen los criterios configurados."],
        "nextAction": next_action,
        "metrics": {
            "durationPs": duration_ps, "energySamples": len(rows), "rmsdSamples": len(rmsd),
            "atomCount": atom_count, "minimumWindowPs": minimum_window_ps,
            "sizeAdjustedWindowPs": size_adjusted_window_ps,
            "timestepFs": timestep_fs, "targetTimestepFs": TARGET_TIMESTEP_FS,
            "targetTemperatureK": target_temperature, "temperatureMeanK": temperature_mean,
            "temperatureStdK": temperature_std, "energyWindowChange": energy_change,
            "energyDriftNoiseRatio": energy_ratio, "volumeDriftPercent": volume_drift,
            "pressureMeanBar": pressure_mean, "pressureStdBar": pressure_std,
            "rmsdWindowChangeAngstrom": rmsd_change, "restraintScale": restraint_scale,
        },
    }


def ensure_continuation_stage(namd_dir: Path, evaluation: dict) -> dict | None:
    """Crea una etapa NPT sin restricciones cuando el evaluador pide continuar."""
    if evaluation.get("status") != "continue":
        return None
    source_stage = evaluation.get("evaluatedStage")
    source_number = _stage_number(source_stage or "")
    if source_number is None:
        return None
    next_number = source_number + 1
    next_stage = f"step6.{next_number}_equilibration"
    destination = namd_dir / f"{next_stage}.inp"
    source = namd_dir / f"{source_stage}.inp"
    if not source.exists():
        return None
    text = source.read_text(encoding="utf-8", errors="replace")
    timestep_fs = _number_from_input(text, r"^\s*timestep\s+([0-9.eE+-]+)") or 1.0
    source_first = int(_number_from_input(text, r"^\s*firsttimestep\s+([0-9.eE+-]+)") or 0)
    source_steps = int(_number_from_input(text, r"^\s*run\s+([0-9.eE+-]+)") or 0)
    atom_count = evaluation.get("metrics", {}).get("atomCount")
    size_window_ps = _minimum_window_ps(int(atom_count) if atom_count is not None else None)
    stability_ready = bool(evaluation.get("stabilityReady"))
    if source_number >= 8 and stability_ready and timestep_fs < TARGET_TIMESTEP_FS:
        next_timestep_fs = min(TARGET_TIMESTEP_FS, timestep_fs + 1.0)
    elif timestep_fs > TARGET_TIMESTEP_FS:
        next_timestep_fs = TARGET_TIMESTEP_FS
    else:
        next_timestep_fs = timestep_fs
    if source_number == 7 or next_timestep_fs >= TARGET_TIMESTEP_FS:
        window_ps = size_window_ps
    else:
        window_ps = min(size_window_ps, TIMESTEP_TRANSITION_WINDOW_PS)
    atom_label = f"{int(atom_count):,}" if atom_count is not None else "unknown"
    continuation_steps = max(1, round(window_ps * 1000.0 / next_timestep_fs))
    dcd_frequency = max(1, round(DCD_RMSD_INTERVAL_PS * 1000.0 / next_timestep_fs))

    if destination.exists():
        existing = destination.read_text(encoding="utf-8", errors="replace")
        existing_run = int(_number_from_input(existing, r"^\s*run\s+([0-9.eE+-]+)") or 0)
        existing_dcd_frequency = int(_number_from_input(existing, r"^\s*dcdfreq\s+([0-9.eE+-]+)") or 0)
        generated = existing.startswith("# Generated by the equilibration agent")
        has_output = (namd_dir / f"{next_stage}.out").exists()
        status = "existing"
        if generated and not has_output and (existing_run < continuation_steps or existing_dcd_frequency != dcd_frequency):
            existing = re.sub(r"(?m)^# Unrestrained NPT observation window:.*$", f"# Unrestrained NPT observation window: {window_ps:g} ps for {atom_label} atoms.", existing, count=1)
            existing = re.sub(r"(?m)^\s*run\s+\d+[^\r\n]*$", f"run                     {continuation_steps}", existing, count=1)
            existing = re.sub(r"(?mi)^\s*dcdfreq\s+\d+[^\r\n]*$", f"dcdfreq                 {dcd_frequency};               # frame RMSD cada {DCD_RMSD_INTERVAL_PS:g} ps", existing, count=1)
            temporary = destination.with_suffix(".inp.tmp")
            temporary.write_text(existing, encoding="utf-8", newline="")
            temporary.replace(destination)
            status = "updated"
        existing_timestep = _number_from_input(existing, r"^\s*timestep\s+([0-9.eE+-]+)") or next_timestep_fs
        return {"stage": next_stage, "file": destination.name, "status": status, "runSteps": max(existing_run, continuation_steps), "durationPs": window_ps, "timestepFs": existing_timestep, "dcdFrequency": dcd_frequency, "dcdIntervalPs": DCD_RMSD_INTERVAL_PS, "atomCount": atom_count}

    text = re.sub(r"(?m)^\s*set outputname\s+[^;\r\n]+;?", f"set outputname          {next_stage};", text, count=1)
    text = re.sub(r"(?m)^\s*set inputname\s+[^;\r\n]+;?", f"set inputname           {source_stage};", text, count=1)
    text = re.sub(r"(?m)^\s*firsttimestep\s+\d+[^\r\n]*$", f"firsttimestep           {source_first + source_steps};", text, count=1)
    text = re.sub(r"(?mi)^\s*timestep\s+[0-9.eE+-]+[^\r\n]*$", f"timestep                {next_timestep_fs:g};                # fs/step · escalón HMR", text, count=1)
    text = re.sub(r"(?mi)^\s*dcdfreq\s+\d+[^\r\n]*$", f"dcdfreq                 {dcd_frequency};               # frame RMSD cada {DCD_RMSD_INTERVAL_PS:g} ps", text, count=1)
    text = re.sub(r"(?m)^\s*run\s+\d+[^\r\n]*$", f"run                     {continuation_steps}", text, count=1)

    text = text.replace(f"NAMD_SEED_6_{source_number}", f"NAMD_SEED_6_{next_number}")
    text = text.replace(f"Semilla de step6.{source_number}", f"Semilla de step6.{next_number}")
    text = text.replace(f"(step6.{source_number})", f"(step6.{next_number})")

    # La etapa de decisión observa el sistema libre bajo el mismo ensamble NPT.
    text = re.sub(r"(?mi)^\s*(?:constraints|consexp|consref|conskfile|conskcol|constraintScaling)\s+[^\r\n]*\r?\n?", "", text)
    text = re.sub(r"(?mi)^\s*(?:reassignFreq|reassignTemp|reassignIncr|reassignHold)\s+[^\r\n]*\r?\n?", "", text)
    text = re.sub(r"(?ms)^# planar restraint\s*.*?^\s*colvarsConfig\s+[^\r\n]*\r?\n?", "", text)
    text = re.sub(r"(?ms)^# dihedral restraint\s*.*?^\s*extraBondsFile\s+[^\r\n]*\r?\n?", "", text)
    provenance = (
        f"# Generated by the equilibration agent from {source_stage}.inp.\n"
        f"# Unrestrained NPT observation window: {window_ps:g} ps at {next_timestep_fs:g} fs for {atom_label} atoms.\n"
    )
    temporary = destination.with_suffix(".inp.tmp")
    temporary.write_text(provenance + text.lstrip(), encoding="utf-8", newline="")
    temporary.replace(destination)
    return {
        "stage": next_stage, "file": destination.name, "status": "created",
        "runSteps": continuation_steps, "durationPs": window_ps, "timestepFs": next_timestep_fs, "dcdFrequency": dcd_frequency, "dcdIntervalPs": DCD_RMSD_INTERVAL_PS, "atomCount": atom_count,
        "inputStage": source_stage, "restrained": False,
    }
