import json
from pathlib import Path
import pytest
from scam_autopsy import cli


@pytest.fixture
def queue(tmp_path, monkeypatch):
    source = Path(__file__).resolve().parents[1] / "content/01-house-wire-r2.json"
    first = json.loads(source.read_text())
    second = dict(first, id="second-case")
    (tmp_path / "content").mkdir()
    (tmp_path / "content/a.json").write_text(json.dumps(first))
    (tmp_path / "content/b.json").write_text(json.dumps(second))
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    monkeypatch.setattr(cli, "LEDGER", tmp_path / "state/publishing.json")
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    return first, second


def test_private_preview_is_not_implicitly_promoted(queue):
    first, second = queue
    cli.reserve(first["id"])
    state = cli.read(); state["cases"][first["id"]]["status"] = "private"; cli.write(cli.LEDGER, state)
    assert cli.reserve()["id"] == second["id"]


def test_request_cannot_silently_select_another_active_case(queue):
    first, second = queue
    cli.reserve(first["id"])
    with pytest.raises(RuntimeError, match="unfinished"):
        cli.reserve(second["id"])


def test_preview_cannot_publish_with_changed_script(queue):
    first, _ = queue
    cli.reserve(first["id"])
    state = cli.read(); state["cases"][first["id"]]["status"] = "private"; cli.write(cli.LEDGER, state)
    first["scenes"][0]["narration"] = "A changed narration cannot match the earlier upload."
    (cli.ROOT / "content/a.json").write_text(json.dumps(first))
    with pytest.raises(RuntimeError, match="changed"):
        cli.reserve(first["id"])
