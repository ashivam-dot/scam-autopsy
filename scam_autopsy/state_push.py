"""Push durable cloud state without overwriting a concurrent main-branch update."""

from __future__ import annotations

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MAX_ATTEMPTS = 3


class StatePushError(RuntimeError):
    pass


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=False)


def push_state(root: Path = ROOT, *, attempts: int = MAX_ATTEMPTS) -> None:
    """Fetch, rebase and push; retry a racing remote, never force a conflict.

    The caller has already committed its state change. A rebase conflict is
    aborted so the local commit remains reviewable and the remote is untouched.
    """
    if attempts < 1:
        raise ValueError("attempts must be positive")
    for attempt in range(attempts):
        fetched = _git(root, "fetch", "--no-tags", "origin", "main")
        if fetched.returncode:
            raise StatePushError("Could not fetch origin/main; state commit remains local")
        rebased = _git(root, "rebase", "--no-autostash", "origin/main")
        if rebased.returncode:
            aborted = _git(root, "rebase", "--abort")
            if aborted.returncode:
                raise StatePushError("Rebase failed and could not be aborted; inspect local state before retry")
            raise StatePushError("State rebase conflict; remote unchanged and local commit retained")
        pushed = _git(root, "push", "origin", "HEAD:main")
        if pushed.returncode == 0:
            return
        if attempt == attempts - 1:
            raise StatePushError("State push did not succeed after bounded retries; local commit retained")


def main() -> int:
    push_state()
    print("Cloud state commit is on origin/main")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
