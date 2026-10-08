"""Behavioral checks for the cloud research gate; no live API calls."""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from scam_autopsy import research


URL = "https://consumer.ftc.gov/consumer-alerts/2026/09/fake-tractor-listing"
OTHER_URL = "https://consumer.ftc.gov/consumer-alerts/2026/09/fake-equipment-payment"
QUOTE = "Scammers send an invoice and ask for a wire transfer before the equipment arrives."
SOURCE = ("The FTC describes fake farm equipment listings. " + QUOTE + " "
          "Search for the business independently and call a number you find yourself. " * 5)
NARRATION = "A fake equipment listing can look convincing, so check the seller independently before sending money by wire."
FULL_MODELS = ("gemini-3.7-flash", "gemini-3.6-flash", "gemini-3.5-flash",
               "gemini-3.8-flash")


def script(url: str = URL) -> dict:
    headings = ("THE LISTING", "THE SELLER", "THE INVOICE", "THE WIRE",
                "THE PAUSE", "THE CHECK", "THE SOURCE")
    adjectives = ("fake", "phony", "bogus", "deceptive", "fraudulent", "invented", "false")
    visuals = ("phone", "profile", "payment", "flow", "warning", "receipt", "cards")
    return {
        "id": "draft", "title": "The fake equipment listing", "hook": NARRATION,
        "source_urls": [url], "description": f"FTC-documented pattern. Source: {url}",
        "format": "short", "scenes": [
            {"narration": NARRATION.replace("fake", adjective), "heading": heading, "visual": visual,
             "items": ["Find the real number", "Call before paying"], "label": "Illustration",
             "evidence_quote": QUOTE} for heading, adjective, visual in zip(headings, adjectives, visuals)
        ],
    }


def review() -> dict:
    return {"factual_fidelity": True, "practical_advice": True, "quality": True,
            "diversity": True, "no_invented_incidents": True, "scene_evidence_complete": True,
            "conditions_preserved": True, "visual_disclosure": True,
            "notes": "The mechanism and action match the source."}


