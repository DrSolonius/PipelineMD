#!/usr/bin/env python3
"""API local para cargar un paquete CHARMM-GUI y ejecutar el preparador."""

from __future__ import annotations

import json
import hashlib
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import uuid
import tarfile
from projects import ProjectStore
from execution_targets import resolve_target, prepare_remote, launch_command, stop_command, sync_outputs, ssh_command, ssh_env, PASSWORDS, CREDENTIALS, check_remote, remote_path
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote

HOST = "127.0.0.1"
PORT = 8765
MAX_UPLOAD = 1024**3
PROJECT = Path(__file__).parents[2]
PROJECT_STORE = ProjectStore(PROJECT)
UPLOADS = PROJECT / "uploads"
SIMULATIONS = PROJECT / "simulations"
RUNS: dict[str, dict] = {}
INGESTERS: dict[str, subprocess.Popen] = {}
DATA_DIR = PROJECT / "dashboard" / "public" / "data"
ACTIVE_DATA_POINTER = DATA_DIR / "active_dashboard.json"
SSH_CONNECTIONS_FILE = PROJECT / "config" / "ssh_connections.json"
SSH_CONNECTIONS_LOCK = threading.Lock()
RUN_START_LOCK = threading.Lock()
NAMD_LAUNCH = (
    "for d in /opt/apps/namd/3.0.1 "
    "/home/guido/rosalind-worker/software/namd/3.0.3 "
    "/home/guido/rosalind-worker/software/namd/3.0.3-multicore; do "
    "if [ -x \"$d/namd3\" ]; then export PATH=\"$d:$PATH\"; break; fi; done; "
    "if ! command -v namd3 >/dev/null 2>&1; then echo 'ERROR: no se encontró namd3'; exit 127; fi; "
    "exec csh README_preparacion"
)
NAMD_STOP = (
    "pids=''; "
    "for proc in /proc/[0-9]*; do "
    "[ \"$(readlink \"$proc/cwd\" 2>/dev/null)\" = \"$PWD\" ] || continue; "
    "[ \"$(cat \"$proc/comm\" 2>/dev/null)\" = namd3 ] || continue; "
    "pids=\"$pids ${proc##*/}\"; done; "
    "if [ -n \"$pids\" ]; then kill -TERM $pids 2>/dev/null || true; sleep 1; "
    "kill -KILL $pids 2>/dev/null || true; fi"
)


def finish_run(job_id: str, process: subprocess.Popen, log_handle) -> None:
    job = RUNS[job_id]
    while True:
        if job["target"]["kind"] == "ssh":
            try:
                sync_outputs(job["target"], Path(job["namdDir"]))
                job.pop("syncError", None)
            except (OSError, ValueError, tarfile.TarError, subprocess.TimeoutExpired) as exc:
                job["syncError"] = str(exc)
        try:
            code = process.wait(timeout=5)
            break
        except subprocess.TimeoutExpired:
            continue
    if job["target"]["kind"] == "ssh":
        try:
            sync_outputs(job["target"], Path(job["namdDir"]))
        except (OSError, ValueError, tarfile.TarError, subprocess.TimeoutExpired) as exc:
            job["syncError"] = str(exc)
    log_handle.close()
    job = RUNS[job_id]
    job["returncode"] = code
    job["status"] = "stopped" if job.get("stopRequested") else ("finished" if code == 0 else "error")


