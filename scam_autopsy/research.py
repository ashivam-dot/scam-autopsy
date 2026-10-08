"""Bounded, source-checked FTC research refill for cloud runs.

Only approved scripts are written to content/. Failure details go to state/research.json.
The Gemini API key is read from the environment and is never written or logged.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import requests
from bs4 import BeautifulSoup


ROOT = Path(__file__).resolve().parents[1]
FEED_URL = "https://consumer.ftc.gov/blog/rss"
MODELS_URL = "https://generativelanguage.googleapis.com/v1beta/models"
GENERATION_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
# Each model is listed with free input/output on Google's Gemini API pricing page.
FREE_TIER_CANDIDATES = ("gemini-3.7-flash", "gemini-3.6-flash", "gemini-3.8-flash", "gemini-3.5-flash")
MAX_GENERATION_CALLS = 4
MAX_ARTICLE_FETCHES = 10
MAX_BYTES = 1_000_000
TIMEOUT = (5, 25)
GENERATION_TIMEOUT = (10, 90)
USER_AGENT = "Mozilla/5.0 (compatible; ScamAutopsyResearch/1.0; +https://consumer.ftc.gov/)"
ALLOWED_HOSTS = {"consumer.ftc.gov", "www.ftc.gov", "www.fbi.gov", "www.ic3.gov", "www.cisa.gov"}
ARTICLE_PATH = re.compile(r"/consumer-alerts/20\d{2}/\d{2}/[a-z0-9-]+/?\Z")
NUMBER = re.compile(
    r"(?<![\w])(?P<currency>[$£€])?(?P<value>\d[\d,]*(?:\.\d+)?)"
    r"(?P<scale>\s*(?:thousand|million|billion))?(?P<percent>%|\s*percent)?(?![\w])", re.I)
WORD = re.compile(r"\b[\w’'-]+\b", re.UNICODE)
SCAM_TOPIC = re.compile(r"scam|fraud|impost|impersonat|phish|fake|deceptive", re.I)
SCAM_TITLE = re.compile(r"scam|fraud|impost|impersonat|phish|spoof|fake (?:job|listing|business|invoice|seller|store|website)", re.I)
SEASONAL_TOPIC = re.compile(r"open enrollment|national .* month|holiday|deadline|this year", re.I)


class ResearchError(RuntimeError):
    pass


class ModelUnavailable(ResearchError):
    """A listed model returned 404 from generateContent."""


class ModelTransient(ResearchError):
    """A model returned a bounded retryable service or quota status."""


def canonical_source_url(url: str) -> str:
    """Accept only exact primary-source HTTPS hosts and safe paths."""
    if not isinstance(url, str) or not url or any(c.isspace() or ord(c) < 32 for c in url):
        raise ResearchError("unsafe source URL")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as exc:
        raise ResearchError("unsafe source URL") from exc
    if (parts.scheme != "https" or parts.hostname not in ALLOWED_HOSTS or
            parts.username or parts.password or port or parts.query or parts.fragment or
            parts.netloc != parts.hostname or not parts.path.startswith("/") or
            "//" in parts.path or ".." in parts.path or "\\" in parts.path or "%" in parts.path):
        raise ResearchError("unsafe source URL")
    if parts.hostname == "consumer.ftc.gov" and not ARTICLE_PATH.fullmatch(parts.path):
        raise ResearchError("unsupported FTC article path")
    return f"https://{parts.hostname}{parts.path.rstrip('/')}"


def _get_public(session: requests.Session, url: str) -> bytes:
    """Follow at most two redirects, validating each hop before connecting."""
    if url != FEED_URL:
        url = canonical_source_url(url)
    for _ in range(3):
        response = session.get(url, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT,
                               allow_redirects=False, stream=True)
        try:
            if response.status_code in (301, 302, 303, 307, 308):
                target = response.headers.get("Location", "")
                # Feed redirects must remain on its exact public endpoint.
                if url == FEED_URL and target == FEED_URL:
                    continue
                url = canonical_source_url(target)
                continue
            response.raise_for_status()
            chunks = []
            length = 0
            for chunk in response.iter_content(chunk_size=32_768):
                length += len(chunk)
                if length > MAX_BYTES:
                    raise ResearchError("source exceeds size limit")
                chunks.append(chunk)
            return b"".join(chunks)
        finally:
            response.close()
    raise ResearchError("too many source redirects")


def discover_articles(session: requests.Session) -> list[tuple[str, str]]:
    """FTC RSS currently puts an HTML anchor in each title; its link field is malformed."""
    root = ET.fromstring(_get_public(session, FEED_URL))
    found: list[tuple[str, str]] = []
    for item in root.findall("./channel/item")[:40]:
        title_node = item.find("title")
        anchor = title_node.find("a") if title_node is not None else None
        title = " ".join(title_node.itertext()).strip() if title_node is not None else ""
        url = anchor.get("href", "") if anchor is not None else (item.findtext("link") or "")
        try:
            url = canonical_source_url(url)
        except ResearchError:
            continue
        description = item.findtext("description") or ""
        relevant = SCAM_TITLE.search(title) or ("qr code" in title.lower() and SCAM_TOPIC.search(description))
        if relevant and not SEASONAL_TOPIC.search(title):
            found.append((url, title))
    if not found:
        raise ResearchError("FTC feed has no usable scam articles")
    return found


def extract_article(html: bytes) -> str:
    soup = BeautifulSoup(html, "html.parser")
    article = soup.select_one("main article.node--view-mode-cfg-default .field--name-body")
    if article is None:
        raise ResearchError("FTC article body unavailable")
    for element in article.select("script, style, noscript, svg, form, nav, aside, [hidden], [aria-hidden=true], [style*='display:none'], .visually-hidden, .usa-sr-only"):
        element.decompose()
    text = " ".join(article.stripped_strings)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) < 300:
        raise ResearchError("FTC article body too short")
    return text


def _model_names(session: requests.Session, key: str) -> tuple[str, ...]:
    response = session.get(MODELS_URL, headers={"x-goog-api-key": key}, timeout=TIMEOUT,
                           params={"pageSize": 1000})
    response.raise_for_status()
    data = response.json()
    if data.get("nextPageToken"):
        raise ResearchError("Gemini model list is incomplete")
    supported = {str(model.get("name", "")).removeprefix("models/") for model in data.get("models", [])
                 if "generateContent" in model.get("supportedGenerationMethods", [])}
    chosen = [name for name in FREE_TIER_CANDIDATES if name in supported]
    if len(chosen) < 2:
        raise ResearchError("two configured free-tier Gemini models are unavailable")
    return tuple(chosen)


def _gemini_json(session: requests.Session, key: str, model: str, prompt: str,
                 budget: list[int], temperature: float) -> dict[str, Any]:
    if model not in FREE_TIER_CANDIDATES:
        raise ResearchError("unconfigured Gemini model")
    payload = {"contents": [{"parts": [{"text": prompt}]}],
               "generationConfig": {"responseMimeType": "application/json", "temperature": temperature,
                                    "maxOutputTokens": 6000,
                                    "thinkingConfig": {"thinkingLevel": "low"}}}
    if budget[0] >= MAX_GENERATION_CALLS:
        raise ResearchError("Gemini generation budget exhausted")
    budget[0] += 1
    response = session.post(GENERATION_URL.format(model=model), headers={"x-goog-api-key": key},
                            json=payload, timeout=GENERATION_TIMEOUT)
    if response.status_code == 404:
        raise ModelUnavailable("Gemini listed model unavailable for generation")
    if response.status_code in (429, 502, 503, 504):
        raise ModelTransient(f"Gemini model returned HTTP {response.status_code}")
    response.raise_for_status()
    data = response.json()
    candidates = data.get("candidates") or []
    if not candidates or candidates[0].get("finishReason") != "STOP":
        raise ResearchError("Gemini generation blocked or incomplete")
    parts = candidates[0].get("content", {}).get("parts", [])
    try:
        result = json.loads("".join(part.get("text", "") for part in parts))
    except (ValueError, TypeError) as exc:
        raise ResearchError("Gemini returned invalid JSON") from exc
    if not isinstance(result, dict):
        raise ResearchError("Gemini returned a non-object")
    return result


def _generate_available(session: requests.Session, key: str, models: tuple[str, ...],
                        unavailable: dict[str, str], prompt: str, budget: list[int],
                        temperature: float, temporary: set[str], delayed: list[bool],
                        *, exclude: str = "", reserve_calls: int = 0
                        ) -> tuple[dict[str, Any], str]:
    for model in models:
        if model == exclude or model in unavailable or model in temporary:
            continue
        if reserve_calls and not any(other != model and other not in unavailable and
                                     other not in temporary for other in models):
            raise ResearchError("no distinct Gemini reviewer model remains")
        if budget[0] + 1 + reserve_calls > MAX_GENERATION_CALLS:
            raise ResearchError("Gemini generation budget cannot cover separate review")
        try:
            return _gemini_json(session, key, model, prompt, budget, temperature), model
        except ModelUnavailable:
            unavailable[model] = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
        except (ModelTransient, requests.exceptions.ReadTimeout):
            temporary.add(model)
            if not delayed[0]:
                time.sleep(2)
                delayed[0] = True
    raise ResearchError("two distinct available free-tier Gemini models are required")


def _writer_prompt(url: str, title: str, source: str) -> str:
    return f"""Create ONE original evergreen English YouTube Short about the documented scam pattern in this FTC article.
