"""Local/SSH execution commands and isolated remote workspaces."""
from __future__ import annotations

import io
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import tarfile
import tempfile
import sys
from encrypted_credentials import CredentialStore

PASSWORDS: dict[str, str] = {}
CREDENTIALS = CredentialStore(
    Path(__file__).parents[2] / "config" / "ssh_connections.sqlite3",
    Path(__file__).parents[2] / "config" / "ssh_master.key",
)

def ssh_env(connection: dict) -> dict | None:
    if connection.get("authType") != "password":
        return None
    password = PASSWORDS.get(connection["id"]) or CREDENTIALS.load(connection["id"])
    if not password:
        raise ValueError("Introduce la contraseña y pulsa Conectar en SSH. La sesión se pierde al reiniciar la API.")
    helper = Path(__file__).with_name("ssh_askpass.exe" if os.name == "nt" else "ssh_askpass.py").resolve()
    if not helper.is_file():
        raise ValueError("Falta el helper SSH askpass.")
    return {**os.environ, "SSH_ASKPASS": str(helper), "SSH_ASKPASS_REQUIRE": "force", "DISPLAY": "md-pipeline", "MD_SSH_PASSWORD": password}



def local_command(directory: Path, script: str) -> list[str]:
    if os.name == "nt":
        return ["wsl.exe", "--cd", str(directory), "--exec", "sh", "-c", script]
    return ["sh", "-c", script]


def ssh_command(connection: dict, script: str) -> list[str]:
    command = ["ssh.exe" if os.name == "nt" else "ssh", "-o", "BatchMode=yes",
               "-o", "ConnectTimeout=8", "-o", "ServerAliveInterval=15",
               "-o", "ServerAliveCountMax=3", "-o", "StrictHostKeyChecking=yes",
               "-p", str(connection["port"])]
    if connection["authType"] == "password":
        command[2] = "BatchMode=no"
        command += ["-o", "PreferredAuthentications=password", "-o", "NumberOfPasswordPrompts=1"]
    if connection["authType"] == "key":
        key = Path(connection["keyPath"]).expanduser()
        if not key.is_file():
            raise ValueError("No existe el archivo de clave SSH.")
        command += ["-i", str(key), "-o", "IdentitiesOnly=yes"]
    return command + [f"{connection['username']}@{connection['host']}", script]


def remote_path(path: str) -> str:
    if path.startswith("~/"):
        return '"$HOME"/' + shlex.quote(path[2:])
    if not path.startswith("/"):
        raise ValueError("El directorio SSH debe ser absoluto o comenzar con ~/.")
    return shlex.quote(path)


def resolve_target(target_id: str, connections: list[dict]) -> dict:
    if target_id == "local":
        if os.name == "nt" and not shutil.which("wsl.exe"):
            raise ValueError("La ejecución local requiere WSL con NAMD y csh.")
        return {"id": "local", "name": "Local (WSL)" if os.name == "nt" else "Local", "kind": "local"}
    connection = next((dict(c) for c in connections if c["id"] == target_id), None)
    if connection is None:
        raise ValueError("El destino SSH ya no existe. Actualiza los destinos.")
    if connection.get("scheduler") not in {"none", "slurm"}:
        raise ValueError("PBS aún no está soportado.")
    if connection.get("status") != "online":
        raise ValueError("Prueba la conexión en el módulo SSH antes de seleccionarla.")
    connection["kind"] = "ssh"
    remote_path(connection["remoteWorkdir"])
    return connection


def remote_environment(connection: dict) -> str:
    setup = connection.get("setupCommand", "").strip()
    return setup + ("; " if setup else "")


def check_remote(connection: dict) -> None:
    scheduler_tools = " sbatch squeue scancel sacct" if connection.get("scheduler") == "slurm" else ""
    script = remote_environment(connection) + (
        "missing=0; for tool in csh tar setsid namd3" + scheduler_tools + "; do "
        "if command -v \"$tool\" >/dev/null 2>&1; then "
        "printf 'OK: %s\\n' \"$tool\"; else "
        "printf 'FALTA: %s\\n' \"$tool\"; missing=1; fi; done; exit \"$missing\""
    )
    check = subprocess.run(ssh_command(connection, "bash -lc " + shlex.quote(script)),
        capture_output=True, timeout=20, env=ssh_env(connection), stdin=subprocess.DEVNULL)
    if check.returncode:
        details = (check.stdout + check.stderr).decode("utf-8", errors="replace").strip()
        if "FALTA:" in details:
            raise ValueError("Requisitos del servidor: " + details +
                "\nRevisa el comando para cargar NAMD en Editar SSH.")
        raise ValueError("No se pudo comprobar el entorno SSH: " + (details[-1000:] or str(check.returncode)))


