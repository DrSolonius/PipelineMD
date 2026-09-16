import io
import shlex
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from execution_targets import resolve_target, remote_path, launch_command, stop_command, sync_outputs, check_remote


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.connection = dict(id="server", name="Server", status="online", scheduler="none",
            host="example.org", username="user", port=2222, authType="agent", remoteWorkdir="~/MD runs")

    def test_missing_unverified_and_pbs_rejected(self):
        for connections in ([], [dict(self.connection, status="error")],
                            [dict(self.connection, scheduler="pbs")]):
            with self.assertRaises(ValueError):
                resolve_target("server", connections)

    def test_ssh_is_snapshot_and_uses_selected_port_and_directory(self):
        target = resolve_target("server", [self.connection])
        target["runDir"] = "~/MD runs/runs/abc"
        self.connection["host"] = "changed"
        command = launch_command(target, Path("."), "local-only")
        self.assertIn("user@example.org", command)
        self.assertIn("2222", command)
        self.assertIn(remote_path(target["runDir"]), shlex.split(command[-1])[-1])
        self.assertNotIn("local-only", command[-1])
        self.assertIn(".pipeline.pid", stop_command(target, Path("."), "")[ -1])

    def test_slurm_uses_scheduler_and_can_be_cancelled(self):
        target = resolve_target("server", [dict(self.connection, scheduler="slurm")])
        target["runDir"] = "~/MD runs/runs/abc"
        launch = launch_command(target, Path("."), "")[-1]
        self.assertIn("sbatch", launch)
        self.assertIn("sacct", launch)
        self.assertIn(".pipeline.slurm_job", launch)
        self.assertIn("scancel", stop_command(target, Path("."), "")[-1])
        resume = launch_command(target, Path("."), "exec csh README_reanudar")[-1]
        self.assertIn("submit_reanudar.slurm", resume)

    def test_preflight_reports_missing_tool(self):
        with patch("execution_targets.subprocess.run", return_value=subprocess.CompletedProcess([], 1, b"OK: csh\nFALTA: namd3\n", b"")):
            with self.assertRaisesRegex(ValueError, "FALTA: namd3"):
                check_remote(self.connection)

    def test_remote_path_quotes_shell_characters(self):
        self.assertEqual(remote_path("~/a; touch bad"), '"$HOME"/\'a; touch bad\'')
        with self.assertRaises(ValueError):
            remote_path("relative")

    def test_sync_only_accepts_regular_stage_outputs(self):
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w:gz") as archive:
            for name in ["step6.0_minimization.out", "step6.0_minimization.coor.old",
                         "step6.0_minimization.xsc.old", "step6.0_minimization.dcd",
                         "../escape", "README_preparacion"]:
                data = b"ENERGY: 0 300\n"
                member = tarfile.TarInfo(name)
                member.size = len(data)
                archive.addfile(member, io.BytesIO(data))
        target = resolve_target("server", [self.connection])
        target["runDir"] = "~/runs/abc"
        with tempfile.TemporaryDirectory() as temporary:
            with patch("execution_targets.subprocess.run", return_value=subprocess.CompletedProcess([], 0, stream.getvalue(), b"")):
                sync_outputs(target, Path(temporary))
            self.assertEqual(sorted(p.name for p in Path(temporary).iterdir()), [
                "step6.0_minimization.coor.old", "step6.0_minimization.out",
                "step6.0_minimization.xsc.old",
            ])


if __name__ == "__main__":
    unittest.main()
