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

    def test_legacy_discovery_without_moving_files(self):
        directory = self.root / "simulations" / "old" / "replica_01" / "namd"
        directory.mkdir(parents=True)
        (directory / "README_preparacion").write_text("original")
        legacy = self.store.get("legacy")
        self.assertEqual(legacy["simulations"][0]["namdDir"], str(directory.resolve()))
        self.store.create("Nuevo", "", str(self.root))
        self.assertEqual(len(self.store.get("legacy")["simulations"]), 1)
        self.assertEqual((directory / "README_preparacion").read_text(), "original")

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
