"""Read-only, bounded decision for the daily reviewed-script refill."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from pathlib import Path

from .health import ROOT, assess


READY_LOW_WATERMARK = 4
READY_CAP = 8
MAX_SCRIPTS_PER_RUN = 2  # research.py enforces this and four model calls.


@dataclass(frozen=True)
class Decision:
    refill: bool
    limit: int
    ready_queue: int
    reasons: tuple[str, ...]


def decide(*, today: date, force: bool, research_status: str | None, ready_queue: int) -> Decision:
    """Keep daily checks active while capping the queue before each refill."""
    if ready_queue < 0:
        raise ValueError("ready_queue must be nonnegative")
    reasons = []
    if today.weekday() == 0:
        reasons.append("monday")
    if force:
        reasons.append("forced")
    if research_status == "failed":
        reasons.append("previous_research_failed")
    if ready_queue < READY_LOW_WATERMARK:
        reasons.append("low_stock")
    limit = min(MAX_SCRIPTS_PER_RUN, max(0, READY_CAP - ready_queue)) if reasons else 0
    return Decision(limit > 0, limit, ready_queue, tuple(reasons))


def _research_status(root: Path) -> str | None:
    path = root / "state" / "research.json"
    if not path.exists():
        return None
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "failed"
    return state.get("status") if isinstance(state, dict) and isinstance(state.get("status"), str) else None


def main() -> int:
    force = os.environ.get("FORCE_REFILL", "").strip().lower() == "true"
    ready_queue = assess(ROOT)["counts"]["ready_queue"]
    decision = decide(today=datetime.now(timezone.utc).date(), force=force,
                      research_status=_research_status(ROOT), ready_queue=ready_queue)
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as stream:
            stream.write(f"refill={'true' if decision.refill else 'false'}\n")
            stream.write(f"limit={decision.limit}\n")
    print(json.dumps(asdict(decision)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
