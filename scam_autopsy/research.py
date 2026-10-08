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
FREE_TIER_CANDIDATES = ("gemini-3.7-flash", "gemini-3.6-flash", "gemini-3.5-flash",
                        "gemini-3.5-flash-lite", "gemini-3.1-flash-lite", "gemini-3.8-flash")
REVIEWER_PREFERENCE = ("gemini-3.5-flash", "gemini-3.7-flash", "gemini-3.6-flash",
                       "gemini-3.8-flash")
MAX_GENERATION_CALLS = 4
MAX_ARTICLE_FETCHES = 10
MAX_BYTES = 1_000_000
MAX_CITATION_SOURCE_CHARS = 14_000
MAX_CITATION_PASSAGES = 48
MAX_CITATION_CHARS = 500
CITATION_OVERLAP = 100
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

    def __init__(self, status_code: int | None = None):
        self.status_code = status_code if status_code in (429, 502, 503, 504) else None
        super().__init__("Gemini temporarily unavailable")


def _safe_error(exc: Exception) -> str:
    """Request exception strings may contain URLs or headers; never persist them."""
    if isinstance(exc, requests.RequestException):
        return type(exc).__name__
    return str(exc)[:200]


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


def _source_passages(source: str) -> tuple[str, ...]:
    """Offer bounded, overlapping literal article windows for model-selected citations."""
    text = source[:MAX_CITATION_SOURCE_CHARS]
    passages: list[str] = []
    start = 0
    while start < len(text) and len(passages) < MAX_CITATION_PASSAGES:
        end = min(start + MAX_CITATION_CHARS, len(text))
        if end < len(text):
            boundary = text.rfind(" ", start + MAX_CITATION_CHARS - CITATION_OVERLAP, end)
            if boundary > start:
                end = boundary
        passage = text[start:end].strip()
        if passage:
            passages.append(passage)
        if end == len(text):
            break
        start = max(start + 1, end - CITATION_OVERLAP)
        while start < end and not text[start - 1].isspace():
            start += 1
    return tuple(passages)


def _materialize_scene_quotes(case: dict[str, Any], passages: tuple[str, ...]) -> None:
    """Replace citation IDs with server-owned text; legacy literal quotes stay exact-checked."""
    scenes = case.get("scenes")
    if not isinstance(scenes, list):
        return
    for index, scene in enumerate(scenes, 1):
        if not isinstance(scene, dict) or "evidence_quote_id" not in scene:
            continue
        quote_id = scene.pop("evidence_quote_id")
        if type(quote_id) is not int or not 1 <= quote_id <= len(passages):
            raise ResearchError(f"scene {index} invalid evidence quote ID")
        scene["evidence_quote"] = passages[quote_id - 1]


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


def _deprioritize_recent_transients(models: tuple[str, ...], prior_state: dict[str, Any],
                                    checked: datetime) -> tuple[str, ...]:
    """Use other free model quotas first after a recent 429/service outage."""
    try:
        previous = datetime.fromisoformat(prior_state.get("checked_at", ""))
        if previous.tzinfo is None or not timedelta(0) <= checked - previous <= timedelta(hours=6):
            return models
    except (TypeError, ValueError):
        return models
    attempts = prior_state.get("generation_attempts", [])
    if not isinstance(attempts, list):
        return models
    cooling = {attempt.get("model") for attempt in attempts if isinstance(attempt, dict) and
               attempt.get("status") in ("transient", "read_timeout")}
    return tuple(model for model in models if model not in cooling) + tuple(
        model for model in models if model in cooling)


