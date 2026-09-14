"""Persistent project/simulation catalog; legacy directories stay in place."""
from __future__ import annotations

from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import threading
import uuid


class ProjectStore:
    def __init__(self, root: Path):
        self.root = root
        self.file = root / "config" / "projects.json"
        self.lock = threading.RLock()

    def _read(self) -> list[dict]:
        projects = json.loads(self.file.read_text(encoding="utf-8")) if self.file.exists() else []
        for project in projects:
            project.setdefault("description", "")
            project.setdefault("directory", None)
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
        self.file.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.file.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(projects, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.file)

    def list(self) -> list[dict]:
        with self.lock:
            return self._read()

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

    def _write_project_metadata(self, project: dict) -> None:
        if not project.get("directory"):
            return
        directory = Path(project["directory"])
        metadata = {key: value for key, value in project.items() if key != "simulations"}
        metadata["simulationCount"] = len(project.get("simulations", []))
        (directory / "project.json").write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
