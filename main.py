import argparse
import json
import re
import shutil
import tarfile
import tempfile
from pathlib import Path, PurePosixPath

from collect_namd_dependencies import collect_dependencies
from split_readme import split_readme


MAX_MEMBERS = 100_000
MAX_TOTAL_SIZE = 2 * 1024**3
TEXT_SUFFIXES = {"", ".conf", ".inp", ".namd", ".sh", ".str", ".tcl", ".txt"}
THERMALIZATION_STEPS = 2_000
THERMALIZATION_RAMP_STEPS = 1_000
EQUILIBRATION_STEPS = 1_000
MINIMIZATION_STEPS = 1_000  # Duración reducida temporalmente para pruebas del pipeline.
PREPARATION_OUTPUT_ENERGIES = 100
PREPARATION_COMPUTE_ENERGIES = 100
STRUCTURAL_RESTART_FREQUENCY = 100
PREPARATION_DCD_INTERVAL_PS = 0.5


def _safe_extract(archive_path: Path, destination: Path) -> None:
    count = 0
    total_size = 0
    with tarfile.open(archive_path, "r:gz") as archive:
        for member in archive:
            count += 1
            total_size += member.size
            if count > MAX_MEMBERS:
                raise ValueError("El paquete contiene demasiados elementos.")
            if total_size > MAX_TOTAL_SIZE:
                raise ValueError("El contenido descomprimido supera 2 GB.")

            path = PurePosixPath(member.name)
            if path.is_absolute() or ".." in path.parts:
                raise ValueError(f"Ruta insegura dentro del paquete: {member.name}")
            if member.issym() or member.islnk() or member.isdev():
                raise ValueError(f"Tipo de elemento no permitido: {member.name}")

            target = destination.joinpath(*path.parts).resolve()
            try:
                target.relative_to(destination.resolve())
            except ValueError as exc:
                raise ValueError(f"Ruta insegura dentro del paquete: {member.name}") from exc

            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            elif member.isfile():
                target.parent.mkdir(parents=True, exist_ok=True)
                source = archive.extractfile(member)
                if source is None:
                    raise ValueError(f"No se pudo leer {member.name}")
                with source, target.open("xb") as output:
                    shutil.copyfileobj(source, output)


def _find_namd(extracted: Path) -> Path:
    matches = sorted(
        path for path in extracted.rglob("*")
        if path.is_dir() and path.name.lower() == "namd"
    )
    if len(matches) != 1:
        raise ValueError(
            f"Se esperaba una carpeta namd y se encontraron {len(matches)}."
        )
    return matches[0]


def _replace_namd_command(namd_dir: Path) -> list[str]:
    changed: list[str] = []
    for path in sorted(item for item in namd_dir.rglob("*") if item.is_file()):
        if path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        try:
            data = path.read_bytes()
            if b"\x00" in data:
                continue
            text = data.decode("utf-8-sig")
        except (OSError, UnicodeDecodeError):
            continue
        updated = re.sub(r"\bnamd(?:2)?\b", "namd3", text)
        if updated != text:
            path.write_text(updated, encoding="utf-8", newline="")
            changed.append(str(path.relative_to(namd_dir)))
    return changed


def _split_namd_readme(namd_dir: Path) -> list[str]:
    candidates = sorted(
        path for path in namd_dir.iterdir()
        if path.is_file()
        and (path.name.lower() == "readme" or path.name.lower().startswith("readme."))
        and path.name.lower() not in {
            "readme_preparacion", "readme_equilibracion",
            "readme.equilibracion", "readme.produccion",
        }
    )
    if len(candidates) != 1:
        raise ValueError(
            f"Se esperaba un README principal en namd y se encontraron {len(candidates)}."
        )
    readme = candidates[0]
    preparation, equilibration = split_readme(readme.read_text(encoding="utf-8-sig"))
    equilibration = re.sub(
        r"(?ms)^# Running minimization\n.*?"
        r"^# Running gradual thermalization\n.*?^@ cnt \+= 1\n\n",
        "",
        equilibration,
        count=1,
    )
    outputs = {
        namd_dir / "README_preparacion": preparation,
        namd_dir / "README_equilibracion": equilibration,
    }
    for path in outputs:
        if path.exists():
            raise ValueError(f"La salida ya existe: {path.name}")
    for path, text in outputs.items():
        path.write_text(text, encoding="utf-8", newline="")
    return [path.name for path in outputs]