Return only a JSON object with id,title,hook,source_urls,description,format,scenes. Title at most 100 characters; description at most 500 characters. Exactly 7 scenes; each scene has narration,heading,visual,items,label,evidence_quote. Aim for 105–115 total spoken narration words; 90–125 are acceptable for a 35–55 second Short. Use 1–3 short on-screen items and concise headings. Vary narrative rhythm and visual metaphors; visual can say phone, flow, payment, profile, receipt, warning, or cards. Show the mechanism, trust transfer, consequence, and practical independently verifiable protective action. Source URL must be exactly [{json.dumps(url)}]; format is short. Call it an FTC-documented pattern or warning, not a specific victim incident, unless the source directly documents one. No invented victims, names, losses, screenshots, outcomes, or quoted messages. Label every fictional UI or reenactment Illustration; label a factual source card SOURCE: FTC. Every scene's evidence_quote must be a short EXACT contiguous passage from the visible FTC article below supporting that scene's factual claims. Do not reproduce article prose in narration. Any numerical claim, including one spelled out in words, must appear in that scene's evidence_quote. Avoid time-sensitive advice and unverifiable superlatives. Description includes the source URL and says FTC-documented pattern.

FTC article title: {title}
FTC source URL: {url}
Visible FTC article text:
{source[:14000]}"""


def _reviewer_prompt(case: dict[str, Any], url: str, source: str) -> str:
    return f"""Independently audit this proposed YouTube Short against the primary FTC article below. Return JSON only with boolean fields factual_fidelity, practical_advice, quality, diversity, no_invented_incidents, and a short notes string. Set each false unless fully supported. Check EVERY factual claim in the hook, title, description, narration, headings and on-screen items against the source. Reject false certainty, unsourced numbers (including quantities written as words), unsafe or vague advice, invented real victims/losses/screenshots/quotes, seasonal framing, and repeated or generic scenes. Evidence quotes must truly support corresponding scenes. Fictional UI must be visibly labeled Illustration. Review editorial quality and useful mechanism explanation.

