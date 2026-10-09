"""Read-only assessment of durable studio evidence, with a local health report.

No YouTube API call is made. Receipts count only when they match reviewed content.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[1]
CHANNEL_ID = "UCz0W-lSVvEeWYufkUVaVUKA"
ACTIVE = {"reserved", "uploading", "processing"}
MIN_ENGAGED_VIEWS = 100
READY_LOW_WATERMARK = 4
# Daily refills retry a failed research run; free-tier 429/503s clear on their own.
RESEARCH_RETRY_GRACE = timedelta(hours=72)
SOURCE_HOSTS = {"www.ic3.gov", "ic3.gov", "www.fbi.gov", "fbi.gov", "consumer.ftc.gov",
                "www.ftc.gov", "ftc.gov", "www.cisa.gov", "cisa.gov"}


def _utc(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = datetime.combine(date.fromisoformat(value), time.min)
        except ValueError:
            return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def _read(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"invalid JSON object: {path.name}")
    return value


def _content_hash(case: dict[str, Any]) -> str:
    # Same canonical payload used by the durable publishing reservation.
    payload = {key: value for key, value in case.items() if key != "path"}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _approved(case: dict[str, Any]) -> bool:
    if (case.get("approved") is not True or case.get("format") not in ("short", "long") or
            not isinstance(case.get("review"), dict) or
            not case["review"].get("reviewer") or not isinstance(case.get("id"), str) or
            not case["id"] or not isinstance(case.get("title"), str) or
            not case["title"] or len(case["title"]) > 100):
        return False
    urls = case.get("source_urls")
    if not isinstance(urls, list) or not urls:
        return False
    for url in urls:
        if not isinstance(url, str):
            return False
        try:
            parts = urlsplit(url)
        except ValueError:
            return False
        if parts.scheme != "https" or parts.hostname not in SOURCE_HOSTS or parts.username or parts.password:
            return False
        try:
            if parts.port not in (None, 443):
                return False
        except ValueError:
            return False
    scenes = case.get("scenes")
    if not isinstance(scenes, list) or not scenes or (case.get("format") == "short" and len(scenes) != 7):
        return False
    return all(isinstance(scene, dict) and scene.get("narration") and scene.get("label") for scene in scenes)


def _public_receipt(record: dict[str, Any], case: dict[str, Any] | None) -> bool:
    return bool(case and _approved(case) and record.get("status") == "published" and
                record.get("channel_id") == CHANNEL_ID and record.get("privacy") == "public" and
                record.get("processing") == "succeeded" and
                isinstance(record.get("video_id"), str) and record["video_id"] and
                record.get("content_sha256") == _content_hash(case) and
                _utc(record.get("verified_at")))


def assess(root: Path = ROOT, *, now: datetime | None = None) -> dict[str, Any]:
    now = _utc(now.isoformat()) if now is not None else datetime.now(timezone.utc)
    assert now is not None
    input_errors: list[str] = []

    def read_state(name: str) -> dict[str, Any] | None:
        try:
            return _read(root / "state" / name)
        except (ValueError, OSError):
            input_errors.append(name)
            return None

    ledger = read_state("publishing.json") or {"cases": {}}
    research = read_state("research.json")
    analytics = read_state("analytics-latest.json")
    growth = read_state("growth.json")
    records = ledger.get("cases", {})
    if not isinstance(records, dict):
        input_errors.append("publishing.json")
        records = {}

    content: dict[str, dict[str, Any]] = {}
    bad_content: list[str] = []
    for path in sorted((root / "content").glob("*.json")):
        try:
            case = _read(path)
        except (ValueError, OSError):
            bad_content.append(path.name)
            continue
        if case and isinstance(case.get("id"), str) and case["id"]:
            if case["id"] in content:
                bad_content.append(path.name)
                continue
            content[case["id"]] = case

    alerts: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []

    def alert(code: str, detail: str = "") -> None:
        alerts.append({"code": code, "detail": detail})

    for name in sorted(set(input_errors)):
        alert("invalid_state_json", name)
    for name in bad_content:
        alert("invalid_content_json", name)
    if not (root / "state/publishing.json").exists():
        alert("publishing_missing")
    if ledger.get("channel_id") != CHANNEL_ID:
        alert("ledger_channel_mismatch")

    approved = {case_id: case for case_id, case in content.items() if _approved(case)}
    ready = [case_id for case_id in approved if case_id not in records]
    public_times: list[datetime] = []
    private_count = 0
    active_count = 0
    for case_id, record in records.items():
        if not isinstance(record, dict):
            alert("invalid_publishing_record", str(case_id))
            continue
        status = record.get("status")
        if not isinstance(status, str):
            alert("invalid_publishing_record", str(case_id))
            continue
        if status == "private":
            private_count += 1
        if status == "published":
            if ledger.get("channel_id") == CHANNEL_ID and _public_receipt(record, content.get(case_id)):
                when = _utc(record["verified_at"])
                assert when is not None
                public_times.append(when)
            else:
                alert("invalid_public_receipt", str(case_id))
        if status in ACTIVE:
            active_count += 1
            started = _utc(record.get("upload_started_at") if status == "uploading" else
                           record.get("reserved_at") or record.get("upload_started_at"))
            if started is None:
                alert("active_missing_timestamp", str(case_id))
            elif now - started > timedelta(hours=24):
                alert("active_over_24h", str(case_id))

    if len(ready) < 2:
        alert("ready_queue_below_2")

    last_failure = None
    if research:
        research_status = research.get("status")
        if research_status == "failed":
            since = _utc(research.get("failing_since"))
            if len(ready) < READY_LOW_WATERMARK or since is None or now - since > RESEARCH_RETRY_GRACE:
                alert("research_failed")
            else:
                warnings.append({"code": "research_retrying", "detail": ""})
            checked = _utc(research.get("checked_at"))
            last_failure = {"component": "research", "at": checked.isoformat() if checked else None,
                            "status": "failed"}
        elif research_status == "partial_success" and (research.get("stopped_reason") or
                                                       (isinstance(research.get("generation_calls"), int) and
                                                        research["generation_calls"] >= 4)) and len(ready) < 2:
            alert("research_partial_exhausted")
    elif len(ready) < 2:
        alert("research_missing_for_refill")

    analytics_time = _utc(analytics.get("as_of")) if analytics else None
    if analytics_time is None:
        alert("analytics_missing_or_undated")
    elif now - analytics_time > timedelta(hours=72):
        alert("analytics_over_72h")

    if public_times and now - max(public_times) > timedelta(days=10):
        alert("no_new_public_case_over_10d")

    metrics = analytics.get("analytics", []) if analytics else []
    if not isinstance(metrics, list):
        metrics = []
    eligible = [row for row in metrics if isinstance(row, dict) and
                isinstance(row.get("engagedViews"), (int, float)) and
                not isinstance(row.get("engagedViews"), bool) and row["engagedViews"] >= MIN_ENGAGED_VIEWS]
    growth_rows = growth.get("eligible_videos", []) if growth else []
    if isinstance(growth_rows, list) and any(
            isinstance(row, dict) and (not isinstance(row.get("engagedViews"), (int, float)) or
                                       row["engagedViews"] < MIN_ENGAGED_VIEWS)
            for row in growth_rows):
        alert("growth_comparison_under_100_engaged_views")

    return {
        "checked_at": now.isoformat(), "channel_id": CHANNEL_ID,
        "status": "needs_attention" if alerts else "healthy",
        "counts": {"reviewed_stock": len(approved), "ready_queue": len(ready),
                   "confirmed_public_cases": len(public_times), "private_previews": private_count,
                   "active_reservations": active_count, "analytics_rows": len(metrics),
                   "eligible_comparisons": len(eligible)},
        "last_pipeline_failure": last_failure,
        "alerts": alerts,
        "warnings": warnings,
    }


def run(root: Path = ROOT, *, now: datetime | None = None) -> dict[str, Any]:
    report = assess(root, now=now)
    path = root / "state/health.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, suffix=".tmp", delete=False) as file:
        temporary = Path(file.name)
        json.dump(report, file, ensure_ascii=False, indent=2)
        file.write("\n")
    os.replace(temporary, path)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Check Scam Autopsy studio health")
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args()
    report = run(args.root)
    print(json.dumps(report, ensure_ascii=False))
    return 1 if report["status"] == "needs_attention" else 0


if __name__ == "__main__":
    raise SystemExit(main())