def _make_readme_fail_fast(text: str) -> str:
    """Detiene la cadena si una etapa NAMD falla o es terminada."""
    lines = text.splitlines(keepends=True)
    updated: list[str] = []
    for index, line in enumerate(lines):
        updated.append(line)
        if not re.match(r"^\s*(?:namd3|\$namd_min)\s+", line):
            continue
        next_line = lines[index + 1] if index + 1 < len(lines) else ""
        if re.match(r"^\s*if\s*\(\s*\$status\s*!=\s*0\s*\)", next_line):
            continue
        indent = re.match(r"^\s*", line).group(0)
        updated.append(f"{indent}if ( $status != 0 ) exit $status\n")
    return "".join(updated)


def _without_velocity_reassignment(text: str) -> str:
    return re.sub(
        r"(?m)^\s*reassign(?:Freq|Temp)\s+[^\r\n]*\r?\n?", "", text
    )


def _without_thermalization_restraints(text: str) -> str:
    """Retira las restricciones planar y diedral solo de la termalización."""
    text = re.sub(
        r"(?ms)^# planar restraint\s*\r?\n.*?^colvarsConfig\s+[^\r\n]*\r?\n?",
        "",
        text,
        count=1,
    )
    return re.sub(
        r"(?ms)^# dihedral restraint\s*\r?\n.*?^extraBondsFile\s+[^\r\n]*\r?\n?",
        "",
        text,
        count=1,
    )


def _add_restart_input(
    text: str,
    input_name: str,
    first_timestep: int,
    *,
    include_velocities: bool = True,
) -> str:
    marker = re.search(r"(?m)^source\s+step5_input\.str\s*;?\s*$", text)
    if marker is None:
        raise ValueError("No se encontró 'source step5_input.str' en step6.1.")
    velocity_line = "binVelocities           $inputname.vel;\n" if include_velocities else ""
    restart = (
        f"\nset inputname           {input_name};\n"
        "binCoordinates          $inputname.coor;\n"
        f"{velocity_line}"
        "extendedSystem          $inputname.xsc;\n"
    )
    text = text[: marker.end()] + restart + text[marker.end() :]
    if include_velocities:
        # A restart already supplies velocities. NAMD rejects an additional
        # initial-temperature directive in the same configuration.
        text = re.sub(r"(?m)^\s*temperature\s+[^\r\n]*\r?\n?", "", text, count=1)
    return re.sub(
        r"(?m)^firsttimestep\s+\d+[^\r\n]*$",
        f"firsttimestep           {first_timestep};",
        text,
        count=1,
    )


def _source_directives(text: str) -> list[str]:
    preserved = []
    for line in text.splitlines():
        code = line.split("#", 1)[0].strip()
        if re.match(r"(?i)^(structure|coordinates|parameters)\s+", code):
            preserved.append(code.rstrip(";"))
    return preserved


def _validate_charmm_gui_provenance(
    original: str, derived: dict[str, str]
) -> int:
    expected = _source_directives(original)
    if not expected:
        raise ValueError(
            "La configuración de CHARMM-GUI no contiene estructura, coordenadas "
            "o parámetros verificables."
        )
    for name, text in derived.items():
        actual = set(_source_directives(text))
        missing = [directive for directive in expected if directive not in actual]
        if missing:
            raise ValueError(
                f"{name} perdió directivas del archivo de CHARMM-GUI: "
                + ", ".join(missing)
            )
    return len(expected)


