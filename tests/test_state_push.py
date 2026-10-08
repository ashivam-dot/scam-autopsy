"""Local bare-repository tests for concurrent cloud state commits."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from scam_autopsy.state_push import StatePushError, push_state


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=True).stdout.strip()


def setup_repositories(tmp_path: Path) -> tuple[Path, Path, Path]:
    remote = tmp_path / "remote.git"
    seed = tmp_path / "seed"
    worker = tmp_path / "worker"
    peer = tmp_path / "peer"
    remote.mkdir()
    seed.mkdir()
    git(remote, "init", "--bare", "--initial-branch=main")
    git(seed, "init", "--initial-branch=main")
    git(seed, "config", "user.name", "Test owner")
    git(seed, "config", "user.email", "owner@example.test")
    (seed / "state").mkdir()
    (seed / "state/publishing.json").write_text('{"status": "reserved"}\n', encoding="utf-8")
    (seed / "README.md").write_text("Original studio\n", encoding="utf-8")
    git(seed, "add", ".")
    git(seed, "commit", "-m", "Initial state")
    git(seed, "remote", "add", "origin", str(remote))
    git(seed, "push", "-u", "origin", "main")
    git(tmp_path, "clone", str(remote), str(worker))
    git(tmp_path, "clone", str(remote), str(peer))
    for clone in (worker, peer):
        git(clone, "config", "user.name", "Test cloud worker")
        git(clone, "config", "user.email", "worker@example.test")
    return remote, worker, peer


def change_ledger(root: Path, status: str) -> None:
    (root / "state/publishing.json").write_text(json.dumps({"status": status}) + "\n", encoding="utf-8")
    git(root, "add", "state/publishing.json")
    git(root, "commit", "-m", f"Record {status}")


def remote_file(remote: Path, path: str) -> str:
    return subprocess.run(["git", "--git-dir", str(remote), "show", f"main:{path}"],
                          capture_output=True, text=True, check=True).stdout


def test_disjoint_code_update_and_ledger_commit_both_reach_remote(tmp_path):
    remote, worker, peer = setup_repositories(tmp_path)
    change_ledger(worker, "private")
    (peer / "README.md").write_text("Updated studio code\n", encoding="utf-8")
    git(peer, "add", "README.md")
    git(peer, "commit", "-m", "Update code")
    git(peer, "push", "origin", "HEAD:main")

    push_state(worker)

    assert remote_file(remote, "README.md") == "Updated studio code\n"
    assert json.loads(remote_file(remote, "state/publishing.json"))["status"] == "private"


def test_conflicting_ledger_update_fails_closed_and_keeps_remote(tmp_path):
    remote, worker, peer = setup_repositories(tmp_path)
    change_ledger(worker, "private")
    change_ledger(peer, "published")
    git(peer, "push", "origin", "HEAD:main")
    before = git(remote, "rev-parse", "main")

    with pytest.raises(StatePushError, match="conflict"):
        push_state(worker)

    assert git(remote, "rev-parse", "main") == before
    assert json.loads(remote_file(remote, "state/publishing.json"))["status"] == "published"
    assert json.loads((worker / "state/publishing.json").read_text())["status"] == "private"
    assert git(worker, "status", "--porcelain") == ""
