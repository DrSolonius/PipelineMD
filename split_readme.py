import argparse
import io
import json
import re
import tarfile
from pathlib import Path, PurePosixPath


def split_readme(text: str) -> tuple[str, str]:
    equilibration = list(
        re.finditer(
            r"(?im)^#\s*Running equilibration[^\n]*",
            text,
        )
    )
    production = list(
        re.finditer(
            r"(?im)^#\s*Running production[^\n]*",
            text,
        )
    )

    if (
        len(equilibration) != 1
        or len(production) != 1
        or equilibration[0].start() >= production[0].start()
    ):
        raise ValueError(
            "Marcadores de equilibración/producción ausentes o ambiguos."
        )

    shared = text[: equilibration[0].start()]

    equilibration_text = (
        shared
        + text[
            equilibration[0].start() : production[0].start()
        ]
    )
    production_text = shared + text[production[0].start() :]

    return equilibration_text, production_text


def process_archive(source: Path, output: Path) -> dict[str, object]:
    if source.resolve() == output.resolve():
        raise ValueError("La salida debe ser distinta del archivo original.")

    additions: list[tuple[tarfile.TarInfo, str, bytes]] = []
    original_names: set[str] = set()

    # Primera pasada: localizar y separar los README.
    with tarfile.open(source, "r|gz") as archive:
        for index, member in enumerate(archive):
            if index >= 100_000:
                raise ValueError("El paquete contiene demasiados elementos.")

            if member.offset_data + member.size > 1024**3:
                raise ValueError("El paquete supera el límite de inspección de 1 GB.")

            member_path = PurePosixPath(member.name)

            if member_path.is_absolute() or ".." in member_path.parts:
                raise ValueError("El paquete contiene rutas inseguras.")

            normalized_name = str(member_path)

            if normalized_name in original_names:
                raise ValueError("El paquete contiene rutas duplicadas.")

            original_names.add(normalized_name)

            is_namd_readme = (
                member.isfile()
                and len(member_path.parts) > 1
                and member_path.parts[-2].lower() == "namd"
                and (
                    member_path.name.lower() == "readme"
                    or member_path.name.lower().startswith("readme.")
                )
            )

            if not is_namd_readme:
                continue

            if member_path.name.lower().endswith(
                (".equilibracion", ".produccion")
            ):
                continue

            if member.size > 1024**2:
                raise ValueError(
                    f"README mayor de 1 MB: {member.name}"
                )

            stream = archive.extractfile(member)

            if stream is None:
                raise ValueError(
                    f"No se pudo leer el README: {member.name}"
                )

            with stream:
                readme_text = stream.read().decode("utf-8-sig")

            equilibration, production = split_readme(readme_text)

            additions.extend(
                [
                    (
                        member,
                        member.name + ".equilibracion",
                        equilibration.encode("utf-8"),
                    ),
                    (
                        member,
                        member.name + ".produccion",
                        production.encode("utf-8"),
                    ),
                ]
            )

    if not additions:
        raise ValueError(
            "No se encontró un README de NAMD dentro del .tgz."
        )

    new_names = [name for _, name, _ in additions]

    if len(set(new_names)) != len(new_names):
        raise ValueError("Se generaron nombres de salida duplicados.")

    if original_names.intersection(new_names):
        raise ValueError(
            "Los README separados ya existen; no se sobrescribirán."
        )

    output.parent.mkdir(parents=True, exist_ok=True)
    created = False

    try:
        # Segunda pasada: copiar el paquete y agregar los README separados.
        with output.open("xb") as raw_output:
            created = True

            with tarfile.open(
                fileobj=raw_output,
                mode="w:gz",
            ) as destination:
                with tarfile.open(source, "r|gz") as archive:
                    for member in archive:
                        if member.isfile():
                            stream = archive.extractfile(member)

                            if stream is None:
                                raise ValueError(
                                    f"No se pudo copiar {member.name}"
                                )

                            with stream:
                                destination.addfile(member, stream)
                        else:
                            destination.addfile(member)

                for original, name, data in additions:
                    item = tarfile.TarInfo(name)
                    item.size = len(data)
                    item.mode = 0o644
                    item.mtime = original.mtime
                    item.uid = original.uid
                    item.gid = original.gid
                    item.uname = original.uname
                    item.gname = original.gname

                    destination.addfile(item, io.BytesIO(data))

    except BaseException:
        if created:
            output.unlink(missing_ok=True)
        raise

    return {
        "archive": str(output.resolve()),
        "added": new_names,
        "original_readme_preserved": True,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Genera una copia de un .tgz de CHARMM-GUI con los "
            "README de NAMD separados."
        )
    )
    parser.add_argument("archive", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    try:
        result = process_archive(args.archive, args.output)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (OSError, tarfile.TarError, EOFError, UnicodeError, ValueError) as exc:
        parser.exit(1, f"No se pudo procesar el archivo: {exc}\n")


if __name__ == "__main__":
    main()