import copy
import json
from pathlib import Path
from unittest.mock import MagicMock
import pytest
from scam_autopsy import youtube

ROOT = Path(__file__).resolve().parents[1]


def case():
    return json.loads((ROOT / "content/01-house-wire.json").read_text())


def test_channel_mismatch_refuses_writes():
    api = MagicMock()
    api.channels.return_value.list.return_value.execute.return_value = {"items": [{"id": "another-channel"}]}
    with pytest.raises(RuntimeError, match="mismatch"):
        youtube.identity(api)
    api.videos.assert_not_called()


@pytest.mark.parametrize("url", ["http://www.ic3.gov/test", "https://www.ic3.gov.evil.test/", "https://user:password@www.ic3.gov/", "https://127.0.0.1/", "https://www.ic3.gov:8443/"])
def test_primary_source_boundary(url):
    value = case(); value["source_urls"] = [url]
    with pytest.raises(ValueError): youtube.validate(value)


def test_unreviewed_case_cannot_publish():
    value = case(); value["approved"] = False
    with pytest.raises(ValueError, match="reviewed"): youtube.validate(value)


def test_all_seed_scripts_have_sources_and_disclosures():
    for path in (ROOT / "content").glob("*.json"):
        youtube.validate(json.loads(path.read_text()))


def test_duplicate_uploaded_case_stops_republication(monkeypatch):
    value = case()
    monkeypatch.setattr(youtube, "recent", lambda api, channel: [{"id": "one", "snippet": {"tags": [youtube.marker(value)]}}, {"id": "two", "snippet": {"tags": [youtube.marker(value)]}}])
    with pytest.raises(RuntimeError, match="Duplicate"): youtube.find_case(None, None, value)


def test_description_has_primary_sources_and_synthetic_narration():
    text = youtube.description(case())
    assert "ic3.gov" in text and "synthetic narration" in text and len(text) < 5000