Article URL: {url}
Visible article text:
{source[:14000]}

Proposed script:
{json.dumps(case, ensure_ascii=False)}"""


def _numbers(value: str) -> set[tuple[str, str, str, str]]:
    results = set()
    for match in NUMBER.finditer(value):
        raw = match.group("value").replace(",", "")
        number = raw.rstrip("0").rstrip(".") if "." in raw else raw.lstrip("0") or "0"
        results.add((number, match.group("currency") or "",
                     (match.group("scale") or "").strip().lower(),
                     "percent" if match.group("percent") else ""))
    return results


def _text_field(value: Any, name: str, max_len: int = 500) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > max_len:
        raise ResearchError(f"invalid {name}")
    return value.strip()


def validate_script(case: dict[str, Any], url: str, source: str) -> dict[str, Any]:
    if case.get("format") != "short" or case.get("source_urls") != [url]:
        raise ResearchError("script source or format invalid")
    for key, length in (("id", 80), ("title", 100), ("hook", 160), ("description", 500)):
        _text_field(case.get(key), key, length)
    if url not in case["description"] or "FTC" not in case["description"]:
        raise ResearchError("description lacks source cue")
    scenes = case.get("scenes")
    if not isinstance(scenes, list) or len(scenes) != 7:
        raise ResearchError("script needs seven scenes")
    narration = []
    for index, scene in enumerate(scenes, 1):
        if not isinstance(scene, dict):
            raise ResearchError("invalid scene")
        for key in ("narration", "heading", "visual", "label", "evidence_quote"):
            _text_field(scene.get(key), f"scene {index} {key}")
        if scene["label"] not in ("Illustration", "SOURCE: FTC"):
            raise ResearchError("scene label invalid")
        if len(scene["heading"]) > 42:
            raise ResearchError("scene heading too long")
        items = scene.get("items")
        if not isinstance(items, list) or not 1 <= len(items) <= 3 or any(
                not isinstance(item, str) or not item.strip() or len(item) > 45 for item in items):
            raise ResearchError("scene items invalid")
        quote = scene["evidence_quote"]
        if quote not in source:
            raise ResearchError("evidence quote absent from article")
        display = " ".join([scene["narration"], scene["heading"], scene["visual"], *items])
        if not _numbers(display).issubset(_numbers(quote)):
            raise ResearchError("scene numeric claim lacks matching evidence quote")
        narration.append(scene["narration"])
    if (len({scene["heading"] for scene in scenes}) != 7 or
            len({scene["narration"] for scene in scenes}) != 7 or
            len({scene["visual"] for scene in scenes}) < 3):
        raise ResearchError("scenes lack distinct writing and visuals")
    count = len(WORD.findall(" ".join(narration)))
    if not 90 <= count <= 125:
        raise ResearchError(f"narration word count {count} outside 90-125")
    metadata = " ".join(case[k] for k in ("title", "hook", "description"))
    metadata = metadata.replace(url, "")
    if not _numbers(metadata).issubset(_numbers(source)):
        raise ResearchError("metadata numeric claim absent from article")
    return case


def _existing(content_dir: Path, state: dict[str, Any]) -> tuple[set[str], set[str], set[str]]:
    urls = set(state.get("used_source_urls", []))
    hashes = set(state.get("used_source_hashes", []))
    ids: set[str] = set()
    for path in content_dir.glob("*.json"):
        case = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(case, dict) or not isinstance(case.get("source_urls", []), list):
            raise ResearchError(f"invalid existing content: {path.name}")
        ids.add(str(case.get("id", "")))
        for url in case.get("source_urls", []):
            try:
                urls.add(canonical_source_url(url))
            except ResearchError:
                continue
    return urls, hashes, ids


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, suffix=".tmp", delete=False) as file:
        temp = Path(file.name)
        json.dump(value, file, ensure_ascii=False, indent=2)
        file.write("\n")
    os.replace(temp, path)


def run_research(limit: int = 2, *, content_dir: Path = ROOT / "content",
                 state_file: Path = ROOT / "state" / "research.json",
                 session: requests.Session | None = None, key: str | None = None) -> list[Path]:
    if not 1 <= limit <= 2:
        raise ValueError("limit must be 1 or 2")
    session = session or requests.Session()
    key = key if key is not None else os.environ.get("GEMINI_API_KEY", "")
    try:
        state = json.loads(state_file.read_text(encoding="utf-8")) if state_file.exists() else {}
        if not isinstance(state, dict) or any(
                not isinstance(state.get(field, []), list) or
                any(not isinstance(item, str) for item in state.get(field, []))
                for field in ("used_source_urls", "used_source_hashes")) or not isinstance(
                    state.get("model_unavailable_until", {}), dict):
            raise ValueError("invalid research state")
    except (ValueError, OSError) as exc:
        _write_json(state_file, {"status": "failed", "error": f"invalid research state: {exc}",
                                 "checked_at": datetime.now(timezone.utc).isoformat(),
                                 "generation_calls": 0})
        return []
    state = {k: v for k, v in state.items() if k in ("used_source_urls", "used_source_hashes",
                                                    "model_unavailable_until")}
    checked = datetime.now(timezone.utc)
    unavailable: dict[str, str] = {}
    for model, until in state.get("model_unavailable_until", {}).items():
        try:
            parsed = datetime.fromisoformat(until)
            if model in FREE_TIER_CANDIDATES and parsed.tzinfo and parsed > checked:
                unavailable[model] = parsed.isoformat()
        except (TypeError, ValueError):
            continue
    state["model_unavailable_until"] = unavailable
    state["checked_at"] = checked.isoformat()
    budget = [0]
    try:
        if not key:
            raise ResearchError("GEMINI_API_KEY is unavailable")
        used_urls, used_hashes, used_ids = _existing(content_dir, state)
        models = _model_names(session, key)
        if len([model for model in models if model not in unavailable]) < 2:
            raise ResearchError("two configured free-tier Gemini models are unavailable")
        pending: list[tuple[Path, dict[str, Any], str, str]] = []
        rejected: list[dict[str, str]] = []
        stopped_reason = ""
        temporary_unavailable: set[str] = set()
        delayed = [False]
        fetched = 0
        for url, title in discover_articles(session):
            if len(pending) >= limit or fetched >= MAX_ARTICLE_FETCHES:
                break
            if url in used_urls:
                continue
            if budget[0] + 2 > MAX_GENERATION_CALLS:
                stopped_reason = "remaining Gemini calls cannot cover writer and reviewer"
                break
            fetched += 1
            try:
                source = extract_article(_get_public(session, url))
            except (ResearchError, requests.RequestException) as exc:
                rejected.append({"source_url": url, "reason": f"source unavailable: {str(exc)[:150]}"})
                continue
            digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
            if digest in used_hashes:
                continue
            try:
                candidate, writer_model = _generate_available(
                    session, key, models, unavailable, _writer_prompt(url, title, source), budget, 0.6,
                    temporary_unavailable, delayed, reserve_calls=1)
            except (ResearchError, requests.RequestException) as exc:
                stopped_reason = str(exc)[:200]
                break
            try:
                validate_script(candidate, url, source)
            except ResearchError as exc:
                rejected.append({"source_url": url, "reason": f"writer validation: {exc}"})
                continue
            # The model controls prose only; ignore any additional generated fields.
            candidate = {key: candidate[key] for key in
                         ("id", "title", "hook", "source_urls", "description", "format", "scenes")}
            candidate["scenes"] = [{key: scene[key] for key in
                                    ("narration", "heading", "visual", "items", "label", "evidence_quote")}
                                   for scene in candidate["scenes"]]
            try:
                review, reviewer_model = _generate_available(
                    session, key, models, unavailable, _reviewer_prompt(candidate, url, source), budget, 0.1,
                    temporary_unavailable, delayed, exclude=writer_model)
            except (ResearchError, requests.RequestException) as exc:
                stopped_reason = str(exc)[:200]
                break
            gates = ("factual_fidelity", "practical_advice", "quality", "diversity", "no_invented_incidents")
            failed_gates = [gate for gate in gates if review.get(gate) is not True]
            if failed_gates:
                rejected.append({"source_url": url, "reason": "reviewer rejected: " + ", ".join(failed_gates)})
                continue
            try:
                review_notes = _text_field(review.get("notes"), "review notes", 1000)
            except ResearchError as exc:
                rejected.append({"source_url": url, "reason": str(exc)})
                continue
            case_id = "ftc-" + hashlib.sha256(url.encode("utf-8")).hexdigest()[:12]
            if case_id in used_ids:
                rejected.append({"source_url": url, "reason": "script ID already exists"})
                continue
            candidate["id"] = case_id
            candidate["approved"] = True
            candidate["review"] = {"reviewer": reviewer_model, "writer": writer_model,
                                   "source_checked_at": state["checked_at"], "notes": review_notes,
                                   "checks": {gate: True for gate in gates}, "source_sha256": digest}
            pending.append((content_dir / f"{case_id}.json", candidate, url, digest))
            used_urls.add(url)
            used_hashes.add(digest)
            used_ids.add(case_id)
        if not pending:
            status = "failed" if rejected or stopped_reason else "no_new_script"
            state.update(status=status, generation_calls=budget[0], rejected=rejected,
                         error=stopped_reason or ("no candidate passed review" if rejected else ""))
            _write_json(state_file, state)
            return []
        # Candidate-level rejection does not erase a previously approved script.
        for path, case, _, _ in pending:
            if path.exists():
                raise ResearchError("content output already exists")
            _write_json(path, case)
        state["used_source_urls"] = sorted(used_urls)
        state["used_source_hashes"] = sorted(used_hashes)
        state.update(status="partial_success" if rejected or stopped_reason else "passed",
                     generated=len(pending), generation_calls=budget[0], rejected=rejected,
                     stopped_reason=stopped_reason)
        _write_json(state_file, state)
        return [path for path, _, _, _ in pending]
    except (ResearchError, requests.RequestException, ET.ParseError, ValueError,
            KeyError, TypeError, AttributeError) as exc:
        state.update(status="failed", error=str(exc)[:300], generation_calls=budget[0])
        _write_json(state_file, state)
        return []


def main() -> int:
    parser = argparse.ArgumentParser(description="Refill reviewed FTC Scam Autopsy Shorts")
    parser.add_argument("--limit", type=int, default=2, choices=(1, 2))
    args = parser.parse_args()
    paths = run_research(args.limit)
    print(json.dumps({"generated": [str(path) for path in paths]}))
    if paths:
        return 0
    state = json.loads((ROOT / "state" / "research.json").read_text(encoding="utf-8"))
    return 1 if state.get("status") == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
