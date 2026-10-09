"""Health alerts use durable receipts and minimum evidence, without API calls."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from scam_autopsy import health


NOW = datetime(2026, 10, 8, 18, 0, tzinfo=timezone.utc)


def case(case_id: str) -> dict:
    return {"id": case_id, "approved": True, "review": {"reviewer": "source-review"},
            "title": "A sourced case", "source_urls": ["https://consumer.ftc.gov/consumer-alerts/2026/09/example"],
            "format": "short", "scenes": [{"narration": "A", "label": "Illustration"}] * 7}


class HealthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "content").mkdir()
        (self.root / "state").mkdir()
        self.write("state/publishing.json", {"channel_id": health.CHANNEL_ID, "cases": {}})
        self.write("state/research.json", {"status": "passed", "checked_at": NOW.isoformat()})
        self.write("state/analytics-latest.json", {"as_of": NOW.date().isoformat(), "analytics": []})
        self.write("state/growth.json", {"as_of": NOW.date().isoformat(), "eligible_videos": []})
        for i in range(3):
            self.write(f"content/case-{i}.json", case(f"case-{i}"))

    def write(self, name: str, value: dict):
        path = self.root / name
        path.write_text(json.dumps(value), encoding="utf-8")

    def read(self, name: str) -> dict:
        return json.loads((self.root / name).read_text(encoding="utf-8"))

    def codes(self, report: dict) -> set[str]:
        return {item["code"] for item in report["alerts"]}

    def record(self, case_id: str, **changes) -> dict:
        content = self.read(f"content/{case_id}.json")
        record = {"status": "published", "channel_id": health.CHANNEL_ID, "privacy": "public",
                  "processing": "succeeded", "video_id": "VIDEO123",
                  "content_sha256": health._content_hash(content),
                  "verified_at": NOW.isoformat()}
        record.update(changes)
        return record

    def test_new_channel_has_ready_stock_without_no_publication_alert(self):
        report = health.assess(self.root, now=NOW)
        self.assertEqual(report["status"], "healthy")
        self.assertEqual(report["counts"]["ready_queue"], 3)
        self.assertEqual(report["counts"]["confirmed_public_cases"], 0)
        self.assertNotIn("no_new_public_case_over_10d", self.codes(report))

    def test_malformed_approved_content_does_not_inflate_ready_stock(self):
        broken = case("case-0")
        broken["source_urls"] = []
        self.write("content/case-0.json", broken)
        report = health.assess(self.root, now=NOW)
        self.assertEqual(report["counts"]["reviewed_stock"], 2)
        self.assertEqual(report["counts"]["ready_queue"], 2)

    def test_only_verified_public_receipt_with_matching_hash_counts(self):
        ledger = self.read("state/publishing.json")
        ledger["cases"]["case-0"] = self.record("case-0")
        self.write("state/publishing.json", ledger)
        report = health.assess(self.root, now=NOW)
        self.assertEqual(report["counts"]["confirmed_public_cases"], 1)
        self.assertEqual(report["counts"]["ready_queue"], 2)
        self.assertEqual(report["status"], "healthy")

        for change in ({"privacy": "private"}, {"processing": "processing"},
                       {"channel_id": "wrong-channel"}, {"content_sha256": "wrong"},
                       {"video_id": ""}):
            with self.subTest(change=change):
                ledger["cases"]["case-0"] = self.record("case-0", **change)
                self.write("state/publishing.json", ledger)
                report = health.assess(self.root, now=NOW)
                self.assertEqual(report["counts"]["confirmed_public_cases"], 0)
                self.assertIn("invalid_public_receipt", self.codes(report))

    def test_private_preview_never_becomes_production_or_ready(self):
        ledger = self.read("state/publishing.json")
        ledger["cases"]["case-0"] = self.record("case-0", status="private", privacy="private")
        self.write("state/publishing.json", ledger)
        report = health.assess(self.root, now=NOW + timedelta(days=20))
        self.assertEqual(report["counts"]["private_previews"], 1)
        self.assertEqual(report["counts"]["confirmed_public_cases"], 0)
        self.assertEqual(report["counts"]["ready_queue"], 2)
        self.assertNotIn("no_new_public_case_over_10d", self.codes(report))

    def test_stale_active_reservation_and_low_queue_alert(self):
        ledger = self.read("state/publishing.json")
        ledger["cases"]["case-0"] = {"status": "reserved", "reserved_at": (NOW - timedelta(hours=25)).isoformat()}
        ledger["cases"]["case-1"] = {"status": "private"}
        self.write("state/publishing.json", ledger)
        report = health.assess(self.root, now=NOW)
        self.assertEqual(report["counts"]["ready_queue"], 1)
        self.assertIn("active_over_24h", self.codes(report))
        self.assertIn("ready_queue_below_2", self.codes(report))

    def test_exact_age_boundaries_do_not_trigger_stale_alerts(self):
        ledger = self.read("state/publishing.json")
        ledger["cases"]["case-0"] = self.record("case-0", verified_at=(NOW - timedelta(days=10)).isoformat())
        ledger["cases"]["case-1"] = {"status": "reserved", "reserved_at": (NOW - timedelta(hours=24)).isoformat()}
        self.write("state/publishing.json", ledger)
        self.write("state/analytics-latest.json", {"as_of": (NOW - timedelta(hours=72)).isoformat(),
                                                    "analytics": []})
        codes = self.codes(health.assess(self.root, now=NOW))
        self.assertNotIn("active_over_24h", codes)
        self.assertNotIn("analytics_over_72h", codes)
        self.assertNotIn("no_new_public_case_over_10d", codes)

    def test_research_failure_is_factual_and_excludes_raw_error(self):
        self.write("state/research.json", {"status": "failed", "checked_at": "2026-10-08T23:30:00+05:30",
                                          "error": "secret key and comment text must not appear"})
        report = health.assess(self.root, now=NOW)
        self.assertIn("research_failed", self.codes(report))
        self.assertEqual(report["last_pipeline_failure"],
                         {"component": "research", "at": NOW.isoformat(), "status": "failed"})
        self.assertNotIn("secret key", json.dumps(report))

    def test_recent_research_failure_with_healthy_stock_is_a_warning(self):
        for i in range(3, 5):
            self.write(f"content/case-{i}.json", case(f"case-{i}"))
        recent = {"status": "failed", "checked_at": NOW.isoformat(),
                  "failing_since": (NOW - timedelta(hours=20)).isoformat()}
        self.write("state/research.json", recent)
        report = health.assess(self.root, now=NOW)
        self.assertEqual(report["status"], "healthy")
        self.assertEqual(report["warnings"], [{"code": "research_retrying", "detail": ""}])
        self.assertEqual(report["last_pipeline_failure"]["component"], "research")

        for state in (recent | {"failing_since": (NOW - timedelta(hours=73)).isoformat()},
                      {"status": "failed", "checked_at": NOW.isoformat()}):
            with self.subTest(state=state):
                self.write("state/research.json", state)
                self.assertIn("research_failed", self.codes(health.assess(self.root, now=NOW)))

    def test_partial_research_exhausted_alerts_when_queue_low(self):
        ledger = self.read("state/publishing.json")
        ledger["cases"]["case-0"] = self.record("case-0")
        ledger["cases"]["case-1"] = self.record("case-1")
        self.write("state/publishing.json", ledger)
        self.write("state/research.json", {"status": "partial_success", "generation_calls": 4,
                                          "stopped_reason": "remaining calls cannot cover review"})
        report = health.assess(self.root, now=NOW)
        self.assertIn("research_partial_exhausted", self.codes(report))
        self.assertIn("ready_queue_below_2", self.codes(report))

    def test_analytics_age_and_new_publication_age_thresholds(self):
        ledger = self.read("state/publishing.json")
        ledger["cases"]["case-0"] = self.record("case-0", verified_at=(NOW - timedelta(days=11)).isoformat())
        self.write("state/publishing.json", ledger)
        self.write("state/analytics-latest.json", {"as_of": (NOW - timedelta(days=4)).date().isoformat(),
                                                    "analytics": []})
        report = health.assess(self.root, now=NOW)
        self.assertIn("analytics_over_72h", self.codes(report))
        self.assertIn("no_new_public_case_over_10d", self.codes(report))

    def test_comparisons_require_100_engaged_views(self):
        self.write("state/analytics-latest.json", {"as_of": NOW.isoformat(), "analytics": [
            {"video": "a", "engagedViews": 99}, {"video": "b", "engagedViews": 100},
            {"video": "c", "engagedViews": 140}, {"video": "d", "engagedViews": "100"}]})
        self.write("state/growth.json", {"eligible_videos": [{"video": "a", "engagedViews": 99}]})
        report = health.assess(self.root, now=NOW)
        self.assertEqual(report["counts"]["eligible_comparisons"], 2)
        self.assertIn("growth_comparison_under_100_engaged_views", self.codes(report))

    def test_invalid_input_still_writes_attention_report_and_cli_code(self):
        (self.root / "state/analytics-latest.json").write_text("{bad")
        with patch("sys.argv", ["health", "--root", str(self.root)]):
            self.assertEqual(health.main(), 1)
        report = self.read("state/health.json")
        self.assertIn("invalid_state_json", self.codes(report))
        self.assertIn("analytics_missing_or_undated", self.codes(report))

    def test_healthy_cli_writes_report_and_exits_zero(self):
        original = health.assess
        with patch("sys.argv", ["health", "--root", str(self.root)]), \
             patch.object(health, "assess", side_effect=lambda root, now=None: original(root, now=NOW)):
            self.assertEqual(health.main(), 0)
        self.assertEqual(self.read("state/health.json")["status"], "healthy")


if __name__ == "__main__":
    unittest.main()
