import json
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from projects import ProjectStore
from project_state import read_config, refresh_simulation_state
import upload_server as api


class ProjectTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.store = ProjectStore(self.root)

    def test_persistence_and_duplicate_names(self):
        project = self.store.create("  Estudio A  ", "Descripción del estudio", str(self.root))
        self.assertEqual(ProjectStore(self.root).get(project["id"])["name"], "Estudio A")
        self.assertEqual(ProjectStore(self.root).get(project["id"])["description"], "Descripción del estudio")
        for name in ("", "estudio a", "a" * 101):
            with self.assertRaises(ValueError):
                self.store.create(name, "", str(self.root))

    def test_same_file_creates_distinct_simulations_and_projects(self):
        a, b = self.store.create("A", "", str(self.root)), self.store.create("B", "", str(self.root))
        simulations = [self.store.allocate(project["id"], "charmm-gui.tgz", "Sistema") for project in (a, a, b)]
        self.assertEqual(len({s["namdDir"] for s in simulations}), 3)
        self.store.register(a["id"], simulations[0])
        with self.assertRaises(ValueError):
            self.store.simulation(b["id"], simulations[0]["id"])
        with self.assertRaises(ValueError):
            self.store.allocate("../../escape", "file.tgz", "Sistema")

    def test_config_ini_tracks_project_and_simulation_steps(self):
        project = self.store.create("Estado portable", "Línea uno\nLínea dos", str(self.root))
        simulation = self.store.allocate(project["id"], "system.tgz", "Sistema")
        namd_dir = Path(simulation["namdDir"])
        namd_dir.mkdir(parents=True)
        for stage in ("step6.0_minimization", "step6.1_thermalization", "step6.2_equilibration"):
            (namd_dir / f"{stage}.inp").write_text("run 20\n")
        self.store.register(project["id"], simulation)
        state = refresh_simulation_state(namd_dir, [
            {"stage": "step6.0_minimization", "status": "done"},
            {"stage": "step6.1_thermalization", "status": "running"},
        ], active=True)
        self.assertEqual(state["currentStep"], "step6.1_thermalization")
        self.assertEqual(state["completedSteps"], ["step6.0_minimization"])
        self.assertEqual(state["pendingSteps"], ["step6.2_equilibration"])
        config = read_config(Path(project["directory"]) / "config.ini")
        self.assertEqual(config["project"]["id"], project["id"])
        self.assertEqual(config["dashboard"]["active_simulation_id"], simulation["id"])
        loaded = ProjectStore(self.root).simulation(project["id"], simulation["id"])
        self.assertEqual(loaded["state"]["progressPercent"], 33)

    def test_state_can_be_rebuilt_from_namd_outputs(self):
        project = self.store.create("Reinicio", "", str(self.root))
        simulation = self.store.allocate(project["id"], "system.tgz", "Sistema")
        namd_dir = Path(simulation["namdDir"])
        namd_dir.mkdir(parents=True)
        (namd_dir / "step6.0_minimization.inp").write_text("run 20\n")
        (namd_dir / "step6.1_thermalization.inp").write_text("run 20\n")
        (namd_dir / "step6.0_minimization.out").write_text("Info: End of program\n")
        (namd_dir / "step6.1_thermalization.out").write_text("ENERGY: 20\n")
        self.store.register(project["id"], simulation)
        state = refresh_simulation_state(namd_dir)
        self.assertEqual(state["completedSteps"], ["step6.0_minimization"])
        self.assertEqual(state["currentStep"], "step6.1_thermalization")
        self.assertEqual(state["status"], "running")

    def test_resume_readme_skips_only_successful_stages(self):
        namd_dir = self.root / "namd"
        namd_dir.mkdir()
        for stage in ("step6.0_minimization", "step6.1_thermalization", "step6.2_equilibration"):
            (namd_dir / f"{stage}.inp").write_text("run 20\n")
        name, pending = api.build_resume_readme(namd_dir, ["step6.0_minimization"])
        script = (namd_dir / name).read_text()
        self.assertIn("SKIP step6.0_minimization", script)
        self.assertNotIn("namd3 step6.0_minimization.inp", script)
        self.assertIn("namd3 step6.1_thermalization.inp", script)
        self.assertEqual(pending, ["step6.1_thermalization", "step6.2_equilibration"])
        empty = self.root / "empty"
        empty.mkdir()
        with self.assertRaisesRegex(ValueError, "step6"):
            api.build_resume_readme(empty, [])

    def test_archiving_preserves_successful_stage_artifacts(self):
        namd_dir = self.root / "archive-namd"
        namd_dir.mkdir()
        kept = namd_dir / "step6.0_minimization.out"
        retried = namd_dir / "step6.1_thermalization.out"
        kept.write_text("Info: End of program\n")
        retried.write_text("partial\n")
        history = api.archive_previous_outputs(namd_dir, {"step6.0_minimization"})
        self.assertTrue(kept.is_file())
        self.assertFalse(retried.exists())
        self.assertTrue((history / retried.name).is_file())

    def test_project_can_be_imported_from_portable_config(self):
        project = self.store.create("Portable", "Objetivo", str(self.root))
        simulation = self.store.allocate(project["id"], "system.tgz", "Sistema")
        Path(simulation["namdDir"]).mkdir(parents=True)
        self.store.register(project["id"], simulation)
        other_root = self.root / "other-catalog"
        imported = ProjectStore(other_root).import_project(project["directory"])
        self.assertEqual(imported["id"], project["id"])
        self.assertEqual(imported["description"], "Objetivo")
        self.assertEqual(imported["simulations"][0]["namdDir"], simulation["namdDir"])

    def test_legacy_discovery_without_moving_files(self):
        directory = self.root / "simulations" / "old" / "replica_01" / "namd"
        directory.mkdir(parents=True)
        (directory / "README_preparacion").write_text("original")
        legacy = self.store.get("legacy")
        self.assertEqual(legacy["simulations"][0]["namdDir"], str(directory.resolve()))
        self.assertEqual(legacy["simulations"][0]["state"]["currentStep"], None)
        self.store.create("Nuevo", "", str(self.root))
        self.assertEqual(len(self.store.get("legacy")["simulations"]), 1)
        self.assertEqual((directory / "README_preparacion").read_text(), "original")

    def test_old_managed_project_is_migrated_to_config_ini(self):
        project = self.store.create("Anterior", "", str(self.root))
        config_path = Path(project["directory"]) / "config.ini"
        config_path.unlink()
        loaded = ProjectStore(self.root).get(project["id"])
        self.assertEqual(loaded["id"], project["id"])
        self.assertTrue(config_path.is_file())

    def test_http_create_upload_select_and_membership(self):
        def prepare(command, **kwargs):
            output = Path(command[-1])
            output.mkdir(parents=True)
            (output / "README_preparacion").write_text("test")
            return subprocess.CompletedProcess(command, 0, "{}", "")

        with patch.multiple(api, PROJECT_STORE=self.store, SIMULATIONS=self.root / "simulations",
                            UPLOADS=self.root / "uploads", ACTIVE_DATA_POINTER=self.root / "active.json",
                            SSH_CONNECTIONS_FILE=self.root / "ssh_connections.json",
                            CREDENTIALS=MagicMock()), \
             patch.object(api, "ensure_ingester") as ingest, \
             patch.object(api.subprocess, "run", side_effect=prepare) as runner:
            server = api.ThreadingHTTPServer(("127.0.0.1", 0), api.Handler)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            def request(path, body=None, headers=None):
                if isinstance(body, dict):
                    body = json.dumps(body).encode()
                with urlopen(Request(f"http://127.0.0.1:{server.server_port}{path}", data=body, headers=headers or {})) as response:
                    return json.load(response)
            try:
                a = request("/projects", {"name": "A", "baseDirectory": str(self.root)})["project"]
                b = request("/projects", {"name": "B", "baseDirectory": str(self.root)})["project"]
                ssh = request("/ssh-connections", {"name": "CUDA1", "host": "192.0.2.11",
                    "port": 22, "username": "guido", "authType": "agent", "keyPath": "",
                    "remoteWorkdir": "/home/guido", "scheduler": "slurm",
                    "setupCommand": "module load namd/3.0.3"})["connection"]
                self.assertEqual(ssh["setupCommand"], "module load namd/3.0.3")
                ssh["setupCommand"] = "source /opt/namd/environment.sh"
                updated_ssh = request("/ssh-connections", ssh)["connection"]
                self.assertEqual(updated_ssh["setupCommand"], "source /opt/namd/environment.sh")
                with self.assertRaises(HTTPError) as error:
                    request("/upload", b"test", {"X-Filename": "system.tgz"})
                self.assertEqual(error.exception.code, 400)
                headers = {"X-Filename": "system.tgz", "X-Project-Id": a["id"]}
                first = request("/upload", b"test", headers)["simulation"]
                project_directory = Path(self.store.get(a["id"])["directory"])
                self.assertTrue((project_directory / "project.json").is_file())
                self.assertTrue((project_directory / "simulations" / first["id"] / "simulation.json").is_file())
                self.assertEqual(Path(first["archivePath"]).read_bytes(), b"test")
                self.assertTrue(Path(first["resultsDir"]).is_dir())
                second = request("/upload", b"test", headers)["simulation"]
                self.assertNotEqual(first["namdDir"], second["namdDir"])
                self.assertEqual(len(request("/projects")["projects"][0]["simulations"]), 2)
                updated = request("/update-project", {"projectId": a["id"], "name": "Proyecto editado", "description": "Objetivo del estudio\nSegunda línea"})["project"]
                self.assertEqual(updated["description"], "Objetivo del estudio\nSegunda línea")
                self.assertEqual(len(updated["simulations"]), 2)
                self.assertEqual(ProjectStore(self.root).get(a["id"])["name"], "Proyecto editado")
                request("/select-simulation", {"projectId": a["id"], "simulationId": first["id"]})
                ingest.assert_called_with(Path(first["namdDir"]))
                with self.assertRaises(HTTPError) as error:
                    request("/run-preparation", {"projectId": b["id"], "simulationId": first["id"]})
                self.assertEqual(error.exception.code, 400)
                self.assertEqual(runner.call_count, 2)
                runner.side_effect = None
                runner.return_value = subprocess.CompletedProcess([], 1, "", "invalid archive")
                with self.assertRaises(HTTPError) as error:
                    request("/upload", b"invalid", headers)
                self.assertEqual(error.exception.code, 422)
                self.assertEqual(len(self.store.get(a["id"])["simulations"]), 2)
            finally:
                server.shutdown()
                server.server_close()


if __name__ == "__main__":
    unittest.main()