def _add_stage_seed(text: str, stage_id: str) -> str:
    """Añade y registra la semilla usada por una etapa dinámica de NAMD."""
    if "NAMD random seed:" in text:
        return text
    env_name = "NAMD_SEED_" + stage_id.replace(".", "_")
    block = (
        f"\n# Semilla de step{stage_id}; {env_name} permite reproducir esta etapa.\n"
        f"if {{[info exists env({env_name})]}} {{\n"
        f"    set random_seed $env({env_name})\n"
        "} else {\n"
        "    set random_seed [expr {abs(([clock clicks] ^ [pid]) % 2147483646) + 1}]\n"
        "}\n"
        "seed                    $random_seed\n"
        f"print                   \"NAMD random seed: $random_seed (step{stage_id})\"\n"
    )
    match = re.search(r"(?m)^set outputname\s+[^\r\n]+$", text)
    if match is None:
        raise ValueError(f"No se encontró outputname al asignar semilla a step{stage_id}.")
    return text[: match.end()] + block + text[match.end() :]


def _enable_gpu_resident(text: str, stage_id: str) -> str:
    """Activa GPU-resident en las equilibraciones step6.2 en adelante."""
    if re.search(r"(?mi)^\s*GPUresident\s+on\s*;?\s*$", text):
        return text
    match = re.search(r"(?m)^set outputname\s+[^\r\n]+$", text)
    if match is None:
        raise ValueError(f"No se encontró outputname al activar GPUresident en step{stage_id}.")
    return text[: match.end()] + "\nGPUresident             on" + text[match.end() :]


def _set_energy_frequency(text: str, stage_name: str) -> str:
    """Separa explícitamente cálculo y salida con una frecuencia eficiente en CUDA."""
    output_line = (
        f"outputEnergies          {PREPARATION_OUTPUT_ENERGIES};"
        "               # ENERGY cada 100 pasos"
    )
    text, output_updates = re.subn(
        r"(?m)^\s*outputEnergies\s+\d+[^\r\n]*$",
        output_line,
        text,
        count=1,
    )
    if output_updates != 1:
        raise ValueError(f"No se encontró outputEnergies en {stage_name}.")
    compute_line = (
        f"computeEnergies         {PREPARATION_COMPUTE_ENERGIES};"
        "               # evita evaluaciones de energía costosas en CUDA"
    )
    if re.search(r"(?mi)^\s*computeEnergies\s+\d+[^\r\n]*$", text):
        return re.sub(
            r"(?mi)^\s*computeEnergies\s+\d+[^\r\n]*$",
            compute_line,
            text,
            count=1,
        )
    return text.replace(output_line, output_line + "\n" + compute_line, 1)


def _set_dcd_interval(text: str, stage_name: str) -> str:
    """Configura DCD por tiempo físico para alimentar el RMSD."""
    match = re.search(r"(?mi)^\s*timestep\s+([0-9.eE+-]+)", text)
    if match is None:
        raise ValueError(f"No se encontró timestep en {stage_name}.")
    timestep_fs = float(match.group(1))
    frequency = max(1, round(PREPARATION_DCD_INTERVAL_PS * 1000.0 / timestep_fs))
    text, updates = re.subn(
        r"(?mi)^\s*dcdfreq\s+\d+[^\r\n]*$",
        f"dcdfreq                 {frequency};               # frame RMSD cada {PREPARATION_DCD_INTERVAL_PS:g} ps",
        text,
        count=1,
    )
    if updates != 1:
        raise ValueError(f"No se encontró dcdfreq en {stage_name}.")
    return text