def _gemini_json(session: requests.Session, key: str, model: str, prompt: str,
                 budget: list[int], temperature: float) -> dict[str, Any]:
    if model not in FREE_TIER_CANDIDATES:
        raise ResearchError("unconfigured Gemini model")
    payload = {"contents": [{"parts": [{"text": prompt}]}],
               "generationConfig": {"responseMimeType": "application/json", "temperature": temperature,
                                    "maxOutputTokens": 6000}}
    if "flash-lite" not in model:
        payload["generationConfig"]["thinkingConfig"] = {"thinkingLevel": "low"}
    if budget[0] >= MAX_GENERATION_CALLS:
        raise ResearchError("Gemini generation budget exhausted")
    budget[0] += 1
    response = session.post(GENERATION_URL.format(model=model), headers={"x-goog-api-key": key},
                            json=payload, timeout=GENERATION_TIMEOUT)
    if response.status_code == 404:
        raise ModelUnavailable("Gemini listed model unavailable for generation")
    if response.status_code in (429, 502, 503, 504):
        raise ModelTransient(response.status_code)
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
                        attempts: list[dict[str, str]], role: str,
                        *, exclude: str = "", reserve_calls: int = 0,
                        reviewer_pool: tuple[str, ...] = ()
                        ) -> tuple[dict[str, Any], str]:
    for model in models:
        if model == exclude or model in unavailable or model in temporary:
            continue
        possible_reviewers = reviewer_pool or models
        if reserve_calls and not any(other != model and other not in unavailable and
                                     other not in temporary for other in possible_reviewers):
            last = attempts[-1] if attempts else None
            cause = f"; last {last['model']} {last['detail']}" if last and last["status"] != "success" else ""
            raise ResearchError("no distinct Gemini reviewer model remains" + cause)
        if budget[0] + 1 + reserve_calls > MAX_GENERATION_CALLS:
            last = attempts[-1] if attempts else None
            cause = f"; last {last['model']} {last['detail']}" if last and last["status"] != "success" else ""
            raise ResearchError("Gemini generation budget cannot cover separate review" + cause)
        try:
            result = _gemini_json(session, key, model, prompt, budget, temperature)
            attempts.append({"model": model, "status": "success", "detail": role})
            return result, model
        except ModelUnavailable:
            attempts.append({"model": model, "status": "not_found", "detail": f"{role}: HTTP 404"})
            unavailable[model] = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
        except ModelTransient as exc:
            detail = f"{role}: HTTP {exc.status_code}" if exc.status_code else f"{role}: retryable HTTP error"
            attempts.append({"model": model, "status": "transient", "detail": detail})
            temporary.add(model)
            if not delayed[0]:
                time.sleep(2)
                delayed[0] = True
        except requests.exceptions.ReadTimeout:
            attempts.append({"model": model, "status": "read_timeout", "detail": role})
            temporary.add(model)
            if not delayed[0]:
                time.sleep(2)
                delayed[0] = True
        except ResearchError as exc:
            attempts.append({"model": model, "status": "generation_error", "detail": f"{role}: {_safe_error(exc)}"})
            raise
        except requests.RequestException as exc:
            code = getattr(getattr(exc, "response", None), "status_code", None)
            detail = f"HTTP {code}" if isinstance(code, int) and 100 <= code <= 599 else type(exc).__name__
            attempts.append({"model": model, "status": "request_error", "detail": f"{role}: {detail}"})
            raise
    last = attempts[-1] if attempts else None
    cause = f"; last {last['model']} {last['detail']}" if last and last["status"] != "success" else ""
    raise ResearchError("two distinct available free-tier Gemini models are required" + cause)


def _writer_prompt(url: str, title: str, passages: tuple[str, ...]) -> str:
    citations = "\n".join(f"[{index}] {json.dumps(passage, ensure_ascii=False)}"
                          for index, passage in enumerate(passages, 1))
    return f"""Create ONE original evergreen English YouTube Short about the documented scam pattern in this FTC article.
Return only a JSON object with id,title,hook,source_urls,description,format,scenes. Title at most 100 characters; description at most 500 characters. Exactly 7 scenes; each scene has narration,heading,visual,items,label,evidence_quote_id. Set evidence_quote_id to an INTEGER from the numbered source passages below. Do not write evidence_quote text; the server copies the selected passage verbatim. Aim for 105–115 total spoken narration words; 90–125 are acceptable for a 35–55 second Short. Use 1–3 short on-screen items and concise headings. Vary narrative rhythm and visual metaphors; visual can say phone, flow, payment, profile, receipt, warning, or cards. Show the mechanism, trust transfer, consequence, and practical independently verifiable protective action. Source URL must be exactly [{json.dumps(url)}]; format is short. Call it an FTC-documented pattern or warning, not a specific victim incident, unless the source directly documents one. Description includes the source URL and says FTC-documented pattern.

The first scene must open with a concrete, spoken hook about the dangerous turn. Set hook to that exact opening text, and begin scenes[0].narration with hook verbatim. Do not use a generic warning as the hook. For EVERY scene, select ONE numbered passage that supports ALL factual clauses in its narration, heading, visual implication, and EVERY item. If one passage cannot support all the scene's claims, remove or simplify those claims. Never infer logos, spoofed websites, screenshots, technical methods, real victims, losses, outcomes, message wording, or adjectives like sophisticated unless the selected passage states them. Do not reproduce article prose in narration. Any numerical claim, including one spelled out in words, must appear in the selected passage.

Preserve the source's exact scope and conditions. If the article says scammers stop answering OR give excuses, do not say they always cut off contact. If advice warns against a seller who says you can ONLY pay by wire, gift card, crypto, or payment app, do not turn it into a blanket ban on wires. Preserve "may," "typically," "usually," and all stated alternatives. Give advice only when the quote supports it. Label every fictional phone, profile, payment screen, invoice, or reenactment Illustration. Use SOURCE: FTC only for an explicit FTC source card showing sourced wording, never for a recreated interface or payment card. Avoid time-sensitive advice and unverifiable superlatives.

FTC article title: {title}
FTC source URL: {url}
Numbered exact passages from the visible FTC article (cite IDs only):
{citations}"""


