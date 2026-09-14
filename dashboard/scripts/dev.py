#!/usr/bin/env python3
"""Levanta juntos el frontend y la API local del pipeline."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).parents[1]


def main() -> int:
    upload = subprocess.Popen([sys.executable, str(ROOT / "scripts" / "upload_server.py")], cwd=ROOT)
    npm = "npm.cmd" if sys.platform == "win32" else "npm"
    try:
        web = subprocess.Popen([npm, "run", "dev:web"], cwd=ROOT)
        return web.wait()
    finally:
        upload.terminate()
        try:
            upload.wait(timeout=5)
        except subprocess.TimeoutExpired:
            upload.kill()


if __name__ == "__main__":
    raise SystemExit(main())
