import argparse
import json
import re
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path


EXTERNAL_PATH = re.compile(
    r"(?P<path>(?:\.\.[\\/])+(?:[^\s\"'<>|;#]+))"
)
TEXT_SUFFIXES = {
    "",
    ".conf",
    ".inp",
    ".namd",
    ".prm",
    ".str",
    ".tcl",
    ".txt",
}


@dataclass(frozen=True)
class Dependency:
    referenced_by: str
    original_reference: str
    source: str
    destination: str
    new_reference: str


def _active_code(line: str) -> str:
    """Return the part before a Tcl/NAMD comment, preserving quoted # chars."""
    quote = None
    escaped = False
    for index, char in enumerate(line):
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if quote:
            if char == quote:
                quote = None
            continue
        if char in {"'", '"'}:
            quote = char
        elif char == "#":
            return line[:index]
    return line


def _read_text(path: Path) -> str | None:
    if path.suffix.lower() not in TEXT_SUFFIXES:
        return None
    try:
        data = path.read_bytes()
        if b"\x00" in data:
            return None
        return data.decode("utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return None


def _external_references(text: str) -> list[str]:
    found: list[str] = []
    for line in text.splitlines():
        for match in EXTERNAL_PATH.finditer(_active_code(line)):
            value = match.group("path").rstrip(",)")
            if value not in found:
                found.append(value)
    return found


def _inside(path: Path, directory: Path) -> bool:
    try:
        path.relative_to(directory)
        return True
    except ValueError:
        return False


def collect_dependencies(
    namd_dir: Path, *, mode: str = "copy", dry_run: bool = False
) -> dict[str, object]:
    namd_dir = namd_dir.resolve()
    package_root = namd_dir.parent
    if not namd_dir.is_dir():
        raise ValueError(f"No existe la carpeta NAMD: {namd_dir}")

    dependencies: list[Dependency] = []
    edits: dict[Path, str] = {}
    destination_sources: dict[Path, Path] = {}

    for config in sorted(path for path in namd_dir.rglob("*") if path.is_file()):
        text = _read_text(config)
        if text is None:
            continue
        updated = text
        for reference in _external_references(text):
            source = (config.parent / reference.replace("\\", "/")).resolve()
            if not _inside(source, package_root):
                raise ValueError(
                    f"La referencia {reference!r} en {config} sale del paquete."
                )
            if not source.exists():
                raise ValueError(
                    f"No existe la dependencia {reference!r} usada por {config}."
                )

            relative_source = source.relative_to(package_root)
            destination = (namd_dir / relative_source).resolve()
            if not _inside(destination, namd_dir):
                raise ValueError(f"Destino inseguro para {source}.")
            previous = destination_sources.get(destination)
            if previous is not None and previous != source:
                raise ValueError(
                    f"Dos dependencias distintas colisionan en {destination}."
                )
            destination_sources[destination] = source
            new_reference = Path(
                __import__("os").path.relpath(destination, config.parent)
            ).as_posix()
            updated = updated.replace(reference, new_reference)
            dependencies.append(
                Dependency(
                    referenced_by=str(config.relative_to(namd_dir)),
                    original_reference=reference,
                    source=str(source),
                    destination=str(destination.relative_to(namd_dir)),
                    new_reference=new_reference,
                )
            )
        if updated != text:
            edits[config] = updated

    if not dry_run:
        for destination, source in sorted(
            destination_sources.items(), key=lambda item: len(item[0].parts)
        ):
            if destination.exists() and destination.resolve() != source.resolve():
                raise ValueError(f"El destino ya existe: {destination}")
            destination.parent.mkdir(parents=True, exist_ok=True)
            if source.is_dir():
                if mode == "move":
                    shutil.move(str(source), str(destination))
                else:
                    shutil.copytree(source, destination)
            elif mode == "move":
                shutil.move(str(source), str(destination))
            else:
                shutil.copy2(source, destination)
        for config, updated in edits.items():
            config.write_text(updated, encoding="utf-8", newline="")

    unique = list(dict.fromkeys(dependencies))
    return {
        "namd_directory": str(namd_dir),
        "mode": mode,
        "dry_run": dry_run,
        "dependencies": [asdict(item) for item in unique],
        "modified_files": [str(path.relative_to(namd_dir)) for path in edits],
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Introduce en NAMD las dependencias referenciadas fuera de la carpeta "
            "y actualiza sus rutas."
        )
    )
    parser.add_argument("namd_directory", type=Path)
    parser.add_argument(
        "--mode", choices=("copy", "move"), default="copy",
        help="Copiar (predeterminado) o mover las dependencias originales.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Sólo informar las dependencias; no cambiar archivos.",
    )
    args = parser.parse_args()
    try:
        result = collect_dependencies(
            args.namd_directory, mode=args.mode, dry_run=args.dry_run
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (OSError, ValueError) as exc:
        parser.exit(1, f"No se pudieron recopilar las dependencias: {exc}\n")


if __name__ == "__main__":
    main()