def _reviewer_prompt(case: dict[str, Any], url: str, source: str) -> str:
    return f"""Independently audit this proposed YouTube Short against the primary FTC article below. Return JSON only with boolean fields factual_fidelity, practical_advice, quality, diversity, no_invented_incidents, scene_evidence_complete, conditions_preserved, visual_disclosure, a short notes string, and first_failure_scene (integer 1–7 for a scene failure, otherwise null). Set each boolean false unless fully supported. Check EVERY claim in the hook, title, description, every scene narration and heading, every visual implication, and EVERY on-screen item. For scene_evidence_complete, compare EACH scene's own exact evidence_quote with ALL claims in that scene; one unsupported clause or item makes it false. Check the source itself too. Unsupported details such as hijacked logos, spoofed websites, or a special technical method make factual_fidelity and scene_evidence_complete false even if the article describes ordinary impersonation. Unsupported evaluative adjectives like sophisticated also fail factual_fidelity.

For conditions_preserved, reject changed scope, certainty, or alternatives: "stops answering OR makes an excuse" cannot become "always cuts off contact"; "never pay anyone who says you can ONLY pay by wire, gift card, crypto, or payment app" cannot become a blanket "no wires" rule. Preserve may, typically, usually, and only. For visual_disclosure, reject SOURCE: FTC on any recreated phone, profile, invoice, payment card or other fictional interface; such visuals need Illustration. SOURCE: FTC is for an explicit factual FTC source card only. Verify the first scene actually speaks the concrete hook verbatim. Reject unsafe or vague advice, invented real victims/losses/screenshots/quotes, unsourced numbers including words, seasonal framing, and generic or repeated scenes. A single failure must set its relevant field false. Do not rubber-stamp; notes should identify a specific checked passage or the first rejection.

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
        if (scene["label"] != "Illustration" and
                re.search(r"\b(phone|profile|payment|invoice|account|message|sms|chat|text)\b",
                          scene["visual"], re.I)):
            raise ResearchError(f"scene {index} fictional UI visual requires Illustration label")
        if len(scene["heading"]) > 42:
            raise ResearchError(f"scene {index} heading too long")
        items = scene.get("items")
        if not isinstance(items, list) or not 1 <= len(items) <= 3 or any(
                not isinstance(item, str) or not item.strip() or len(item) > 45 for item in items):
            raise ResearchError(f"scene {index} items invalid")
        quote = scene["evidence_quote"]
        if quote not in source:
            raise ResearchError(f"scene {index} evidence quote absent from article")
        display = " ".join([scene["narration"], scene["heading"], scene["visual"], *items])
        if not _numbers(display).issubset(_numbers(quote)):
            raise ResearchError(f"scene {index} numeric claim lacks matching evidence quote")
        narration.append(scene["narration"])
    if not scenes[0]["narration"].startswith(case["hook"]):
        raise ResearchError("first scene must speak hook verbatim")
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
        _write_json(state_file, {"status": "failed", "error": "invalid research state",
                                 "checked_at": datetime.now(timezone.utc).isoformat(),
                                 "generation_calls": 0})
        return []
    prior_state = state
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
    attempts: list[dict[str, str]] = []
    state["generation_attempts"] = attempts
    try:
        if not key:
            raise ResearchError("GEMINI_API_KEY is unavailable")
        used_urls, used_hashes, used_ids = _existing(content_dir, state)
        models = _deprioritize_recent_transients(_model_names(session, key), prior_state, checked)
        reviewer_models = tuple(model for model in REVIEWER_PREFERENCE if model in models)
        writer_models = tuple(model for model in models if "flash-lite" in model) + tuple(
            model for model in models if "flash-lite" not in model)
        state["model_order"] = list(models)
        state["writer_order"] = list(writer_models)
        state["reviewer_order"] = list(reviewer_models)
        if (len([model for model in models if model not in unavailable]) < 2 or
                not any(model not in unavailable for model in reviewer_models)):
            raise ResearchError("distinct free-tier Gemini writer and non-Lite reviewer are unavailable")
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
                rejected.append({"source_url": url, "reason": f"source unavailable: {_safe_error(exc)}"})
                continue
            digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
            if digest in used_hashes:
                continue
            passages = _source_passages(source)
            try:
                candidate, writer_model = _generate_available(
                    session, key, writer_models, unavailable, _writer_prompt(url, title, passages), budget, 0.6,
                    temporary_unavailable, delayed, attempts, "writer", reserve_calls=1,
                    reviewer_pool=reviewer_models)
            except (ResearchError, requests.RequestException) as exc:
                stopped_reason = _safe_error(exc)
                break
            try:
                _materialize_scene_quotes(candidate, passages)
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
                    session, key, reviewer_models, unavailable, _reviewer_prompt(candidate, url, source), budget, 0.1,
                    temporary_unavailable, delayed, attempts, "reviewer", exclude=writer_model)
            except (ResearchError, requests.RequestException) as exc:
                stopped_reason = _safe_error(exc)
                break
            gates = ("factual_fidelity", "practical_advice", "quality", "diversity",
                     "no_invented_incidents", "scene_evidence_complete", "conditions_preserved",
                     "visual_disclosure")
            failed_gates = [gate for gate in gates if review.get(gate) is not True]
            if failed_gates:
                failure_scene = review.get("first_failure_scene")
                location = (f"scene {failure_scene}: " if type(failure_scene) is int and
                            1 <= failure_scene <= 7 else "")
                rejected.append({"source_url": url,
                                 "reason": "reviewer rejected: " + location + ", ".join(failed_gates)})
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
        state.update(status="failed", error=_safe_error(exc), generation_calls=budget[0])
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