def ensure_ingester(namd_dir: Path) -> None:
    """Mantiene un solo lector de resultados por carpeta NAMD."""
    key = str(namd_dir.resolve())
    current = INGESTERS.get(key)
    if current is not None and current.poll() is None:
        return
    for old_key, old_process in list(INGESTERS.items()):
        if old_process.poll() is None:
            old_process.terminate()
        INGESTERS.pop(old_key, None)
    source_id = hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]
    output = DATA_DIR / f"dashboard_{source_id}.json"
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    pointer_tmp = ACTIVE_DATA_POINTER.with_suffix(".json.tmp")
    pointer_tmp.write_text(json.dumps({"dataFile": output.name, "namdDir": key}), encoding="utf-8")
    pointer_tmp.replace(ACTIVE_DATA_POINTER)
    INGESTERS[key] = subprocess.Popen(
        [sys.executable, str(Path(__file__).with_name("ingest_namd.py")), "--namd-dir", key, "--output", str(output), "--watch", "--interval", "1"],
        cwd=PROJECT / "dashboard",
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def selected_dashboard_file() -> Path:
    try:
        pointer = json.loads(ACTIVE_DATA_POINTER.read_text(encoding="utf-8"))
        candidate = (DATA_DIR / Path(pointer["dataFile"]).name).resolve()
        candidate.relative_to(DATA_DIR.resolve())
        return candidate
    except (OSError, KeyError, ValueError, json.JSONDecodeError):
        return DATA_DIR / "dashboard.json"


def restore_active_ingester() -> None:
    """Reanuda el lector de la simulación seleccionada después de reiniciar la API."""
    try:
        pointer = json.loads(ACTIVE_DATA_POINTER.read_text(encoding="utf-8"))
        namd_dir = Path(pointer["namdDir"]).resolve()
        known = any(str(namd_dir) == simulation.get("namdDir")
                    for project in PROJECT_STORE.list() for simulation in project["simulations"])
        if known and namd_dir.is_dir():
            ensure_ingester(namd_dir)
    except (OSError, KeyError, ValueError, json.JSONDecodeError):
        return


def archive_previous_outputs(namd_dir: Path) -> Path | None:
    runtime_patterns = (
        "step6*.out", "step6*.coor", "step6*.vel", "step6*.xsc", "step6*.dcd",
        "step6*.coor.old", "step6*.vel.old", "step6*.xsc.old",
        "step6*.coor.BAK", "step6*.vel.BAK", "step6*.xsc.BAK",
        "step6*.colvars.traj", "step6*.colvars.state",
    )
    previous_files = list(dict.fromkeys(
        path for pattern in runtime_patterns for path in namd_dir.glob(pattern)
    ))
    if not previous_files:
        return None
    history = namd_dir / "run_history" / datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    history.mkdir(parents=True, exist_ok=False)
    for previous in previous_files:
        shutil.move(str(previous), history / previous.name)
    source_id = hashlib.sha1(str(namd_dir.resolve()).encode("utf-8")).hexdigest()[:10]
    (DATA_DIR / f"structural_state_{source_id}.npz").unlink(missing_ok=True)
    return history


def read_output_tail(path: Path, limit: int = 400) -> str:
    try:
        with path.open("rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            handle.seek(max(0, size - max(limit, 65_536)))
            return handle.read().decode("utf-8", errors="replace")[-limit:]
    except OSError:
        return ""


def remember_completed_tail(job: dict, path: Path) -> None:
    emitted = job.setdefault("completedTailFiles", set())
    key = str(path)
    if key in emitted:
        return
    emitted.add(key)
    job.setdefault("completedTails", []).append({"file": path.name, "tail": read_output_tail(path)})


def output_progress(job: dict) -> tuple[str, str | None, bool, list[dict]]:
    """Sigue el .out activo y conserva el tail final de cada etapa."""
    outputs = list(Path(job["namdDir"]).glob("step6*.out"))
    if not outputs:
        return "", None, False, job.setdefault("completedTails", [])
    def stage_number(path: Path) -> int:
        match = re.match(r"step6\.(\d+)", path.name)
        return int(match.group(1)) if match else -1

    # README_preparacion corre step6.x en orden y los .out anteriores se archivan
    # al comenzar. El índice mayor es por tanto la etapa entrante, incluso si su
    # archivo todavía está vacío o comparte resolución temporal con el anterior.
    active = max(outputs, key=stage_number)
    for completed in outputs:
        if completed != active:
            remember_completed_tail(job, completed)
    active_key = str(active)
    if job.get("tailFile") != active_key:
        previous = Path(job["tailFile"]) if job.get("tailFile") else None
        if previous is not None and previous.exists():
            # README_preparacion es secuencial: si apareció otro .out, la etapa
            # anterior ya devolvió el control al script.
            remember_completed_tail(job, previous)
        job["tailFile"] = active_key
        job["stepsVisible"] = False
    try:
        with active.open("rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            handle.seek(max(0, size - 65_536))
            recent = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return "", active.name, bool(job.get("stepsVisible")), job.setdefault("completedTails", [])
    if "ENERGY:" in recent:
        job["stepsVisible"] = True
    has_steps = bool(job.get("stepsVisible"))
    has_error = "FATAL ERROR:" in recent or "\nERROR:" in recent or recent.startswith("ERROR:")
    if "End of program" in recent or (job.get("status") not in {"running", "stopping"}):
        remember_completed_tail(job, active)
    show_tail = not has_steps or has_error or job.get("status") == "error"
    return (recent[-400:] if show_tail else ""), active.name, has_steps, job.setdefault("completedTails", [])


def safe_name(value: str) -> str:
    name = Path(unquote(value)).name
    name = re.sub(r"(?i)\.(tar\.gz|tgz)$", "", name)
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._-")
    return name[:80] or f"sistema_{uuid.uuid4().hex[:8]}"


def load_ssh_connections() -> list[dict]:
    with SSH_CONNECTIONS_LOCK:
        try:
            saved = json.loads(SSH_CONNECTIONS_FILE.read_text(encoding="utf-8"))
            return saved if isinstance(saved, list) else []
        except (OSError, json.JSONDecodeError):
            return []


def save_ssh_connections(connections: list[dict]) -> None:
    with SSH_CONNECTIONS_LOCK:
        SSH_CONNECTIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
        temporary = SSH_CONNECTIONS_FILE.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(connections, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(SSH_CONNECTIONS_FILE)


def validate_ssh_connection(raw: dict, existing: dict | None = None) -> dict:
    name = str(raw.get("name", "")).strip()
    host = str(raw.get("host", "")).strip()
    username = str(raw.get("username", "")).strip()
    if not name or len(name) > 80:
        raise ValueError("El nombre es obligatorio y admite hasta 80 caracteres.")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,252}", host):
        raise ValueError("Host o dirección IP inválida.")
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", username):
        raise ValueError("Usuario SSH inválido.")
    try:
        port = int(raw.get("port", 22))
    except (TypeError, ValueError) as exc:
        raise ValueError("El puerto SSH debe ser numérico.") from exc
    if not 1 <= port <= 65535:
        raise ValueError("El puerto SSH debe estar entre 1 y 65535.")
    auth_type = str(raw.get("authType", "agent"))
    if auth_type not in {"agent", "key", "password"}:
        raise ValueError("Método de autenticación inválido.")
    key_path = str(raw.get("keyPath", "")).strip()
    if auth_type == "key" and not key_path:
        raise ValueError("Selecciona el archivo de clave privada.")
    scheduler = str(raw.get("scheduler", "none"))
    if scheduler not in {"none", "slurm", "pbs"}:
        raise ValueError("Scheduler remoto inválido.")
    remote_workdir = str(raw.get("remoteWorkdir", "~/md-pipeline")).strip()
    if not remote_workdir or len(remote_workdir) > 500 or any(char in remote_workdir for char in "\r\n\0"):
        raise ValueError("Directorio remoto inválido.")
    setup_command = str(raw.get("setupCommand", "")).strip()
    if len(setup_command) > 1000 or any(c in setup_command for c in "\r\n\0"):
        raise ValueError("El comando admite una sola línea y hasta 1000 caracteres.")
    now = datetime.now().astimezone().isoformat()
    connection_changed = bool(existing) and any(
        (existing or {}).get(field) != value for field, value in {
            "host": host, "port": port, "username": username,
            "authType": auth_type, "keyPath": key_path if auth_type == "key" else "",
            "remoteWorkdir": remote_workdir, "setupCommand": setup_command,
        }.items()
    )
    return {
        "id": (existing or {}).get("id") or uuid.uuid4().hex[:12],
        "name": name, "host": host, "port": port, "username": username,
        "authType": auth_type, "keyPath": key_path if auth_type == "key" else "",
        "remoteWorkdir": remote_workdir, "scheduler": scheduler,
        "setupCommand": setup_command,
        "status": "untested" if connection_changed else (existing or {}).get("status", "untested"),
        "lastTestedAt": None if connection_changed else (existing or {}).get("lastTestedAt"),
        "lastMessage": "La conexión cambió; vuelve a probarla." if connection_changed else (existing or {}).get("lastMessage", "Todavía no se ha probado."),
        "createdAt": (existing or {}).get("createdAt", now), "updatedAt": now,
    }


def test_ssh_connection(connection: dict) -> tuple[bool, str]:
    try:
        command = ssh_command(connection, "printf MD_PIPELINE_SSH_OK")
        # First-use enrollment matches the existing SSH test behavior.
        command = ["StrictHostKeyChecking=accept-new" if item == "StrictHostKeyChecking=yes" else item for item in command]
        environment = ssh_env(connection)
    except ValueError as exc:
        return False, str(exc)
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=12, check=False, env=environment, stdin=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"No se pudo completar la conexión: {exc}"
    output = (result.stdout or result.stderr or "").strip()
    if result.returncode == 0 and "MD_PIPELINE_SSH_OK" in result.stdout:
        return True, "Conexión SSH correcta."
    return False, output[-500:] or f"ssh terminó con código {result.returncode}."


class Handler(BaseHTTPRequestHandler):
    server_version = "MDPipelineUpload/1.0"

    def _headers(self, status: int, content_type: str = "application/json; charset=utf-8") -> None:
        origin = self.headers.get("Origin", "")
        allowed_origin = origin if origin in {"http://localhost:3000", "http://127.0.0.1:3000"} else "http://localhost:3000"
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Access-Control-Allow-Origin", allowed_origin)
        self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Filename, X-Project-Id")
        self.send_header("Access-Control-Allow-Methods", "POST, DELETE, OPTIONS, GET")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def _json(self, status: int, value: dict) -> None:
        try:
            self._headers(status)
            self.wfile.write(json.dumps(value, ensure_ascii=False).encode("utf-8"))
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            pass

    def do_OPTIONS(self) -> None:
        self._headers(204)

    def _serve_events(self) -> None:
        origin = self.headers.get("Origin", "")
        allowed_origin = origin if origin in {"http://localhost:3000", "http://127.0.0.1:3000"} else "http://localhost:3000"
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", allowed_origin)
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        last_signature = None
        event_id = 0
        heartbeat_at = time.monotonic()
        try:
            while True:
                data_file = selected_dashboard_file()
                try:
                    stat = data_file.stat()
                    signature = (str(data_file), stat.st_mtime_ns, stat.st_size)
                    if signature != last_signature:
                        payload = data_file.read_text(encoding="utf-8")
                        json.loads(payload)  # no emitir un archivo capturado a medio reemplazo
                        event_id += 1
                        frame = f"id: {event_id}\nevent: dashboard\ndata: {payload.replace(chr(10), '')}\n\n"
                        self.wfile.write(frame.encode("utf-8"))
                        self.wfile.flush()
                        last_signature = signature
                        heartbeat_at = time.monotonic()
                except (OSError, json.JSONDecodeError):
                    pass
                if time.monotonic() - heartbeat_at >= 10:
                    self.wfile.write(b": heartbeat\n\n")
                    self.wfile.flush()
                    heartbeat_at = time.monotonic()
                time.sleep(0.25)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            return

    def do_GET(self) -> None:
        if self.path == "/projects":
            try:
                active = None
                if ACTIVE_DATA_POINTER.exists():
                    active = json.loads(ACTIVE_DATA_POINTER.read_text(encoding="utf-8")).get("namdDir")
                self._json(200, {"projects": PROJECT_STORE.list(), "activeNamdDir": active})
            except (OSError, ValueError) as exc:
                self._json(500, {"error": str(exc)})
        elif self.path == "/health":
            self._json(200, {"status": "ok"})
        elif self.path.split("?", 1)[0] == "/events":
            self._serve_events()
        elif self.path.split("?", 1)[0] == "/dashboard-data":
            data_file = selected_dashboard_file()
            try:
                payload = data_file.read_bytes()
                self._headers(200)
                self.wfile.write(payload)
            except OSError as exc:
                self._json(404, {"error": f"Datos NAMD no disponibles: {exc}"})
        elif self.path.split("?", 1)[0] == "/execution-targets":
            self._json(200, {"targets": [
                {"id": "local", "name": "Local (WSL)", "available": shutil.which("wsl.exe") is not None if sys.platform == "win32" else True,
                 "detail": "NAMD y csh instalados en el equipo local"},
                *[{"id": c["id"], "name": c["name"], "available": c.get("status") == "online" and c.get("scheduler") in {"none", "slurm"},
                   "detail": f"{c['username']}@{c['host']} · {c['remoteWorkdir']} · " + ("SLURM" if c.get("scheduler") == "slurm" else "PBS pendiente de soporte" if c.get("scheduler") == "pbs" else c.get("status", "untested"))}
                  for c in load_ssh_connections()]]})
        elif self.path.split("?", 1)[0] == "/ssh-connections":
            self._json(200, {"connections": load_ssh_connections()})
        elif self.path.startswith("/run-status/"):
            job_id = self.path.split("?", 1)[0].rsplit("/", 1)[-1]
            job = RUNS.get(job_id)
            if not job:
                self._json(404, {"error": "Ejecución inexistente"})
                return
            try:
                lines = Path(job["logFile"]).read_text(encoding="utf-8", errors="replace").splitlines()[-80:]
            except OSError:
                lines = []
            tail, active_output, steps_visible, completed_tails = output_progress(job)
            self._json(200, {
                "jobId": job_id,
                "target": {k: job["target"].get(k) for k in ("id", "name", "kind", "runDir")},
                "syncError": job.get("syncError"),
                "status": job["status"],
                "returncode": job.get("returncode"),
                "lines": lines,
                "tail": tail,
                "activeOutput": active_output,
                "stepsVisible": steps_visible,
                "completedTails": completed_tails,
            })
        else:
            self._json(404, {"error": "Ruta inexistente"})

    def do_POST(self) -> None:
        if self.path == "/run-preparation":
            with RUN_START_LOCK:
                self._post()
        else:
            self._post()

    def _post(self) -> None:
        if self.path == "/choose-project-directory":
            try:
                import tkinter as tk
                from tkinter import filedialog
                window = tk.Tk()
                window.withdraw()
                window.attributes("-topmost", True)
                directory = filedialog.askdirectory(
                    parent=window, title="Selecciona dónde guardar el proyecto", mustexist=True)
                window.destroy()
                self._json(200, {"directory": directory or None, "cancelled": not bool(directory)})
            except Exception as exc:
                self._json(500, {"error": f"No se pudo abrir el selector de carpetas: {exc}"})
            return
        if self.path in {"/projects", "/update-project", "/select-simulation"}:
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 65536:
                    raise ValueError("Solicitud inválida.")
                request = json.loads(self.rfile.read(length))
                if not isinstance(request, dict):
                    raise ValueError("Solicitud inválida.")
                if self.path == "/projects":
                    project = PROJECT_STORE.create(str(request.get("name", "")),
                        str(request.get("description", "")), str(request.get("baseDirectory", "")))
                    self._json(201, {"project": project})
                elif self.path == "/update-project":
                    project = PROJECT_STORE.update(str(request.get("projectId", "")), str(request.get("name", "")), str(request.get("description", "")))
                    self._json(200, {"project": project})
                else:
                    simulation = PROJECT_STORE.simulation(str(request.get("projectId", "")), str(request.get("simulationId", "")))
                    directory = Path(simulation["namdDir"]).resolve()
                    if not (directory / "README_preparacion").is_file():
                        raise ValueError("Los archivos de la simulación ya no están disponibles.")
                    ensure_ingester(directory)
                    self._json(200, {"simulation": simulation})
            except (OSError, ValueError) as exc:
                self._json(400, {"error": str(exc)})
            return
        if self.path == "/ssh-connections":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                request = json.loads(self.rfile.read(length) or b"{}")
                connections = load_ssh_connections()
                requested_id = str(request.get("id", ""))
                existing = next((item for item in connections if item["id"] == requested_id), None)
                connection = validate_ssh_connection(request, existing)
                credentials_changed = bool(existing) and any(
                    existing.get(field) != connection.get(field)
                    for field in ("host", "port", "username", "authType", "keyPath")
                )
                if existing:
                    connections = [connection if item["id"] == requested_id else item for item in connections]
                else:
                    connections.append(connection)
                if credentials_changed:
                    PASSWORDS.pop(connection["id"], None)
                    CREDENTIALS.delete(connection["id"])
                save_ssh_connections(connections)
                self._json(200 if existing else 201, {"connection": connection})
            except (ValueError, json.JSONDecodeError) as exc:
                self._json(400, {"error": str(exc)})
            return
        if self.path.startswith("/ssh-environment/"):
            connection = next((c for c in load_ssh_connections() if c["id"] == self.path.rsplit("/", 1)[-1]), None)
            try:
                if connection is None:
                    raise ValueError("Conexión inexistente.")
                check_remote(connection)
                self._json(200, {"message": "Disponibles: csh, tar, setsid y namd3."})
            except (ValueError, OSError, subprocess.TimeoutExpired) as exc:
                self._json(422, {"error": str(exc)})
            return
        if self.path.startswith("/ssh-test/"):
            connection_id = self.path.rsplit("/", 1)[-1]
            connections = load_ssh_connections()
            connection = next((item for item in connections if item["id"] == connection_id), None)
            if connection is None:
                self._json(404, {"error": "Conexión SSH inexistente."})
                return
            if connection["authType"] == "password":
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= 16384:
                        raise ValueError("Introduce la contraseña para conectar.")
                    payload = json.loads(self.rfile.read(length))
                    password = payload.get("password")
                    if not isinstance(password, str) or not password or len(password) > 2048 or any(c in password for c in "\r\n\0"):
                        raise ValueError("Contraseña inválida.")
                    PASSWORDS[connection_id] = password
                except (ValueError, AttributeError) as exc:
                    self._json(400, {"error": str(exc)})
                    return
            success, message = test_ssh_connection(connection)
            if not success:
                PASSWORDS.pop(connection_id, None)
            elif connection["authType"] == "password":
                CREDENTIALS.save(connection_id, PASSWORDS[connection_id])
            connection["status"] = "online" if success else "error"
            connection["lastTestedAt"] = datetime.now().astimezone().isoformat()
            connection["lastMessage"] = message
            save_ssh_connections(connections)
            self._json(200 if success else 422, {"connection": connection, "success": success})
            return
        if self.path == "/stop-preparation":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                request = json.loads(self.rfile.read(length) or b"{}")
                job_id = str(request.get("jobId", ""))
                job = RUNS.get(job_id)
                if not job:
                    self._json(404, {"error": "Ejecución inexistente."})
                    return
                if job["status"] not in {"running", "stopping"}:
                    self._json(200, {"jobId": job_id, "status": job["status"]})
                    return
                job["stopRequested"] = True
                job["status"] = "stopping"
                process: subprocess.Popen = job["process"]
                namd_dir = Path(job["namdDir"])
                try:
                    subprocess.run(
                        stop_command(job["target"], namd_dir, NAMD_STOP), env=ssh_env(job["target"]),
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=10,
                        check=True, cwd=namd_dir,
                    )
                except (subprocess.TimeoutExpired, subprocess.CalledProcessError) as exc:
                    job["stopRequested"] = False
                    job["status"] = "running"
                    raise ValueError("No se pudo confirmar STOP en el destino. Vuelve a intentarlo.") from exc
                if process.poll() is None:
                    process.terminate()
                self._json(200, {"jobId": job_id, "status": "stopping"})
            except (OSError, ValueError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
                self._json(400, {"error": str(exc)})
            return
        if self.path == "/run-preparation":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                request = json.loads(self.rfile.read(length) or b"{}")
                simulation = PROJECT_STORE.simulation(str(request.get("projectId", "")), str(request.get("simulationId", "")))
                namd_dir = Path(simulation["namdDir"]).resolve()
                readme = namd_dir / "README_preparacion"
                if not readme.is_file():
                    raise ValueError("No existe README_preparacion en la simulación activa.")
                active = next((job for job in RUNS.values() if job["namdDir"] == str(namd_dir) and job["status"] in {"running", "stopping"}), None)
                if active:
                    self._json(409, {"error": "La preparación ya está en ejecución."})
                    return
                target = resolve_target(str(request.get("targetId", "local")), load_ssh_connections())
                job_id = uuid.uuid4().hex[:12]
                if target["kind"] == "ssh":
                    target["runDir"] = prepare_remote(target, namd_dir, job_id)
                else:
                    probe = subprocess.run(launch_command(target, namd_dir,
                        NAMD_LAUNCH.replace("exec csh README_preparacion", "command -v csh >/dev/null")),
                        cwd=namd_dir, capture_output=True, timeout=20)
                    if probe.returncode:
                        raise ValueError("El equipo local requiere NAMD y csh disponibles en WSL/Linux.")
                history = archive_previous_outputs(namd_dir)
                ensure_ingester(namd_dir)
                log_file = namd_dir / "preparation_runner.log"
                log_handle = log_file.open("w", encoding="utf-8")
                try:
                    process = subprocess.Popen(
                        launch_command(target, namd_dir, NAMD_LAUNCH), env=ssh_env(target),
                        stdin=subprocess.DEVNULL,
                        stdout=log_handle, stderr=subprocess.STDOUT, cwd=namd_dir,
                    )
                except OSError:
                    log_handle.close()
                    raise
                RUNS[job_id] = {"projectId": request["projectId"], "simulationId": simulation["id"], "target": target, "status": "running", "namdDir": str(namd_dir), "logFile": str(log_file), "pid": process.pid, "process": process, "stopRequested": False, "historyDir": str(history) if history else None}
                threading.Thread(target=finish_run, args=(job_id, process, log_handle), daemon=True).start()
                self._json(202, {"jobId": job_id, "status": "running", "logFile": str(log_file), "targetName": target["name"], "remoteDir": target.get("runDir")})
            except (OSError, ValueError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
                self._json(400, {"error": str(exc)})
            return
        if self.path != "/upload":
            self._json(404, {"error": "Ruta inexistente"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        filename = self.headers.get("X-Filename", "")
        if not filename.lower().endswith((".tgz", ".tar.gz")):
            self._json(400, {"error": "Selecciona un archivo .tgz o .tar.gz de CHARMM-GUI."})
            return
        if length <= 0 or length > MAX_UPLOAD:
            self._json(413, {"error": "El archivo está vacío o supera 1 GB."})
            return

        system = safe_name(filename)
        try:
            project_id = self.headers.get("X-Project-Id", "")
            simulation = PROJECT_STORE.allocate(project_id, Path(unquote(filename)).name, system)
        except (OSError, ValueError) as exc:
            self._json(400, {"error": str(exc)})
            return
        destination = Path(simulation["archivePath"])
        destination.parent.mkdir(parents=True, exist_ok=False)
        Path(simulation["resultsDir"]).mkdir(parents=True, exist_ok=False)
        output = Path(simulation["namdDir"])
        try:
            remaining = length
            with destination.open("xb") as handle:
                while remaining:
                    chunk = self.rfile.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise OSError("La carga terminó antes de completar el archivo.")
                    handle.write(chunk)
                    remaining -= len(chunk)
            if output.exists():
                self._json(409, {"error": "Ya existe el directorio de esta simulación. Vuelve a intentar la carga."})
                return
            result = subprocess.run(
                [sys.executable, str(PROJECT / "main.py"), str(destination), "--output", str(output)],
                cwd=PROJECT, capture_output=True, text=True, timeout=300, check=False,
            )
            if result.returncode != 0:
                shutil.rmtree(Path(simulation["directory"]), ignore_errors=True)
                self._json(422, {"error": (result.stderr or result.stdout or "No se pudo preparar el paquete.").strip()})
                return
            report = json.loads(result.stdout)
            PROJECT_STORE.register(project_id, simulation)
            ensure_ingester(output)
            self._json(201, {"status": "prepared", "projectId": project_id, "simulation": simulation, "system": system, "namdDir": str(output), "report": report})
        except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
            shutil.rmtree(Path(simulation["directory"]), ignore_errors=True)
            self._json(500, {"error": str(exc)})

    def do_DELETE(self) -> None:
        if not self.path.startswith("/ssh-connections/"):
            self._json(404, {"error": "Ruta inexistente"})
            return
        connection_id = self.path.rsplit("/", 1)[-1]
        connections = load_ssh_connections()
        remaining = [item for item in connections if item["id"] != connection_id]
        if len(remaining) == len(connections):
            self._json(404, {"error": "Conexión SSH inexistente."})
            return
        PASSWORDS.pop(connection_id, None)
        CREDENTIALS.delete(connection_id)
        save_ssh_connections(remaining)
        self._json(200, {"deleted": connection_id})

    def log_message(self, format: str, *args: object) -> None:
        print(f"[upload] {self.address_string()} {format % args}", flush=True)


class LocalServer(ThreadingHTTPServer):
    allow_reuse_address = False

    def server_bind(self):
        if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


if __name__ == "__main__":
    restore_active_ingester()
    print(f"Upload API en http://{HOST}:{PORT}", flush=True)
    LocalServer((HOST, PORT), Handler).serve_forever()
