"""Persistent project/simulation catalog; legacy directories stay in place."""
from __future__ import annotations

from datetime import datetime
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import re
import shutil
import sqlite3
import threading
import uuid

from project_state import read_config, refresh_simulation_state, state_from_config, write_config


class ProjectStore:
    def __init__(self, root: Path):
        self.root = root
        self.file = root / "config" / "projects.json"  # Legacy migration source.
        self.database = root / "config" / "projects.sqlite3"
        self.lock = threading.RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @contextmanager
    def _connection(self):
        connection = self._connect()
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        self.database.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS projects (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL COLLATE NOCASE UNIQUE,
                    description TEXT NOT NULL, directory TEXT, created_at TEXT
                );
                CREATE TABLE IF NOT EXISTS simulations (
                    id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                    name TEXT NOT NULL, filename TEXT, directory TEXT, archive_path TEXT,
                    results_dir TEXT, namd_dir TEXT NOT NULL, created_at TEXT
                );
            """)
            has_projects = connection.execute("SELECT EXISTS(SELECT 1 FROM projects)").fetchone()[0]
        if has_projects or not self.file.exists():
            return
        try:
            legacy = json.loads(self.file.read_text(encoding="utf-8"))
            if isinstance(legacy, list):
                self._write(legacy)
        except (OSError, json.JSONDecodeError, sqlite3.Error):
            pass

    def _read(self) -> list[dict]:
        with self._connection() as connection:
            project_rows = connection.execute(
                "SELECT id, name, description, directory, created_at FROM projects ORDER BY created_at, id"
            ).fetchall()
            simulation_rows = connection.execute(
                "SELECT id, project_id, name, filename, directory, archive_path, results_dir, namd_dir, created_at "
                "FROM simulations ORDER BY created_at, id"
            ).fetchall()
        projects = [{"id": row["id"], "name": row["name"], "description": row["description"],
                     "directory": row["directory"], "createdAt": row["created_at"], "simulations": []}
                    for row in project_rows]
        by_id = {project["id"]: project for project in projects}
        for row in simulation_rows:
            if row["project_id"] in by_id:
                by_id[row["project_id"]]["simulations"].append({
                    "id": row["id"], "name": row["name"], "filename": row["filename"],
                    "directory": row["directory"], "archivePath": row["archive_path"],
                    "resultsDir": row["results_dir"], "namdDir": row["namd_dir"],
                    "createdAt": row["created_at"],
                })
        for project in projects:
            for simulation in project["simulations"]:
                simulation["state"] = state_from_config(project.get("directory"), simulation["id"])
        known = {s["namdDir"] for p in projects for s in p["simulations"]}
        legacy = next((p for p in projects if p["id"] == "legacy"), None)
        for directory in sorted((self.root / "simulations").glob("*/replica_*/namd")):
            path = str(directory.resolve())
            if path in known or not (directory / "README_preparacion").is_file():
                continue
            if legacy is None:
                legacy = {"id": "legacy", "name": "Simulaciones anteriores", "description": "", "createdAt": None, "simulations": []}
                projects.append(legacy)
            legacy["simulations"].append({"id": hashlib.sha256(path.encode()).hexdigest()[:16],
                "name": directory.parent.parent.name + " / " + directory.parent.name,
                "namdDir": path, "filename": None, "createdAt": None})
        return projects

    def _write(self, projects: list[dict]) -> None:
        with self._connection() as connection:
            connection.execute("DELETE FROM simulations")
            connection.execute("DELETE FROM projects")
            for project in projects:
                connection.execute(
                    "INSERT INTO projects (id, name, description, directory, created_at) VALUES (?, ?, ?, ?, ?)",
                    (project["id"], project["name"], project.get("description", ""),
                     project.get("directory"), project.get("createdAt")),
                )
                for simulation in project.get("simulations", []):
                    connection.execute("""
                        INSERT INTO simulations (
                            id, project_id, name, filename, directory, archive_path,
                            results_dir, namd_dir, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (simulation["id"], project["id"], simulation["name"], simulation.get("filename"),
                           simulation.get("directory"), simulation.get("archivePath"), simulation.get("resultsDir"),
                           simulation["namdDir"], simulation.get("createdAt")))

    def list(self) -> list[dict]:
        with self.lock:
            projects = self._read()
            for project in projects:
                directory = project.get("directory")
                migrated = False
                if directory and Path(directory).is_dir() and not (Path(directory) / "config.ini").is_file():
                    # Projects created by versions prior to config.ini are
                    # upgraded in place the first time they are opened.
                    self._write_project_metadata(project)
                    migrated = True
                for simulation in project.get("simulations", []):
                    namd_dir = Path(simulation["namdDir"])
                    if namd_dir.is_dir() and (migrated or not directory):
                        simulation["state"] = refresh_simulation_state(namd_dir)
            return projects

    def create(self, name: str, description: str = "", base_directory: str = "") -> dict:
        name = name.strip()
        description = description.strip()
        if len(description) > 4000:
            raise ValueError("La descripción admite hasta 4000 caracteres.")
        if not name or len(name) > 100:
            raise ValueError("El nombre del proyecto debe tener entre 1 y 100 caracteres.")
        parent = Path(base_directory).expanduser().resolve()
        if not parent.is_dir() or not parent.is_absolute():
            raise ValueError("Selecciona una carpeta existente para guardar el proyecto.")
        with self.lock:
            projects = self._read()
            if any(p["name"].casefold() == name.casefold() for p in projects):
                raise ValueError("Ya existe un proyecto con ese nombre.")
            project_id = uuid.uuid4().hex[:16]
            slug = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip(".-_")[:60] or "proyecto"
            directory = parent / f"{slug}-{project_id[:8]}"
            directory.mkdir(exist_ok=False)
            (directory / "simulations").mkdir()
            project = {"id": project_id, "name": name, "description": description,
                "directory": str(directory), "createdAt": datetime.now().astimezone().isoformat(), "simulations": []}
            self._write_project_metadata(project)
            projects.append(project)
            self._write(projects)
            return project

    def import_project(self, directory: str) -> dict:
        """Register a portable project from the config.ini stored in its root."""
        root = Path(directory).expanduser().resolve()
        config_path = root / "config.ini"
        if not root.is_dir() or not config_path.is_file():
            raise ValueError("La carpeta seleccionada no contiene config.ini.")
        config = read_config(config_path)
        if not config.has_section("project"):
            raise ValueError("config.ini no contiene la sección [project].")
        values = config["project"]
        project_id = values.get("id", "").strip()
        name = values.get("name", "").strip()
        if not re.fullmatch(r"[A-Za-z0-9]{8,64}", project_id) or not name:
            raise ValueError("config.ini no contiene un proyecto válido.")
        simulations = []
        for section in config.sections():
            match = re.fullmatch(r"simulation:([^:]+)", section)
            if not match:
                continue
            simulation_id = match.group(1)
            simulation_root = (root / "simulations" / simulation_id).resolve()
            try:
                simulation_root.relative_to(root)
            except ValueError as exc:
                raise ValueError("config.ini contiene una ruta de simulación insegura.") from exc
            item = config[section]
            namd_dir = simulation_root / "work" / "replica_01" / "namd"
            if not namd_dir.is_dir():
                raise ValueError(f"No se encontró la carpeta NAMD de {simulation_id}.")
            filename = item.get("filename") or None
            simulations.append({
                "id": simulation_id,
                "name": item.get("name", simulation_id),
                "filename": filename,
                "directory": str(simulation_root),
                "archivePath": str(simulation_root / "charmm-gui" / filename) if filename else None,
                "resultsDir": str(simulation_root / "results"),
                "namdDir": str(namd_dir),
                "createdAt": item.get("created_at") or None,
            })
        project = {
            "id": project_id,
            "name": name,
            "description": values.get("description", "").replace("\\n", "\n"),
            "directory": str(root),
            "createdAt": values.get("created_at") or None,
            "simulations": simulations,
        }
        with self.lock:
            projects = self._read()
            existing = next((item for item in projects if item["id"] == project_id), None)
            if existing:
                return existing
            if any(item["name"].casefold() == name.casefold() for item in projects):
                raise ValueError("Ya existe otro proyecto con ese nombre.")
            projects.append(project)
            self._write(projects)
            self._write_project_metadata(project)
        return project

    def update(self, project_id: str, name: str, description: str) -> dict:
        name, description = name.strip(), description.strip()
        if not name or len(name) > 100 or len(description) > 4000:
            raise ValueError("Nombre obligatorio (hasta 100 caracteres); descripción hasta 4000 caracteres.")
        with self.lock:
            projects = self._read()
            project = next((p for p in projects if p["id"] == project_id), None)
            if project is None:
                raise ValueError("Proyecto inexistente.")
            if any(p["id"] != project_id and p["name"].casefold() == name.casefold() for p in projects):
                raise ValueError("Ya existe un proyecto con ese nombre.")
            project.update(name=name, description=description)
            self._write_project_metadata(project)
            self._write(projects)
            return project

    def get(self, project_id: str) -> dict:
        project = next((p for p in self.list() if p["id"] == project_id), None)
        if project is None:
            raise ValueError("Selecciona un proyecto existente antes de cargar una simulaciÃ³n.")
        return project

    def simulation(self, project_id: str, simulation_id: str) -> dict:
        simulation = next((s for s in self.get(project_id)["simulations"] if s["id"] == simulation_id), None)
        if simulation is None:
            raise ValueError("La simulaciÃ³n no pertenece al proyecto seleccionado.")
        return simulation

    def allocate(self, project_id: str, filename: str, name: str) -> dict:
        project = self.get(project_id)
        simulation_id = uuid.uuid4().hex[:16]
        if project.get("directory"):
            simulation_root = Path(project["directory"]) / "simulations" / simulation_id
            directory = simulation_root / "work" / "replica_01" / "namd"
            archive = simulation_root / "charmm-gui" / Path(filename).name
            results = simulation_root / "results"
        else:
            simulation_root = self.root / "simulations" / "projects" / project_id / simulation_id
            directory = simulation_root / "replica_01" / "namd"
            archive = simulation_root / "charmm-gui" / Path(filename).name
            results = simulation_root / "results"
        simulation_root = simulation_root.resolve()
        directory, archive, results = directory.resolve(), archive.resolve(), results.resolve()
        return {"id": simulation_id, "name": name, "filename": filename,
            "directory": str(simulation_root), "archivePath": str(archive),
            "resultsDir": str(results), "namdDir": str(directory),
            "createdAt": datetime.now().astimezone().isoformat()}

    def register(self, project_id: str, simulation: dict) -> None:
        with self.lock:
            projects = self._read()
            project = next((p for p in projects if p["id"] == project_id), None)
            if project is None:
                raise ValueError("Proyecto inexistente.")
            project["simulations"].append(simulation)
            simulation_root = Path(simulation["directory"])
            simulation_root.mkdir(parents=True, exist_ok=True)
            Path(simulation["resultsDir"]).mkdir(parents=True, exist_ok=True)
            metadata = {key: value for key, value in simulation.items() if key != "archivePath"}
            (simulation_root / "simulation.json").write_text(
                json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
            self._write_project_metadata(project)
            self._write(projects)
            refresh_simulation_state(Path(simulation["namdDir"]))

    def delete_simulation(self, project_id: str, simulation_id: str) -> None:
        """Remove one managed simulation and all of its local artifacts."""
        with self.lock:
            projects = self._read()
            project = next((item for item in projects if item["id"] == project_id), None)
            if project is None:
                raise ValueError("Proyecto inexistente.")
            simulation = next((item for item in project["simulations"] if item["id"] == simulation_id), None)
            if simulation is None:
                raise ValueError("Simulación inexistente.")
            if not project.get("directory"):
                raise ValueError("Las simulaciones anteriores no se eliminan desde el catálogo.")
            root = Path(simulation.get("directory", "")).resolve()
            allowed = (Path(project["directory"]) / "simulations").resolve()
            try:
                root.relative_to(allowed)
            except ValueError as exc:
                raise ValueError("La ruta de la simulación no es segura.") from exc
            if root == allowed or not root.is_dir():
                raise ValueError("La carpeta de la simulación no está disponible.")
            shutil.rmtree(root)
            project["simulations"] = [item for item in project["simulations"] if item["id"] != simulation_id]
            self._write_project_metadata(project)
            self._write(projects)

    def _write_project_metadata(self, project: dict) -> None:
        if not project.get("directory"):
            return
        directory = Path(project["directory"])
        metadata = {key: value for key, value in project.items() if key != "simulations"}
        metadata["simulationCount"] = len(project.get("simulations", []))
        (directory / "project.json").write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
        config_path = directory / "config.ini"
        config = read_config(config_path)
        if not config.has_section("project"):
            config.add_section("project")
        config["project"].update({
            "schema_version": "1",
            "id": str(project["id"]),
            "name": str(project["name"]),
            "description": str(project.get("description", "")).replace("\n", "\\n"),
            "directory": str(directory.resolve()),
            "created_at": str(project.get("createdAt") or ""),
            "simulation_count": str(len(project.get("simulations", []))),
        })
        if not config.has_section("dashboard"):
            config.add_section("dashboard")
        known_simulation_sections = {f"simulation:{item['id']}" for item in project.get("simulations", [])}
        for section in list(config.sections()):
            if section.startswith("simulation:") and not any(
                section == known or section.startswith(known + ":stage:")
                for known in known_simulation_sections
            ):
                config.remove_section(section)
        for simulation in project.get("simulations", []):
            section = f"simulation:{simulation['id']}"
            if not config.has_section(section):
                config.add_section(section)
            config[section].update({
                "id": str(simulation["id"]),
                "name": str(simulation["name"]),
                "filename": str(simulation.get("filename") or ""),
                "directory": f"simulations/{simulation['id']}",
                "results_dir": f"simulations/{simulation['id']}/results",
                "namd_dir": f"simulations/{simulation['id']}/work/replica_01/namd",
                "created_at": str(simulation.get("createdAt") or ""),
                "run_status": config[section].get("run_status", "pending"),
                "current_step": config[section].get("current_step", "step6.0_minimization"),
                "completed_steps": config[section].get("completed_steps", ""),
                "pending_steps": config[section].get("pending_steps", ""),
                "failed_steps": config[section].get("failed_steps", ""),
                "progress_percent": config[section].get("progress_percent", "0"),
                "updated_at": config[section].get("updated_at", str(simulation.get("createdAt") or "")),
            })
        write_config(config_path, config)