class SourceValidationTests(unittest.TestCase):
    def test_rejects_ssrf_credentials_ip_and_lookalike_hosts(self):
        bad = [
            "http://consumer.ftc.gov/consumer-alerts/2026/09/fake-tractor-listing",
            "https://user:secret@consumer.ftc.gov/consumer-alerts/2026/09/fake-tractor-listing",
            "https://127.0.0.1/consumer-alerts/2026/09/fake-tractor-listing",
            "https://consumer.ftc.gov.evil.test/consumer-alerts/2026/09/fake-tractor-listing",
            "https://consumer.ftc.gov:444/consumer-alerts/2026/09/fake-tractor-listing",
            "https://consumer.ftc.gov/%2e%2e/secret",
            "https://consumer.ftc.gov//internal",
            "https://consumer.ftc.gov/consumer-alerts/2026/09/a?url=https://127.0.0.1",
            "https://consumer.ftc.gov/consumer-alerts/2026/09/a#fragment",
            "https://consumer.ftc.gov:invalid/consumer-alerts/2026/09/a",
        ]
        for url in bad:
            with self.subTest(url=url), self.assertRaises(research.ResearchError):
                research.canonical_source_url(url)
        self.assertEqual(research.canonical_source_url(URL), URL)

    def test_extracts_only_visible_article_body(self):
        html = ("<html><script>UNTRUSTED SCRIPT</script><main><article class='node--view-mode-cfg-default'>"
                "<div class='field--name-body'><p>" + (QUOTE + " ") * 5 +
                "</p><script>HIDDEN CLAIM</script></div></article></main></html>").encode()
        text = research.extract_article(html)
        self.assertIn(QUOTE, text)
        self.assertNotIn("UNTRUSTED SCRIPT", text)
        self.assertNotIn("HIDDEN CLAIM", text)

    def test_quote_and_numeric_claim_must_match_source(self):
        valid = script()
        self.assertEqual(research.validate_script(valid, URL, SOURCE), valid)
        absent = copy.deepcopy(valid)
        absent["scenes"][0]["evidence_quote"] = "Scammers always steal your tractor."
        with self.assertRaisesRegex(research.ResearchError, "evidence quote absent"):
            research.validate_script(absent, URL, SOURCE)
        number = copy.deepcopy(valid)
        number["scenes"][0]["narration"] += " They demand $8,000."
        with self.assertRaisesRegex(research.ResearchError, "numeric claim"):
            research.validate_script(number, URL, SOURCE)
        metadata = copy.deepcopy(valid)
        metadata["title"] += " $8,000"
        with self.assertRaisesRegex(research.ResearchError, "metadata numeric"):
            research.validate_script(metadata, URL, SOURCE)

    def test_fictional_ui_requires_illustration_and_hook_must_be_spoken(self):
        for visual in ("phone", "profile", "payment", "invoice screen"):
            with self.subTest(visual=visual):
                candidate = script()
                candidate["scenes"][0]["visual"] = visual
                candidate["scenes"][0]["label"] = "SOURCE: FTC"
                with self.assertRaisesRegex(research.ResearchError, "fictional UI"):
                    research.validate_script(candidate, URL, SOURCE)
        candidate = script()
        candidate["hook"] = "This generic warning never appears in the narration."
        with self.assertRaisesRegex(research.ResearchError, "speak hook verbatim"):
            research.validate_script(candidate, URL, SOURCE)

    def test_numeric_support_preserves_currency_scale_and_percent(self):
        self.assertNotEqual(research._numbers("$1 million"), research._numbers("$1"))
        self.assertNotEqual(research._numbers("$1"), research._numbers("£1"))
        self.assertNotEqual(research._numbers("1%"), research._numbers("1"))
        self.assertEqual(research._numbers("$1,000"), research._numbers("$1000"))
        case = script()
        case["scenes"][0]["evidence_quote"] = "The loss was $1 million."
        case["scenes"][0]["narration"] += " It cost $1."
        with self.assertRaisesRegex(research.ResearchError, "numeric claim"):
            research.validate_script(case, URL, SOURCE + " The loss was $1 million.")

    def test_source_backed_95_word_short_passes_but_outside_90_to_125_fails(self):
        narrations = [
            "A fake tractor listing appears and invites a buyer to contact the seller.",
            "The seller sends an invoice and asks for a wire before the tractor arrives.",
            "That transfer request is the dangerous turn because a convincing listing is not proof.",
            "The FTC says scammers use equipment listings and request payment before any delivery.",
            "Before paying, find the business yourself and call a number from an independent source.",
            "Do not trust the invoice when the listing is the thing you are checking.",
            "Pause the payment until you have verified the seller through your own search.",
        ]
        advice_quote = "Search for the business independently and call a number you find yourself."
        candidate = script()
        for index, scene in enumerate(candidate["scenes"]):
            scene["narration"] = narrations[index]
            scene["evidence_quote"] = advice_quote if index in (4, 6) else QUOTE
        candidate["hook"] = narrations[0]
        self.assertEqual(len(research.WORD.findall(" ".join(narrations))), 95)
        self.assertEqual(research.validate_script(candidate, URL, SOURCE), candidate)

        too_short = copy.deepcopy(candidate)
        too_short["scenes"][0]["narration"] = "A fake listing appears."
        too_short["hook"] = too_short["scenes"][0]["narration"]
        with self.assertRaisesRegex(research.ResearchError, "outside 90-125"):
            research.validate_script(too_short, URL, SOURCE)

        too_long = copy.deepcopy(candidate)
        too_long["scenes"][0]["narration"] += " Check the real seller before paying. " * 6
        with self.assertRaisesRegex(research.ResearchError, "outside 90-125"):
            research.validate_script(too_long, URL, SOURCE)

    def test_escaped_rss_link_is_ignored_in_favor_of_title_anchor(self):
        feed = (f"<rss><channel><item><title><a href='{URL}'>Fake tractor scam</a></title>"
                "<link>https://consumer.ftc.gov/%3Cbad-link%3E</link>"
                "<description>Scammers impersonate sellers.</description></item></channel></rss>").encode()
        with patch.object(research, "_get_public", return_value=feed):
            self.assertEqual(research.discover_articles(Mock()), [(URL, "Fake tractor scam")])

    def test_redirect_to_private_address_is_rejected_before_connecting(self):
        response = Mock(status_code=302, headers={"Location": "https://127.0.0.1/internal"})
        session = Mock()
        session.get.return_value = response
        with self.assertRaisesRegex(research.ResearchError, "unsafe source URL"):
            research._get_public(session, URL)
        self.assertEqual(session.get.call_count, 1)
        response.close.assert_called_once()


class ResearchRunTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.content = self.root / "content"
        self.content.mkdir()
        self.state = self.root / "state" / "research.json"

    def run_it(self, limit=2):
        return research.run_research(limit, content_dir=self.content, state_file=self.state,
                                     session=Mock(), key="test-key")

    def test_dedupes_existing_content_source_before_generation(self):
        (self.content / "existing.json").write_text(json.dumps({"id": "seed", "source_urls": [URL]}))
        with patch.object(research, "_model_names", return_value=research.FREE_TIER_CANDIDATES), \
             patch.object(research, "discover_articles", return_value=[(URL, "Old scam"), (OTHER_URL, "New scam")]), \
             patch.object(research, "_get_public", return_value=b"html"), \
             patch.object(research, "extract_article", return_value=SOURCE), \
             patch.object(research, "_gemini_json", side_effect=[script(OTHER_URL), review()]) as generate:
            paths = self.run_it()
        self.assertEqual(len(paths), 1)
        self.assertEqual(generate.call_count, 2)
        self.assertEqual(json.loads(paths[0].read_text())["source_urls"], [OTHER_URL])
        self.assertTrue(json.loads(paths[0].read_text())["approved"])
        self.assertEqual(json.loads(self.state.read_text())["status"], "passed")

    def test_dedupes_source_hash_from_prior_state(self):
        import hashlib
        digest = hashlib.sha256(SOURCE.encode()).hexdigest()
        self.state.parent.mkdir()
        self.state.write_text(json.dumps({"used_source_hashes": [digest]}))
        with patch.object(research, "_model_names", return_value=research.FREE_TIER_CANDIDATES), \
             patch.object(research, "discover_articles", return_value=[(URL, "New scam")]), \
             patch.object(research, "_get_public", return_value=b"html"), \
             patch.object(research, "extract_article", return_value=SOURCE), \
             patch.object(research, "_gemini_json") as generate:
            self.assertEqual(self.run_it(), [])
        generate.assert_not_called()
        self.assertEqual(json.loads(self.state.read_text())["status"], "no_new_script")

    def test_missing_distinct_supported_models_fails_closed(self):
        with patch.object(research, "_model_names", side_effect=research.ResearchError("two configured free-tier Gemini models are unavailable")), \
             patch.object(research, "discover_articles") as discover:
            self.assertEqual(self.run_it(), [])
        discover.assert_not_called()
        self.assertEqual(list(self.content.glob("*.json")), [])
        self.assertEqual(json.loads(self.state.read_text())["status"], "failed")

    def test_missing_key_fails_before_any_network_call(self):
        session = Mock()
        self.assertEqual(research.run_research(content_dir=self.content, state_file=self.state,
                                               session=session, key=""), [])
        session.get.assert_not_called()
        session.post.assert_not_called()
        self.assertEqual(json.loads(self.state.read_text())["status"], "failed")

    def test_corrupt_state_fails_closed_before_generation(self):
        self.state.parent.mkdir()
        self.state.write_text("{broken")
        with patch.object(research, "_model_names") as models:
            self.assertEqual(self.run_it(), [])
        models.assert_not_called()
        self.assertEqual(json.loads(self.state.read_text())["status"], "failed")

    def test_failed_second_review_preserves_first_approved_script(self):
        with patch.object(research, "_model_names", return_value=research.FREE_TIER_CANDIDATES), \
             patch.object(research, "discover_articles", return_value=[(URL, "Scam one"), (OTHER_URL, "Scam two")]), \
             patch.object(research, "_get_public", return_value=b"html"), \
             patch.object(research, "extract_article", side_effect=[SOURCE + " " + URL,
                                                                  SOURCE + " " + OTHER_URL]), \
             patch.object(research, "_gemini_json", side_effect=[script(URL), review(), script(OTHER_URL),
                                                                   {**review(), "factual_fidelity": False}]):
            paths = self.run_it()
        self.assertEqual(len(paths), 1)
        self.assertEqual(json.loads(paths[0].read_text())["source_urls"], [URL])
        result = json.loads(self.state.read_text())
        self.assertEqual(result["status"], "partial_success")
        self.assertEqual(result["rejected"][0]["source_url"], OTHER_URL)
        self.assertIn("factual_fidelity", result["rejected"][0]["reason"])

    def test_writer_rejection_tries_next_source_with_remaining_budget(self):
        first = script(URL)
        first["scenes"][0]["evidence_quote"] = "invented quote"
        generated = iter((first, script(OTHER_URL), review()))

        def generate(*args):
            budget = args[4]
            budget[0] += 1
            return next(generated)

        with patch.object(research, "_model_names", return_value=research.FREE_TIER_CANDIDATES), \
             patch.object(research, "discover_articles", return_value=[(URL, "Scam one"), (OTHER_URL, "Scam two")]), \
             patch.object(research, "_get_public", return_value=b"html"), \
             patch.object(research, "extract_article", side_effect=[SOURCE + URL, SOURCE + OTHER_URL]), \
             patch.object(research, "_gemini_json", side_effect=generate) as model:
            paths = self.run_it()
        self.assertEqual(len(paths), 1)
        self.assertEqual(model.call_count, 3)
        state = json.loads(self.state.read_text())
        self.assertEqual(state["generation_calls"], 3)
        self.assertEqual(state["status"], "partial_success")
        self.assertIn("writer validation", state["rejected"][0]["reason"])

    def test_listed_404_model_falls_back_and_is_excluded_for_seven_days(self):
        used_models = []

        def generate(_session, _key, model, prompt, budget, _temperature):
            budget[0] += 1
            used_models.append(model)
            if model == "gemini-3.7-flash":
                raise research.ModelUnavailable("listed but generation unavailable")
            return script(URL) if "Create ONE" in prompt else review()

        with patch.object(research, "_model_names", return_value=FULL_MODELS), \
             patch.object(research, "discover_articles", return_value=[(URL, "Scam one")]), \
             patch.object(research, "_get_public", return_value=b"html"), \
             patch.object(research, "extract_article", return_value=SOURCE), \
             patch.object(research, "_gemini_json", side_effect=generate):
            paths = self.run_it(limit=1)
        self.assertEqual(len(paths), 1)
        self.assertEqual(used_models, ["gemini-3.7-flash", "gemini-3.6-flash", "gemini-3.5-flash"])
        created = json.loads(paths[0].read_text())
        self.assertEqual(created["review"]["writer"], "gemini-3.6-flash")
        self.assertEqual(created["review"]["reviewer"], "gemini-3.5-flash")
        state = json.loads(self.state.read_text())
        self.assertEqual(state["generation_calls"], 3)
        from datetime import datetime, timezone
        until = datetime.fromisoformat(state["model_unavailable_until"]["gemini-3.7-flash"])
        self.assertGreater(until, datetime.now(timezone.utc))

    def test_persisted_404_exclusion_skips_bad_model_next_run(self):
        from datetime import datetime, timedelta, timezone
        self.state.parent.mkdir()
        self.state.write_text(json.dumps({"model_unavailable_until": {
            "gemini-3.7-flash": (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()}}))
        models = []

        def generate(_session, _key, model, prompt, budget, _temperature):
            budget[0] += 1
            models.append(model)
            return script(URL) if "Create ONE" in prompt else review()

        with patch.object(research, "_model_names", return_value=FULL_MODELS), \
             patch.object(research, "discover_articles", return_value=[(URL, "Scam one")]), \
             patch.object(research, "_get_public", return_value=b"html"), \
             patch.object(research, "extract_article", return_value=SOURCE), \
             patch.object(research, "_gemini_json", side_effect=generate):
            self.assertEqual(len(self.run_it(limit=1)), 1)
        self.assertEqual(models, ["gemini-3.6-flash", "gemini-3.5-flash"])

    def test_404_response_is_not_retried_on_same_model(self):
        session = Mock()
        session.post.return_value = Mock(status_code=404)
        budget = [0]
        with self.assertRaises(research.ModelUnavailable):
            research._gemini_json(session, "test-key", "gemini-3.8-flash", "test", budget, 0.6)
        self.assertEqual(session.post.call_count, 1)
        self.assertEqual(budget[0], 1)

    def test_no_distinct_working_models_fails_closed_without_output(self):
        def unavailable(_session, _key, _model, _prompt, budget, _temperature):
            budget[0] += 1
            raise research.ModelUnavailable("listed but unavailable")

        with patch.object(research, "_model_names", return_value=research.FREE_TIER_CANDIDATES), \
             patch.object(research, "discover_articles", return_value=[(URL, "Scam one")]), \
             patch.object(research, "_get_public", return_value=b"html"), \
             patch.object(research, "extract_article", return_value=SOURCE), \
             patch.object(research, "_gemini_json", side_effect=unavailable) as model:
            self.assertEqual(self.run_it(limit=1), [])
        self.assertEqual(model.call_count, 3)
        self.assertEqual(list(self.content.glob("*.json")), [])
        state = json.loads(self.state.read_text())
        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["generation_calls"], 3)

    def test_503_writer_falls_back_once_without_persistent_exclusion(self):
        used_models = []

        def generate(_session, _key, model, prompt, budget, _temperature):
            budget[0] += 1
            used_models.append(model)
            if model == "gemini-3.7-flash":
                raise research.ModelTransient(503)
            return script(URL) if "Create ONE" in prompt else review()

        with patch.object(research, "_model_names", return_value=FULL_MODELS), \
             patch.object(research, "discover_articles", return_value=[(URL, "Scam one")]), \
             patch.object(research, "_get_public", return_value=b"html"), \
             patch.object(research, "extract_article", return_value=SOURCE), \
             patch.object(research, "_gemini_json", side_effect=generate), \
             patch.object(research.time, "sleep") as sleep:
            paths = self.run_it(limit=1)
        self.assertEqual(len(paths), 1)
        self.assertEqual(used_models, ["gemini-3.7-flash", "gemini-3.6-flash", "gemini-3.5-flash"])
        sleep.assert_called_once_with(2)
        state = json.loads(self.state.read_text())
        self.assertEqual(state["generation_calls"], 3)
        self.assertNotIn("gemini-3.7-flash", state["model_unavailable_until"])
        self.assertEqual([(a["model"], a["status"], a["detail"]) for a in state["generation_attempts"]],
                         [("gemini-3.7-flash", "transient", "writer: HTTP 503"),
                          ("gemini-3.6-flash", "success", "writer"),
                          ("gemini-3.5-flash", "success", "reviewer")])

    def test_total_transient_outage_records_sanitized_attempts_and_last_cause(self):
        statuses = iter((429, 503, 502))

        def unavailable(_session, _key, _model, _prompt, budget, _temperature):
            budget[0] += 1
            raise research.ModelTransient(next(statuses))

        with patch.object(research, "_model_names", return_value=FULL_MODELS), \
             patch.object(research, "discover_articles", return_value=[(URL, "Scam one")]), \
             patch.object(research, "_get_public", return_value=b"html"), \
             patch.object(research, "extract_article", return_value=SOURCE), \
             patch.object(research, "_gemini_json", side_effect=unavailable), \
             patch.object(research.time, "sleep") as sleep:
            self.assertEqual(self.run_it(limit=1), [])
        state = json.loads(self.state.read_text())
        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["generation_calls"], 3)
        self.assertEqual(state["generation_attempts"], [
            {"model": "gemini-3.7-flash", "status": "transient", "detail": "writer: HTTP 429"},
            {"model": "gemini-3.6-flash", "status": "transient", "detail": "writer: HTTP 503"},
            {"model": "gemini-3.5-flash", "status": "transient", "detail": "writer: HTTP 502"}])
        self.assertIn("gemini-3.5-flash writer: HTTP 502", state["error"])
        sleep.assert_called_once_with(2)

    def test_request_exception_text_is_not_persisted(self):
        with patch.object(research, "_model_names", side_effect=research.requests.HTTPError(
                "Authorization API_KEY_UNSAFE in request URL")):
            self.assertEqual(self.run_it(limit=1), [])
        state = json.loads(self.state.read_text())
        self.assertEqual(state["error"], "HTTPError")
        self.assertNotIn("API_KEY_UNSAFE", json.dumps(state))

    def test_recent_transient_models_move_behind_unfailed_free_models(self):
        from datetime import datetime, timezone
        self.state.parent.mkdir()
        self.state.write_text(json.dumps({
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "generation_attempts": [
                {"model": "gemini-3.7-flash", "status": "transient", "detail": "writer: HTTP 429"},
                {"model": "gemini-3.6-flash", "status": "transient", "detail": "writer: HTTP 429"},
                {"model": "gemini-3.8-flash", "status": "transient", "detail": "writer: HTTP 503"}],
        }))
        chosen = []

        def generate(_session, _key, model, prompt, budget, _temperature):
            budget[0] += 1
            chosen.append(model)
            return script(URL) if "Create ONE" in prompt else review()

        with patch.object(research, "_model_names", return_value=research.FREE_TIER_CANDIDATES), \
             patch.object(research, "discover_articles", return_value=[(URL, "Scam one")]), \
             patch.object(research, "_get_public", return_value=b"html"), \
             patch.object(research, "extract_article", return_value=SOURCE), \
             patch.object(research, "_gemini_json", side_effect=generate):
            paths = self.run_it(limit=1)
        self.assertEqual(len(paths), 1)
        self.assertEqual(chosen, ["gemini-3.5-flash-lite", "gemini-3.5-flash"])
        state = json.loads(self.state.read_text())
        self.assertEqual(state["model_order"][:3],
                         ["gemini-3.5-flash", "gemini-3.5-flash-lite", "gemini-3.1-flash-lite"])
        self.assertEqual(state["generation_calls"], 2)
        created = json.loads(paths[0].read_text())
        self.assertEqual(created["review"]["writer"], "gemini-3.5-flash-lite")
        self.assertEqual(created["review"]["reviewer"], "gemini-3.5-flash")

    def test_recent_lite_503_uses_other_lite_writer_and_full_reviewer(self):
        from datetime import datetime, timezone
        self.state.parent.mkdir()
        self.state.write_text(json.dumps({
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "generation_attempts": [
                {"model": "gemini-3.5-flash", "status": "success", "detail": "writer"},
                {"model": "gemini-3.5-flash-lite", "status": "transient",
                 "detail": "reviewer: HTTP 503"},
                {"model": "gemini-3.1-flash-lite", "status": "success", "detail": "reviewer"},
            ],
        }))
        chosen = []

        def generate(_session, _key, model, prompt, budget, _temperature):
            budget[0] += 1
            chosen.append(model)
            return script(URL) if "Create ONE" in prompt else review()

        with patch.object(research, "_model_names", return_value=research.FREE_TIER_CANDIDATES), \
             patch.object(research, "discover_articles", return_value=[(URL, "Scam one")]), \
             patch.object(research, "_get_public", return_value=b"html"), \
             patch.object(research, "extract_article", return_value=SOURCE), \
             patch.object(research, "_gemini_json", side_effect=generate):
            paths = self.run_it(limit=1)
        self.assertEqual(len(paths), 1)
        self.assertEqual(chosen, ["gemini-3.1-flash-lite", "gemini-3.5-flash"])
        state = json.loads(self.state.read_text())
        self.assertEqual(state["writer_order"][:2],
                         ["gemini-3.1-flash-lite", "gemini-3.5-flash-lite"])
        self.assertEqual(state["reviewer_order"][0], "gemini-3.5-flash")
        self.assertEqual(state["generation_calls"], 2)

    def test_lite_only_models_cannot_approve_without_full_flash_reviewer(self):
        models = ("gemini-3.5-flash-lite", "gemini-3.1-flash-lite")
        with patch.object(research, "_model_names", return_value=models), \
             patch.object(research, "_gemini_json") as generate:
            self.assertEqual(self.run_it(limit=1), [])
        generate.assert_not_called()
        self.assertEqual(json.loads(self.state.read_text())["status"], "failed")

    def test_lite_writer_cannot_use_another_lite_after_full_reviewer_outage(self):
        models = ("gemini-3.5-flash-lite", "gemini-3.5-flash", "gemini-3.1-flash-lite")
        called = []

        def generate(_session, _key, model, prompt, budget, _temperature):
            budget[0] += 1
            called.append(model)
            if "Create ONE" in prompt:
                return script(URL)
            raise research.ModelTransient(503)

        with patch.object(research, "_model_names", return_value=models), \
             patch.object(research, "discover_articles", return_value=[(URL, "Scam one")]), \
             patch.object(research, "_get_public", return_value=b"html"), \
             patch.object(research, "extract_article", return_value=SOURCE), \
             patch.object(research, "_gemini_json", side_effect=generate), \
             patch.object(research.time, "sleep"):
            self.assertEqual(self.run_it(limit=1), [])
        self.assertEqual(called, ["gemini-3.5-flash-lite", "gemini-3.5-flash"])
        self.assertEqual(json.loads(self.state.read_text())["generation_calls"], 2)

    def test_failed_conditions_preserved_review_rejects_script(self):
        weak_review = {**review(), "conditions_preserved": False,
                       "notes": "The source says stops answering or gives an excuse, not always stops."}
        with patch.object(research, "_model_names", return_value=research.FREE_TIER_CANDIDATES), \
             patch.object(research, "discover_articles", return_value=[(URL, "Scam one")]), \
             patch.object(research, "_get_public", return_value=b"html"), \
             patch.object(research, "extract_article", return_value=SOURCE), \
             patch.object(research, "_gemini_json", side_effect=[script(URL), weak_review]):
            self.assertEqual(self.run_it(limit=1), [])
        state = json.loads(self.state.read_text())
        self.assertEqual(state["status"], "failed")
        self.assertIn("conditions_preserved", state["rejected"][0]["reason"])
        self.assertEqual(list(self.content.glob("*.json")), [])

    def test_old_transient_models_return_to_normal_priority(self):
        from datetime import datetime, timedelta, timezone
        old = {"checked_at": (datetime.now(timezone.utc) - timedelta(hours=7)).isoformat(),
               "generation_attempts": [{"model": "gemini-3.7-flash", "status": "transient",
                                        "detail": "writer: HTTP 429"}]}
        self.assertEqual(research._deprioritize_recent_transients(research.FREE_TIER_CANDIDATES,
                                                                  old, datetime.now(timezone.utc)),
                         research.FREE_TIER_CANDIDATES)

    def test_read_timeout_falls_back_within_global_call_budget(self):
        used_models = []

        def generate(_session, _key, model, prompt, budget, _temperature):
            budget[0] += 1
            used_models.append(model)
            if model == "gemini-3.7-flash":
                raise research.requests.exceptions.ReadTimeout("generation read timed out")
            return script(URL) if "Create ONE" in prompt else review()

        with patch.object(research, "_model_names", return_value=FULL_MODELS), \
             patch.object(research, "discover_articles", return_value=[(URL, "Scam one")]), \
             patch.object(research, "_get_public", return_value=b"html"), \
             patch.object(research, "extract_article", return_value=SOURCE), \
             patch.object(research, "_gemini_json", side_effect=generate), \
             patch.object(research.time, "sleep") as sleep:
            paths = self.run_it(limit=1)
        self.assertEqual(len(paths), 1)
        self.assertEqual(used_models, ["gemini-3.7-flash", "gemini-3.6-flash", "gemini-3.5-flash"])
        self.assertEqual(json.loads(self.state.read_text())["generation_calls"], 3)
        sleep.assert_called_once_with(2)

    def test_approved_first_script_survives_later_503_and_budget_limit(self):
        calls = []

        def generate(_session, _key, model, prompt, budget, _temperature):
            budget[0] += 1
            calls.append(model)
            if len(calls) == 3:
                raise research.ModelTransient(503)
            return script(URL) if "Create ONE" in prompt else review()

        with patch.object(research, "_model_names", return_value=FULL_MODELS), \
             patch.object(research, "discover_articles", return_value=[(URL, "Scam one"), (OTHER_URL, "Scam two")]), \
             patch.object(research, "_get_public", return_value=b"html"), \
             patch.object(research, "extract_article", side_effect=[SOURCE + URL, SOURCE + OTHER_URL]), \
             patch.object(research, "_gemini_json", side_effect=generate), \
             patch.object(research.time, "sleep") as sleep:
            paths = self.run_it()
        self.assertEqual(len(paths), 1)
        self.assertEqual(json.loads(paths[0].read_text())["source_urls"], [URL])
        self.assertEqual(calls, ["gemini-3.7-flash", "gemini-3.5-flash", "gemini-3.7-flash"])
        sleep.assert_called_once_with(2)
        state = json.loads(self.state.read_text())
        self.assertEqual(state["status"], "partial_success")
        self.assertEqual(state["generation_calls"], 3)

    def test_retryable_status_codes_are_bounded_transients(self):
        for status in (429, 502, 503, 504):
            with self.subTest(status=status):
                session = Mock()
                session.post.return_value = Mock(status_code=status)
                budget = [0]
                with self.assertRaises(research.ModelTransient):
                    research._gemini_json(session, "test-key", "gemini-3.7-flash", "test", budget, 0.6)
                self.assertEqual(session.post.call_count, 1)
                self.assertEqual(budget[0], 1)

    def test_429_uses_one_call_then_signals_model_fallback(self):
        response = Mock(status_code=429)
        session = Mock()
        session.post.return_value = response
        budget = [0]
        with self.assertRaises(research.ModelTransient):
            research._gemini_json(session, "test-key", "gemini-3.7-flash", "test", budget, 0.1)
        self.assertEqual(session.post.call_count, 1)
        self.assertEqual(budget[0], 1)

    def test_current_flash_uses_documented_low_thinking_level(self):
        response = Mock(status_code=200)
        response.json.return_value = {"candidates": [{"finishReason": "STOP",
                                                      "content": {"parts": [{"text": "{}"}]}}]}
        session = Mock()
        session.post.return_value = response
        research._gemini_json(session, "test-key", "gemini-3.8-flash", "test", [0], 0.6)
        self.assertEqual(session.post.call_args.kwargs["json"]["generationConfig"]["thinkingConfig"],
                         {"thinkingLevel": "low"})
        self.assertEqual(session.post.call_args.kwargs["timeout"], (10, 90))

    def test_flash_lite_uses_json_without_unverified_thinking_config(self):
        response = Mock(status_code=200)
        response.json.return_value = {"candidates": [{"finishReason": "STOP",
                                                      "content": {"parts": [{"text": "{}"}]}}]}
        session = Mock()
        session.post.return_value = response
        for model in ("gemini-3.5-flash-lite", "gemini-3.1-flash-lite"):
            research._gemini_json(session, "test-key", model, "test", [0], 0.1)
            config = session.post.call_args.kwargs["json"]["generationConfig"]
            self.assertEqual(config["responseMimeType"], "application/json")
            self.assertNotIn("thinkingConfig", config)

    def test_only_configured_supported_models_can_be_selected(self):
        session = Mock()
        session.get.return_value.json.return_value = {"models": [
            {"name": "models/gemini-3.8-flash", "supportedGenerationMethods": ["generateContent"]},
            {"name": "models/gemini-paid-only", "supportedGenerationMethods": ["generateContent"]},
        ]}
        with self.assertRaisesRegex(research.ResearchError, "unavailable"):
            research._model_names(session, "test-key")


if __name__ == "__main__":
    unittest.main()