def prepare_remote(connection: dict, directory: Path, job_id: str, resume_stages: set[str] | None = None) -> str:
    remote = connection["remoteWorkdir"].rstrip("/") + "/runs/" + job_id
    prefix = remote_path(remote)
    # Unique job directory: existing remote results are never replaced.
    check_remote(connection)
    with tempfile.TemporaryFile() as archive:
        with tarfile.open(fileobj=archive, mode="w:gz") as tar:
            for path in directory.rglob("*"):
                relative = path.relative_to(directory)
                if path.is_symlink() or "run_history" in relative.parts:
                    continue
                # Copy the complete prepared NAMD folder. Only artifacts from an
                # earlier run are omitted; inputs with uncommon extensions must
                # also reach the remote machine.
                runtime = path.suffix in {".out", ".dcd", ".coor", ".vel", ".xsc", ".xst"} or \
                    ".restart." in path.name or path.name.endswith((".BAK", ".old", ".sync"))
                resume_artifact = bool(resume_stages) and any(
                    path.name.startswith(stage + ".") for stage in resume_stages
                ) and path.suffix in {".coor", ".vel", ".xsc"}
                if path.is_file() and (not runtime or resume_artifact) and path.name != "preparation_runner.log":
                    tar.add(path, arcname=relative.as_posix(), recursive=False)
        archive.seek(0)
        result = subprocess.run(ssh_command(connection,
            f"mkdir -p {remote_path(connection['remoteWorkdir'].rstrip('/') + '/runs')} && mkdir {prefix} && tar -xzf - -C {prefix} && test -f {prefix}/README_preparacion"),
            stdin=archive, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=300, env=ssh_env(connection))
    if result.returncode:
        raise ValueError("No se pudo transferir la simulación: " + result.stderr.decode(errors="replace")[-500:])
    return remote


def launch_command(target: dict, directory: Path, script: str) -> list[str]:
    if target["kind"] == "local":
        return local_command(directory, script)
    if target.get("scheduler") == "slurm":
        workdir = remote_path(target["runDir"])
        submit_script = "submit_reanudar.slurm" if "README_reanudar" in script else "submit_preparacion.slurm"
        script = (
            f"cd {workdir} && test -f {submit_script} && "
            f"job=$(sbatch --parsable --chdir={workdir} {submit_script}); "
            "job=${job%%;*}; case \"$job\" in ''|*[!0-9]*) echo 'ERROR: sbatch no devolvió un Job ID'; exit 1;; esac; "
            "echo \"$job\" > .pipeline.slurm_job; echo \"SLURM_JOB_ID=$job\"; "
            "while squeue -h -j \"$job\" | grep -q .; do sleep 5; done; "
            "state=$(sacct -n -X -j \"$job\" --format=State -P | head -n 1 | cut -d'|' -f1); "
            "echo \"SLURM_STATE=$state\"; case \"$state\" in COMPLETED*) exit 0;; *) exit 1;; esac"
        )
    else:
        readme = "README_reanudar" if "README_reanudar" in script else "README_preparacion"
        script = remote_environment(target) + f"cd {remote_path(target['runDir'])} && " + \
            f"setsid sh -c 'echo $$ > .pipeline.pid; exec csh {readme}' & wait $!"
    return ssh_command(target, "bash -lc " + shlex.quote(script))



def stop_command(target: dict, directory: Path, script: str) -> list[str]:
    if target["kind"] == "local":
        return local_command(directory, script)
    if target.get("scheduler") == "slurm":
        return ssh_command(target, f"cd {remote_path(target['runDir'])} && "
            'job=$(cat .pipeline.slurm_job) && case "$job" in ""|*[!0-9]*) exit 1;; esac; '
            'scancel "$job"')
    return ssh_command(target, f"cd {remote_path(target['runDir'])} && "
        'pid=$(cat .pipeline.pid) && case "$pid" in ""|*[!0-9]*) exit 1;; esac; '
        '/bin/kill -TERM -- -"$pid"')


def sync_outputs(target: dict, directory: Path) -> None:
    """Copy outputs and the current coordinate snapshot atomically."""
    result = subprocess.run(ssh_command(target,
        f"cd {remote_path(target['runDir'])} && "
        "tar --ignore-failed-read -czf - -- step6*.out step6*.coor step6*.vel step6*.xsc step6*.coor.old step6*.xsc.old"),
        capture_output=True, timeout=25, env=ssh_env(target), stdin=subprocess.DEVNULL)
    if not result.stdout:
        return
    with tarfile.open(fileobj=io.BytesIO(result.stdout), mode="r:gz") as archive:
        for member in archive:
            if not member.isfile() or not re.fullmatch(
                r"step6\.\d+_[A-Za-z0-9_]+(?:\.restart)?\.(?:out|coor|vel|xsc|coor\.old|xsc\.old)",
                member.name,
            ):
                continue
            source = archive.extractfile(member)
            if source is not None:
                temporary = directory / (member.name + ".sync")
                with temporary.open("wb") as output:
                    shutil.copyfileobj(source, output)
                temporary.replace(directory / member.name)
