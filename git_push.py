"""
git_push.py
===========
Senior DevOps GitOps helper: commits and pushes freshly-fetched telemetry
JSON files from the local Git repository to `origin/main`, non-interactively.

Intended to run immediately after `fetch_grid.py` (see grid-tracker.service).
Logs to `update.log` in the repo directory and raises a non-zero exit code
(with a CRITICAL log alert) on any failure so systemd/monitoring can detect it.
"""

from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------
REPO_DIR = Path(__file__).resolve().parent
LOG_FILE = REPO_DIR / "update.log"

# Pathspec passed to `git add`. Git supports glob patterns natively in
# pathspecs, so no shell expansion is required. Must match fetch_grid.py's
# EXPORT_DIR (docs/data, so GitHub Pages can serve index.html + data together).
DATA_PATHSPEC = "docs/data/*.json"

COMMIT_MESSAGE = (
    "chore(telemetry): automated update of national, balancing, and regional grid data"
)
GIT_TIMEOUT = 30  # seconds per git subprocess call

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
    ],
)
logger = logging.getLogger("git_push")


class GitCommandError(RuntimeError):
    """Raised when a git subprocess exits with a non-zero status."""


def run_git(*args: str) -> subprocess.CompletedProcess:
    """Run a git command in REPO_DIR, logging output, raising on failure."""
    cmd = ["git", *args]
    logger.info("Running: %s", " ".join(cmd))
    try:
        result = subprocess.run(
            cmd,
            cwd=REPO_DIR,
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT,
        )
    except subprocess.TimeoutExpired as exc:
        raise GitCommandError(f"Command timed out after {GIT_TIMEOUT}s: {' '.join(cmd)}") from exc

    if result.stdout.strip():
        logger.info("stdout: %s", result.stdout.strip())
    if result.stderr.strip():
        logger.warning("stderr: %s", result.stderr.strip())

    if result.returncode != 0:
        raise GitCommandError(
            f"Command failed (exit {result.returncode}): {' '.join(cmd)}\n{result.stderr.strip()}"
        )
    return result


def alert(message: str) -> None:
    """Surface a critical failure. Extend this to page/webhook/email as needed."""
    logger.critical("ALERT: %s", message)


def main() -> int:
    logger.info("=== git_push run starting in %s ===", REPO_DIR)
    try:
        run_git("add", DATA_PATHSPEC)

        status = run_git("status", "--porcelain")
        if not status.stdout.strip():
            logger.info("No changes detected after git add; nothing to commit or push.")
            return 0

        run_git("commit", "-m", COMMIT_MESSAGE)
        run_git("push", "origin", "main")

        logger.info("Push to origin/main completed successfully.")
        return 0

    except GitCommandError as exc:
        alert(str(exc))
        return 1
    except Exception as exc:  # noqa: BLE001 - top-level safety net for unattended runs
        alert(f"Unexpected error: {exc}")
        return 1


if __name__ == "__main__":
    exit_code = main()
    if exit_code != 0:
        logger.critical("git_push.py exiting with non-zero status %d", exit_code)
    sys.exit(exit_code)
