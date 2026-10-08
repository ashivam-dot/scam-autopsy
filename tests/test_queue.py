import json
from pathlib import Path
from unittest.mock import Mock
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


def test_interrupted_private_preview_reconciles_without_schedule_promotion(queue, monkeypatch):
    first, _ = queue
    cli.reserve(first["id"], private=True)
    state = cli.read()
    record = state["cases"][first["id"]]
    record.update(status="processing", video_id="existing-private-video")
    cli.write(cli.LEDGER, state)
    output = cli.ROOT / "reserve-output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))

    assert cli.reserve()["id"] == first["id"]  # Scheduled run reconciles the active case.
    assert "intent=private\n" in output.read_text()
    client = Mock()
    monkeypatch.setattr(cli.youtube, "client", client)
    with pytest.raises(RuntimeError, match="Private reservation"):
        cli.publish(cli.ROOT / "content/a.json", "unused.mp4", make_public=True)
    client.assert_not_called()

    monkeypatch.setattr(cli.youtube, "identity", lambda api: {"id": cli.youtube.CHANNEL_ID})
    monkeypatch.setattr(cli.youtube, "find_case", lambda api, channel, case: {"id": "existing-private-video"})
    finalize = Mock(return_value={"video_id": "existing-private-video", "privacy": "private",
                                  "channel_id": cli.youtube.CHANNEL_ID, "processing": "succeeded"})
    monkeypatch.setattr(cli.youtube, "finalize", finalize)
    cli.publish(cli.ROOT / "content/a.json", "unused.mp4", make_public=False)
    assert cli.read()["cases"][first["id"]]["status"] == "private"
    assert finalize.call_args.kwargs["publish"] is False


def test_only_named_manual_public_confirmation_can_promote_private_case(queue):
    first, _ = queue
    cli.reserve(first["id"], private=True)
    with pytest.raises(ValueError, match="name the exact case"):
        cli.reserve(public_confirmed=True)
    cli.reserve(first["id"], public_confirmed=True)
    record = cli.read()["cases"][first["id"]]
    assert record["intended_private"] is False
    assert record["public_confirmed_at"]


def test_legacy_interrupted_reservation_without_intent_defaults_private(queue):
    first, _ = queue
    cli.reserve(first["id"])
    state = cli.read()
    record = state["cases"][first["id"]]
    record.pop("intended_private")
    record["status"] = "processing"
    cli.write(cli.LEDGER, state)
    cli.reserve()  # Scheduled recovery must persist a conservative intent.
    assert cli.read()["cases"][first["id"]]["intended_private"] is True