def _separate_minimization_and_thermalization(namd_dir: Path) -> list[str]:
    equilibration_path = namd_dir / "step6.1_equilibration.inp"
    original = equilibration_path.read_text(encoding="utf-8-sig")
    execution = re.search(r"(?m)^\s*minimize\s+(\d+)\s*$", original)
    if execution is None:
        raise ValueError("step6.1_equilibration.inp no contiene una minimización.")
    minimization_steps = MINIMIZATION_STEPS
    common = original[: execution.start()].rstrip() + "\n\n"
    provenance = (
        "# Derived from CHARMM-GUI step6.1_equilibration.inp.\n"
        "# System-specific structure, force field, box and restraints are preserved.\n"
    )

    minimization = re.sub(
        r"(?m)^set outputname\s+[^;\r\n]+;?",
        "set outputname          step6.0_minimization;",
        common,
        count=1,
    )
    minimization = provenance + minimization
    minimization += f"minimize                {minimization_steps}\n"
    minimization += "output                  step6.0_minimization\n"

    thermalization = re.sub(
        r"(?m)^set outputname\s+[^;\r\n]+;?",
        "set outputname          step6.1_thermalization;",
        common,
        count=1,
    )
    thermalization = _without_velocity_reassignment(thermalization)
    thermalization = _without_thermalization_restraints(thermalization)
    thermalization = _set_energy_frequency(thermalization, "la termalización")
    thermalization = _set_dcd_interval(thermalization, "la termalización")
    thermalization = re.sub(
        r"(?m)^temperature\s+[^\r\n]+$",
        "temperature             1.0;",
        thermalization,
        count=1,
    )
    thermalization = _add_restart_input(
        thermalization,
        "step6.0_minimization",
        minimization_steps,
        include_velocities=False,
    )
    # La minimización no entrega velocidades útiles. Se crea una rampa lineal
    # reproducible y luego Langevin mantiene la temperatura objetivo.
    thermalization = thermalization.replace(
        "binCoordinates          $inputname.coor;\n",
        "binCoordinates          $inputname.coor;\n"
        "# Semilla de step6.1; NAMD_SEED_6_1 permite repetirla exactamente.\n"
        "if {[info exists env(NAMD_SEED_6_1)]} {\n"
        "    set random_seed $env(NAMD_SEED_6_1)\n"
        "} else {\n"
        "    set random_seed [expr {abs(([clock clicks] ^ [pid]) % 2147483646) + 1}]\n"
        "}\n"
        "seed                    $random_seed\n"
        "print                   \"NAMD random seed: $random_seed (step6.1)\"\n"
    )
    thermalization += (
        "# Rampa lineal de temperatura: 1 K -> $temp en 1000 pasos.\n"
        "set ramp_start          1.0\n"
        f"set ramp_steps          {THERMALIZATION_RAMP_STEPS}\n"
        "set ramp_increment      [expr {($temp - $ramp_start) / ($ramp_steps - 1)}]\n"
        "reassignFreq            1\n"
        "reassignTemp            $ramp_start\n"
        "reassignIncr            $ramp_increment\n"
        "reassignHold            $temp\n"
        "# NAMD no permite cambiar reassignFreq después de iniciar run.\n"
        "# Tras alcanzar $temp, reassignHold mantiene el objetivo hasta el final.\n"
        f"run                     {THERMALIZATION_STEPS}\n"
    )
    thermalization = provenance + thermalization

    equilibration = _without_velocity_reassignment(common)
    equilibration = _set_energy_frequency(equilibration, "la primera equilibración")
    equilibration = _set_dcd_interval(equilibration, "la primera equilibración")
    equilibration, restart_frequency_updates = re.subn(
        r"(?m)^\s*restartfreq\s+\d+[^\r\n]*$",
        f"restartfreq             {STRUCTURAL_RESTART_FREQUENCY};               # snapshot para RMSD cada 100 pasos",
        equilibration,
        count=1,
    )
    if restart_frequency_updates != 1:
        raise ValueError("No se encontró restartfreq en la primera equilibración.")
    equilibration = _add_restart_input(
        equilibration,
        "step6.1_thermalization",
        minimization_steps + THERMALIZATION_STEPS,
    )
    equilibration += f"run                     {EQUILIBRATION_STEPS}\n"
    equilibration = provenance + equilibration

    _validate_charmm_gui_provenance(
        original,
        {
            "step6.0_minimization.inp": minimization,
            "step6.1_thermalization.inp": thermalization,
            "step6.1_equilibration.inp": equilibration,
        },
    )

    minimization_path = namd_dir / "step6.0_minimization.inp"
    thermalization_path = namd_dir / "step6.1_thermalization.inp"
    for path in (minimization_path, thermalization_path):
        if path.exists():
            raise ValueError(f"La etapa ya existe: {path.name}")
    minimization_path.write_text(minimization, encoding="utf-8", newline="")
    thermalization_path.write_text(thermalization, encoding="utf-8", newline="")
    equilibration_path.write_text(equilibration, encoding="utf-8", newline="")

    for path in sorted(namd_dir.glob("step6.*_equilibration.inp")):
        if path == equilibration_path:
            continue
        text = path.read_text(encoding="utf-8-sig")
        text, replacements = re.subn(
            r"(?m)^(run\s+)\d+([^\r\n]*)$",
            rf"\g<1>{EQUILIBRATION_STEPS}\g<2>",
            text,
        )
        if replacements < 1:
            raise ValueError(f"No se encontró run en {path.name}.")
        text = _set_energy_frequency(text, path.name)
        text = _set_dcd_interval(text, path.name)
        text, restart_frequency_updates = re.subn(
            r"(?m)^\s*restartfreq\s+\d+[^\r\n]*$",
            f"restartfreq             {STRUCTURAL_RESTART_FREQUENCY};               # snapshot para RMSD cada 100 pasos",
            text,
            count=1,
        )
        if restart_frequency_updates != 1:
            raise ValueError(f"No se encontró restartfreq en {path.name}.")
        path.write_text(text, encoding="utf-8", newline="")

    # 6.0 y 6.1 quedan reservados para minimización y termalización.
    # Se renombran las equilibraciones en orden descendente para evitar colisiones.
    for path in sorted(namd_dir.glob("step6.*_equilibration.inp"), reverse=True):
        match = re.fullmatch(r"step6\.(\d+)_equilibration\.inp", path.name)
        if match is None:
            continue
        new_index = int(match.group(1)) + 1
        text = path.read_text(encoding="utf-8-sig")
        text = re.sub(
            r"step6\.(\d+)_equilibration",
            lambda item: f"step6.{int(item.group(1)) + 1}_equilibration",
            text,
        )
        new_path = namd_dir / f"step6.{new_index}_equilibration.inp"
        if new_path.exists():
            raise ValueError(f"La etapa renumerada ya existe: {new_path.name}")
        path.unlink()
        new_path.write_text(text, encoding="utf-8", newline="")

    # La primera equilibración siempre debe continuar desde la termalización.
    first_equilibration = namd_dir / "step6.2_equilibration.inp"
    first_text = first_equilibration.read_text(encoding="utf-8-sig")
    first_text, replacements = re.subn(
        r"(?m)^set inputname\s+[^;\r\n]+;?",
        "set inputname           step6.1_thermalization;",
        first_text,
        count=1,
    )
    if replacements != 1:
        raise ValueError("No se pudo enlazar step6.2 con step6.1_thermalization.")
    first_equilibration.write_text(first_text, encoding="utf-8", newline="")

    # Cada proceso dinámico registra su propia semilla y admite reproducción
    # mediante NAMD_SEED_6_2, NAMD_SEED_6_3, etc.
    for path in sorted(namd_dir.glob("step6.*_equilibration.inp")):
        match = re.fullmatch(r"step6\.(\d+)_equilibration\.inp", path.name)
        if match is None:
            continue
        stage_id = f"6.{match.group(1)}"
        text = path.read_text(encoding="utf-8-sig")
        expected_first = (
            minimization_steps
            + THERMALIZATION_STEPS
            + (int(match.group(1)) - 2) * EQUILIBRATION_STEPS
        )
        text, replacements = re.subn(
            r"(?m)^(firsttimestep\s+)\d+([^\r\n]*)$",
            rf"\g<1>{expected_first}\g<2>",
            text,
            count=1,
        )
        if replacements != 1:
            raise ValueError(f"No se encontró firsttimestep en {path.name}.")
        text = _enable_gpu_resident(text, stage_id)
        text = _add_stage_seed(text, stage_id)
        path.write_text(text, encoding="utf-8", newline="")

    production = namd_dir / "step7_production.inp"
    production_text = production.read_text(encoding="utf-8-sig")
    production_text = re.sub(
        r"step6\.(\d+)_equilibration",
        lambda item: f"step6.{int(item.group(1)) + 1}_equilibration",
        production_text,
    )
    production.write_text(production_text, encoding="utf-8", newline="")

    readme = namd_dir / "README"
    readme_text = readme.read_text(encoding="utf-8-sig")
    marker = "# Running equilibration steps"
    if marker not in readme_text:
        raise ValueError("No se encontró el bloque de equilibración en README.")
    prelude = (
        "# Running minimization\n"
        "set cnt    = 0\n"
        "namd3 step6.${cnt}_minimization.inp > step6.${cnt}_minimization.out\n"
        "@ cnt += 1\n\n"
        "# Running gradual thermalization\n"
        "namd3 step6.${cnt}_thermalization.inp > step6.${cnt}_thermalization.out\n"
        "@ cnt += 1\n\n"
    )
    readme_text = readme_text.replace(marker, prelude + marker, 1)
    readme_text = readme_text.replace(
        "set cnt    = 1\nset cntmax = 6", "set cnt    = 2\nset cntmax = 7", 1
    )
    readme_text = readme_text.replace(
        "printf ${equi_prefix} 6", "printf ${equi_prefix} 7"
    )
    readme.write_text(
        _make_readme_fail_fast(readme_text),
        encoding="utf-8",
        newline="",
    )
    return [
        minimization_path.name,
        thermalization_path.name,
        "step6.2_equilibration.inp",
    ]


