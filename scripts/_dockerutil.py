"""Shared helpers for the setup scripts.

Locating ``docker``: we try ``PATH`` first (correct on Linux/CI and normal
Windows shells), then fall back to Docker Desktop's per-user install location.
This keeps the scripts working even in a shell whose PATH predates the Docker
install, without hardcoding a path that would break on Linux.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
COMPOSE_SERVICE = "db"


def find_docker() -> str:
    """Return the path to the ``docker`` executable, or raise if it cannot be found."""
    exe = shutil.which("docker")
    if exe:
        return exe

    local_appdata = os.environ.get("LOCALAPPDATA", "")
    program_files = os.environ.get("ProgramFiles", "")
    candidates = [
        Path(local_appdata) / "Programs" / "DockerDesktop" / "resources" / "bin" / "docker.exe",
        Path(program_files) / "Docker" / "Docker" / "resources" / "bin" / "docker.exe",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)

    raise RuntimeError(
        "Could not find the 'docker' executable. Is Docker Desktop installed? "
        "If it is, open a new terminal so PATH picks it up, or start Docker Desktop."
    )


def compose_base() -> list[str]:
    """Return the base ``docker compose`` argument list."""
    return [find_docker(), "compose"]


def psql_capture(sql: str, db: str = "postgres") -> str:
    """Run one SQL statement as postgres via psql in the container; return trimmed stdout.

    Raises RuntimeError on a non-zero exit so callers can fail fast.
    """
    cmd = compose_base() + [
        "exec",
        "-T",
        COMPOSE_SERVICE,
        "psql",
        "-v",
        "ON_ERROR_STOP=1",
        "-U",
        "postgres",
        "-d",
        db,
        "-tAc",
        sql,
    ]
    result = subprocess.run(cmd, cwd=PROJECT_ROOT, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"psql failed (exit {result.returncode}): {result.stderr.strip()}")
    return result.stdout.strip()
