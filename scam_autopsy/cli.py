"""Cloud queue with a durable reservation and conservative crash recovery."""
from __future__ import annotations
import argparse
import json
import hashlib
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from . import youtube

ROOT = Path(__file__).resolve().parents[1]
LEDGER = ROOT / "state/publishing.json"


def now():
    return datetime.now(timezone.utc).isoformat()


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")
    temporary.replace(path)


def read():
    return json.loads(LEDGER.read_text()) if LEDGER.exists() else {"channel_id": youtube.CHANNEL_ID, "cases": {}}


def content_hash(case):
    return hashlib.sha256(json.dumps({k: v for k, v in case.items() if k != "path"}, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def durable_save(state, message):
    write(LEDGER, state)
    if os.environ.get("GITHUB_ACTIONS") == "true":
        subprocess.run(["git", "add", "state/publishing.json"], cwd=ROOT, check=True)
        changed = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=ROOT).returncode
        if changed:
            subprocess.run(["git", "commit", "-m", message], cwd=ROOT, check=True)
            subprocess.run(["git", "push", "origin", "HEAD:main"], cwd=ROOT, check=True)


def reserve(case_id=None):
    state = read()
    cases = [youtube.validate(json.loads(p.read_text())) | {"path": str(p.relative_to(ROOT))} for p in sorted((ROOT / "content").glob("*.json")) if json.loads(p.read_text()).get("approved")]
    active = [i for i, r in state["cases"].items() if r["status"] in ("reserved", "uploading", "processing")]
    if len(active) > 1:
        raise RuntimeError("Multiple active reservations; refusing new work")
    if active:
        if case_id and case_id != active[0]:
            raise RuntimeError("An unfinished case must reconcile before selecting another")
        case_id = active[0]
    selected = next((c for c in cases if (not case_id or c["id"] == case_id) and state["cases"].get(c["id"], {}).get("status") != "published" and (case_id or state["cases"].get(c["id"], {}).get("status") != "private")), None)
    if selected is None:
        if case_id:
            raise RuntimeError("Case absent or already published")
        return None
    case_id = selected["id"]
    if case_id not in state["cases"]:
        state["cases"][case_id] = {"status": "reserved", "reserved_at": now(), "path": selected["path"], "content_sha256": content_hash(selected)}
        durable_save(state, "Reserve case " + case_id)
    if state["cases"][case_id].get("content_sha256") != content_hash(selected):
        raise RuntimeError("Reserved source/script changed; require a new reviewed case ID")
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as out:
            out.write("case=" + case_id + "\npath=" + selected["path"] + "\n")
    print(json.dumps({"case": case_id, "path": selected["path"], "status": state["cases"][case_id]["status"]}))
    return selected


def publish(path, media, make_public=True):
    case = youtube.validate(json.loads(Path(path).read_text()))
    state = read()
    record = state["cases"].get(case["id"])
    if not record:
        raise RuntimeError("Case must be durably reserved before uploading")
    if record.get("content_sha256") != content_hash(case):
        raise RuntimeError("Script differs from durably reserved content")
    api = youtube.client()
    channel = youtube.identity(api)
    existing = youtube.find_case(api, channel, case)
    if existing:
        video_id = existing["id"]
    elif record.get("video_id"):
        video_id = record["video_id"]
    else:
        if record["status"] == "uploading":
            raise RuntimeError("Prior upload result is ambiguous; inspect channel before retrying")
        record.update(status="uploading", upload_started_at=now())
        durable_save(state, "Record upload attempt " + case["id"])
        video_id = youtube.upload_private(api, case, Path(media))
        record.update(status="processing", video_id=video_id)
        durable_save(state, "Record private upload " + case["id"])
    receipt = youtube.finalize(api, case, video_id, publish=make_public)
    if make_public:
        try:
            receipt["playlist_id"] = youtube.file_in_playlist(api, video_id)
        except Exception as exc:
            receipt["playlist_error"] = type(exc).__name__
    record.update(receipt, status="published" if make_public else "private", verified_at=now())
    durable_save(state, "Verify " + receipt["privacy"] + " case " + case["id"])
    print(json.dumps(receipt))


def stats():
    report = youtube.snapshot()
    write(ROOT / "state/analytics-latest.json", report)
    write(ROOT / "reports" / (report["as_of"] + ".json"), report)
    metrics = report["analytics"]
    eligible = [r for r in metrics if r.get("engagedViews", 0) >= 100]
    ranked = sorted(eligible, key=lambda r: (r.get("subscribersGained", 0) - r.get("subscribersLost", 0)) / max(r.get("engagedViews", 1), 1), reverse=True)
    write(ROOT / "state/growth.json", {"as_of": report["as_of"], "minimum_engaged_views": 100, "eligible_videos": ranked, "decision": "Insufficient evidence; maintain test cadence" if not ranked else "Develop a deeper investigation of the strongest subscriber-conversion topic; source and review separately", "automatic_title_changes": False})
    print(json.dumps({"channel": report["channel"], "measured_videos": len(metrics), "qualified_for_comparison": len(eligible)}))


def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="action", required=True)
    q = sub.add_parser("reserve"); q.add_argument("--case")
    q = sub.add_parser("publish"); q.add_argument("path"); q.add_argument("media"); q.add_argument("--private", action="store_true")
    sub.add_parser("stats")
    args = p.parse_args()
    if args.action == "reserve": reserve(args.case)
    elif args.action == "publish": publish(args.path, args.media, not args.private)
    else: stats()


if __name__ == "__main__":
    main()