def build_namd_package(source: Path, output: Path) -> dict[str, object]:
    source = source.resolve()
    output = output.resolve()
    if not source.is_file():
        raise ValueError(f"No existe el archivo de entrada: {source}")
    if output.exists():
        raise ValueError(f"La carpeta de salida ya existe: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(dir=output.parent, prefix="charmmgui-") as temp_name:
        extracted = Path(temp_name)
        _safe_extract(source, extracted)
        namd_dir = _find_namd(extracted)

        dependency_report = collect_dependencies(namd_dir, mode="copy")
        dependencies = dependency_report["dependencies"]
        for dependency in dependencies:
            dependency["source"] = str(
                Path(dependency["source"]).relative_to(namd_dir.parent)
            ).replace("\\", "/")
        renamed_in = _replace_namd_command(namd_dir)
        separated_stages = _separate_minimization_and_thermalization(namd_dir)
        readmes = _split_namd_readme(namd_dir)

        shutil.copytree(namd_dir, output)

    return {
        "source": str(source),
        "namd_output": str(output),
        "dependency_reference_count": len(dependencies),
        "dependency_file_count": len(
            {dependency["destination"] for dependency in dependencies}
        ),
        "dependencies": dependencies,
        "namd_command_replaced_in": renamed_in,
        "stages_created_or_updated": separated_stages,
        "readmes_created": readmes,
        "simulations_executed": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Prepara una carpeta NAMD autosuficiente desde un paquete de CHARMM-GUI."
    )
    parser.add_argument("archive", type=Path, help="Archivo .tgz de CHARMM-GUI")
    parser.add_argument(
        "--output", type=Path, default=Path("salida_namd") / "namd",
        help="Carpeta NAMD final (predeterminado: salida_namd/namd).",
    )
    args = parser.parse_args()
    try:
        report = build_namd_package(args.archive, args.output)
        print(json.dumps(report, ensure_ascii=False, indent=2))
    except (OSError, tarfile.TarError, EOFError, UnicodeError, ValueError) as exc:
        parser.exit(1, f"No se pudo preparar NAMD: {exc}\n")


if __name__ == "__main__":
    main()
