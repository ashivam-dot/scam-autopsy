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
            "conditions_preserved": True, "visual_disclosure": True, "original_narration": True,
            "notes": "The mechanism and action match the source.", "first_failure_scene": None}


class SourceValidationTests(unittest.TestCase):
    def test_rejects_source_reading_but_accepts_original_paraphrase(self):
        copied = script()
        copied_lines = [
            "Imagine showing up at the dealership and finding out the dealer has no record of your payment.",
            "Scammers set up bogus car dealership websites to trick you into paying up front for a car.",
            "They clone real dealer websites by copying brand logos, vehicle listings, and even photos.",
            "These sites might advertise rare muscle cars or hard-to-find classics to lure you in.",
            "To put you at ease, they describe the buying process and offer flexible return policies.",
            "Protect yourself by asking to see the car and the dealership in person.",
            "Walk away if the dealer insists on an upfront payment by wire transfer only.",
        ]
        article = (SOURCE + " But imagine showing up at the dealership and finding out the dealer "
                   "has no record of your order or your payment. Scammers set up bogus car "
                   "dealership websites to trick you into paying up front for a car you’ll never "
                   "lay hands on. They clone a real auto dealer’s website, copying the brand logos, "
                   "vehicle listings, and photos down to the last detail. They might advertise rare "
                   "muscle cars or hard-to-find classics to lure you in. They describe the buying "
                   "process in detail and offer flexible return policies to put you at ease. So go "
                   "the extra mile — ask to see the car, and the dealership, in person. Walk away if "
                   "the dealer insists on an upfront payment by wire transfer only.")
        for scene, line in zip(copied["scenes"], copied_lines):
            scene["narration"] = line
        copied["hook"] = copied_lines[0]
        covered, total = research._source_reading_counts(copied_lines, article)
        self.assertGreater(covered * 100, total * research.MAX_SOURCE_READING_PERCENT)
        with self.assertRaisesRegex(research.ResearchError, "narration copies source prose"):
            research.validate_script(copied, URL, article)

        original = script()
        self.assertEqual(research._source_reading_counts(
            [scene["narration"] for scene in original["scenes"]], SOURCE)[0], 0)
        self.assertEqual(research.validate_script(original, URL, SOURCE), original)

    def test_short_source_phrase_and_exact_thirty_percent_are_allowed(self):
        source_words = [f"sourceword{i}" for i in range(40)]
        article = SOURCE + " " + " ".join(source_words)
        scene_sizes = (10, 10, 10, 17, 17, 18, 18)
        for borrowed in (8, 30):
            with self.subTest(borrowed=borrowed):
                words = source_words[:borrowed] + [f"originalword{i}" for i in range(100 - borrowed)]
                candidate = script()
                offset = 0
                for scene, size in zip(candidate["scenes"], scene_sizes):
                    scene["narration"] = " ".join(words[offset:offset + size])
                    offset += size
                candidate["hook"] = candidate["scenes"][0]["narration"]
                self.assertEqual(research._source_reading_counts(
                    [scene["narration"] for scene in candidate["scenes"]], article), (borrowed, 100))
                self.assertEqual(research.validate_script(candidate, URL, article), candidate)

    def test_prompts_explain_originality_and_renderer_visual_contract(self):
        writer = research._writer_prompt(URL, "Fake listing", research._source_passages(SOURCE))
        reviewer = research._reviewer_prompt(script(), URL, SOURCE)
        for prompt in (writer, reviewer):
            self.assertIn("UNKNOWN CONTACT", prompt)
            self.assertIn("LOOKS FAMILIAR?", prompt)
            self.assertIn("ILLUSTRATED DOCUMENT", prompt)
        self.assertIn("empty dealership lot", writer)
        self.assertIn("Empty lot", reviewer)
        self.assertIn("original spoken story", writer)
        self.assertIn("original_narration", reviewer)

    def test_citation_passages_are_bounded_and_literal_source_substrings(self):
        source = (SOURCE + " ") * 40
        passages = research._source_passages(source)
        self.assertTrue(passages)
        self.assertLessEqual(len(passages), research.MAX_CITATION_PASSAGES)
        self.assertTrue(all(0 < len(passage) <= 500 and passage in source[:14000]
                            for passage in passages))
        self.assertIn(source[:100], passages[0])
        self.assertIn(source[:14000][-100:], passages[-1])

    def test_citation_ids_materialize_owned_quotes_and_reject_invalid_ids(self):
        passages = research._source_passages(SOURCE)
        candidate = script()
        for scene in candidate["scenes"]:
            scene["evidence_quote_id"] = 1
            scene["evidence_quote"] = "fabricated quote that must be discarded"
        research._materialize_scene_quotes(candidate, passages)
        self.assertTrue(all(scene["evidence_quote"] == passages[0] and
                            "evidence_quote_id" not in scene for scene in candidate["scenes"]))
        self.assertEqual(research.validate_script(candidate, URL, SOURCE), candidate)

        for bad_id in (0, len(passages) + 1, True, "1", 1.0):
            with self.subTest(bad_id=bad_id):
                invalid = script()
                invalid["scenes"][0]["evidence_quote_id"] = bad_id
                with self.assertRaisesRegex(research.ResearchError,
                                            "scene 1 invalid evidence quote ID"):
                    research._materialize_scene_quotes(invalid, passages)

        absent = script()
        del absent["scenes"][0]["evidence_quote"]
        research._materialize_scene_quotes(absent, passages)
        with self.assertRaisesRegex(research.ResearchError, "scene 1 evidence_quote"):
            research.validate_script(absent, URL, SOURCE)

    def test_materialized_quote_does_not_permit_changed_numbers(self):
        candidate = script()
        candidate["scenes"][0]["evidence_quote_id"] = 1
        candidate["scenes"][0]["narration"] += " They demand $8,000."
        research._materialize_scene_quotes(candidate, research._source_passages(SOURCE))
        with self.assertRaisesRegex(research.ResearchError, "scene 1 numeric claim"):
            research.validate_script(candidate, URL, SOURCE)

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

    def test_repeated_failure_keeps_first_failure_time_and_success_clears_it(self):
        def run_failed():
            research.run_research(content_dir=self.content, state_file=self.state, session=Mock(), key="")
            return json.loads(self.state.read_text())

        first = run_failed()
        self.assertEqual(first["failing_since"], first["checked_at"])
        self.assertEqual(run_failed()["failing_since"], first["checked_at"])

        self.state.write_text(json.dumps({"status": "failed", "checked_at": "2026-10-08T19:01:57+00:00"}))
        self.assertEqual(run_failed()["failing_since"], "2026-10-08T19:01:57+00:00")

        with patch.object(research, "_model_names", return_value=research.FREE_TIER_CANDIDATES), \
             patch.object(research, "discover_articles", return_value=[(URL, "New scam")]), \
             patch.object(research, "_get_public", return_value=b"html"), \
             patch.object(research, "extract_article", return_value=SOURCE), \
             patch.object(research, "_gemini_json", side_effect=[script(URL), review()]):
            self.assertEqual(len(self.run_it()), 1)
        self.assertNotIn("failing_since", json.loads(self.state.read_text()))

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

    def test_recent_reviewer_rejection_tries_untried_car_topic_before_qr(self):
        from datetime import datetime, timezone
        self.state.parent.mkdir()
        self.state.write_text(json.dumps({
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "rejected": [{"source_url": URL,
                          "reason": "reviewer rejected: scene 1: factual_fidelity"}],
        }))
        fetched = []

        def get_public(_session, url):
            fetched.append(url)
            return b"html"

        with patch.object(research, "_model_names", return_value=research.FREE_TIER_CANDIDATES), \
             patch.object(research, "discover_articles", return_value=[(URL, "QR scam"),
                                                                         (OTHER_URL, "Car scam")]), \
             patch.object(research, "_get_public", side_effect=get_public), \
             patch.object(research, "extract_article", return_value=SOURCE), \
             patch.object(research, "_gemini_json", side_effect=[script(OTHER_URL), review()]):
            paths = self.run_it(limit=1)
        self.assertEqual(fetched, [OTHER_URL])
        self.assertEqual(len(paths), 1)
        self.assertEqual(json.loads(paths[0].read_text())["source_urls"], [OTHER_URL])

    def test_source_rejection_backoff_expires_after_six_hours(self):
        from datetime import datetime, timedelta, timezone
        checked = datetime.now(timezone.utc)
        articles = [(URL, "QR scam"), (OTHER_URL, "Car scam")]
        prior = {"checked_at": (checked - timedelta(hours=7)).isoformat(),
                 "rejected": [{"source_url": URL, "reason": "writer validation: scene 1 invalid"}],
                 "source_rejection_until": {
                     URL: (checked - timedelta(minutes=1)).isoformat()}}
        self.assertEqual(research._deprioritize_recent_rejections(articles, prior, checked), articles)

    def test_service_failure_preserves_prior_source_rejection_for_next_run(self):
        from datetime import datetime, timezone
        self.state.parent.mkdir()
        self.state.write_text(json.dumps({
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "rejected": [{"source_url": URL,
                          "reason": "reviewer rejected: scene 1: factual_fidelity"}],
        }))

        def unavailable(_session, _key, _model, _prompt, budget, _temperature, _role):
            budget[0] += 1
            raise research.ModelTransient(503)

        models = ("gemini-3.5-flash-lite", "gemini-3.1-flash-lite", "gemini-3.7-flash",
                  "gemma-4-31b-it")
        with patch.object(research, "_model_names", return_value=models), \
             patch.object(research, "discover_articles", return_value=[(URL, "QR scam"),
                                                                         (OTHER_URL, "Car scam")]), \
             patch.object(research, "_get_public", return_value=b"html"), \
             patch.object(research, "extract_article", return_value=SOURCE), \
             patch.object(research, "_gemini_json", side_effect=unavailable), \
             patch.object(research.time, "sleep"):
            self.assertEqual(self.run_it(limit=1), [])
        failed_state = json.loads(self.state.read_text())
        self.assertEqual(failed_state["rejected"], [])
        self.assertIn(URL, failed_state["source_rejection_until"])

        fetched = []

        def get_public(_session, url):
            fetched.append(url)
            return b"html"

        with patch.object(research, "_model_names", return_value=("gemma-4-26b-a4b-it",
                                                                   "gemma-4-31b-it")), \
             patch.object(research, "discover_articles", return_value=[(URL, "QR scam"),
                                                                         (OTHER_URL, "Car scam")]), \
             patch.object(research, "_get_public", side_effect=get_public), \
             patch.object(research, "extract_article", return_value=SOURCE), \
             patch.object(research, "_gemini_json", side_effect=[script(OTHER_URL), review()]):
            paths = self.run_it(limit=1)
        self.assertEqual(fetched, [OTHER_URL])
        self.assertEqual(len(paths), 1)
        self.assertEqual(json.loads(paths[0].read_text())["source_urls"], [OTHER_URL])

    def test_approved_source_dedupes_even_when_recent_rejection_reorders_feed(self):
        from datetime import datetime, timezone
        (self.content / "approved.json").write_text(json.dumps({"id": "approved",
                                                                 "source_urls": [OTHER_URL]}))
        self.state.parent.mkdir()
        self.state.write_text(json.dumps({
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "rejected": [{"source_url": URL,
                          "reason": "writer validation: scene 1 evidence quote absent from article"}],
        }))
        with patch.object(research, "_model_names", return_value=research.FREE_TIER_CANDIDATES), \
             patch.object(research, "discover_articles", return_value=[(URL, "QR scam"),
                                                                         (OTHER_URL, "Car scam")]), \
             patch.object(research, "_get_public", return_value=b"html") as fetch, \
             patch.object(research, "extract_article", return_value=SOURCE), \
             patch.object(research, "_gemini_json", side_effect=[script(URL), review()]):
            paths = self.run_it(limit=1)
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(fetch.call_args.args[1], URL)
        self.assertEqual(len(paths), 1)
        self.assertEqual(json.loads(paths[0].read_text())["source_urls"], [URL])

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

        def generate(_session, _key, model, prompt, budget, _temperature, _role):
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

        def generate(_session, _key, model, prompt, budget, _temperature, _role):
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
        def unavailable(_session, _key, _model, _prompt, budget, _temperature, _role):
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

        def generate(_session, _key, model, prompt, budget, _temperature, _role):
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

        def unavailable(_session, _key, _model, _prompt, budget, _temperature, _role):
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

        def generate(_session, _key, model, prompt, budget, _temperature, _role):
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
        self.assertEqual(chosen, ["gemini-3.5-flash-lite", "gemma-4-31b-it"])
        state = json.loads(self.state.read_text())
        self.assertEqual(state["model_order"][:3],
                         ["gemini-3.5-flash", "gemini-3.5-flash-lite", "gemini-3.1-flash-lite"])
        self.assertEqual(state["generation_calls"], 2)
        created = json.loads(paths[0].read_text())
        self.assertEqual(created["review"]["writer"], "gemini-3.5-flash-lite")
        self.assertEqual(created["review"]["reviewer"], "gemma-4-31b-it")

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

        def generate(_session, _key, model, prompt, budget, _temperature, _role):
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
        self.assertEqual(chosen, ["gemini-3.1-flash-lite", "gemma-4-31b-it"])
        state = json.loads(self.state.read_text())
        self.assertEqual(state["writer_order"][:2],
                         ["gemini-3.1-flash-lite", "gemma-4-26b-a4b-it"])
        self.assertEqual(state["reviewer_order"][0], "gemma-4-31b-it")
        self.assertEqual(state["generation_calls"], 2)

    def test_fresh_lite_writes_first_but_cooling_survives_unrelated_failure(self):
        from datetime import datetime, timedelta, timezone
        models = research.FREE_TIER_CANDIDATES
        chosen = []
        current_url = [URL]

        def generate(_session, _key, model, _prompt, budget, _temperature, role):
            budget[0] += 1
            chosen.append((model, role))
            return script(current_url[0]) if role == "writer" else review()

        with patch.object(research, "_model_names", return_value=models), \
             patch.object(research, "discover_articles", return_value=[(URL, "Scam one")]), \
             patch.object(research, "_get_public", return_value=b"html"), \
             patch.object(research, "extract_article", return_value=SOURCE), \
             patch.object(research, "_gemini_json", side_effect=generate):
            self.assertEqual(len(self.run_it(limit=1)), 1)
        self.assertEqual(chosen[0], ("gemini-3.5-flash-lite", "writer"))
        fresh_state = json.loads(self.state.read_text())
        self.assertEqual(fresh_state["writer_order"][:3],
                         ["gemini-3.5-flash-lite", "gemini-3.1-flash-lite",
                          "gemma-4-26b-a4b-it"])

        # The next run has recent outages for both Lite models and 3.7 Flash.
        until = (datetime.now(timezone.utc) + timedelta(hours=5)).isoformat()
        fresh_state["model_transient_until"] = {model: until for model in
                                                 ("gemini-3.5-flash-lite",
                                                  "gemini-3.1-flash-lite", "gemini-3.7-flash")}
        self.state.write_text(json.dumps(fresh_state))
        with patch.object(research, "_model_names", side_effect=research.ResearchError(
                "catalog temporarily unavailable")):
            self.assertEqual(self.run_it(limit=1), [])
        failed_state = json.loads(self.state.read_text())
        self.assertEqual(set(failed_state["model_transient_until"]),
                         {"gemini-3.5-flash-lite", "gemini-3.1-flash-lite", "gemini-3.7-flash"})

        chosen.clear()
        current_url[0] = OTHER_URL
        with patch.object(research, "_model_names", return_value=models), \
             patch.object(research, "discover_articles", return_value=[(OTHER_URL, "Scam two")]), \
             patch.object(research, "_get_public", return_value=b"html"), \
             patch.object(research, "extract_article", return_value=SOURCE + OTHER_URL), \
             patch.object(research, "_gemini_json", side_effect=generate):
            self.assertEqual(len(self.run_it(limit=1)), 1)
        self.assertEqual(chosen[0], ("gemma-4-26b-a4b-it", "writer"))
        self.assertEqual(chosen[1], ("gemma-4-31b-it", "reviewer"))
        cooled_state = json.loads(self.state.read_text())
        self.assertEqual(cooled_state["writer_order"][0], "gemma-4-26b-a4b-it")
        self.assertGreater(cooled_state["writer_order"].index("gemini-3.5-flash-lite"),
                           cooled_state["writer_order"].index("gemma-4-31b-it"))

    def test_recent_reviewer_transient_moves_behind_healthy_reviewers(self):
        from datetime import datetime, timedelta, timezone
        self.state.parent.mkdir()
        self.state.write_text(json.dumps({"model_transient_until": {
            "gemma-4-31b-it": (datetime.now(timezone.utc) + timedelta(hours=5)).isoformat()}}))
        chosen = []

        def generate(_session, _key, model, _prompt, budget, _temperature, role):
            budget[0] += 1
            chosen.append((model, role))
            return script(URL) if role == "writer" else review()

        with patch.object(research, "_model_names", return_value=research.FREE_TIER_CANDIDATES), \
             patch.object(research, "discover_articles", return_value=[(URL, "Scam one")]), \
             patch.object(research, "_get_public", return_value=b"html"), \
             patch.object(research, "extract_article", return_value=SOURCE), \
             patch.object(research, "_gemini_json", side_effect=generate):
            paths = self.run_it(limit=1)
        self.assertEqual(len(paths), 1)
        self.assertEqual(chosen, [("gemini-3.5-flash-lite", "writer"),
                                  ("gemini-3.5-flash", "reviewer")])
        state = json.loads(self.state.read_text())
        self.assertEqual(state["reviewer_order"][0], "gemini-3.5-flash")
        self.assertEqual(state["reviewer_order"][-1], "gemma-4-31b-it")
        self.assertEqual(state["generation_calls"], 2)

    def test_cooldown_maps_drop_unsafe_unknown_expired_and_excess_entries(self):
        from datetime import datetime, timedelta, timezone
        checked = datetime.now(timezone.utc)
        active = (checked + timedelta(hours=5)).isoformat()
        expired = (checked - timedelta(seconds=1)).isoformat()
        model_state = {"model_transient_until": {"gemini-3.5-flash-lite": active,
                                                  "unknown-paid-model": active,
                                                  "gemini-3.7-flash": expired}}
        self.assertEqual(research._model_cooldowns(model_state, checked),
                         {"gemini-3.5-flash-lite": active})
        urls = [f"https://consumer.ftc.gov/consumer-alerts/2026/09/fake-listing-{i}"
                for i in range(45)]
        source_state = {"source_rejection_until": {**{url: active for url in urls},
                                                    "https://evil.example/consumer-alerts/2026/09/a": active,
                                                    "https://consumer.ftc.gov/consumer-alerts/2026/09/x?x=1": active,
                                                    "https://consumer.ftc.gov/consumer-alerts/2026/09/old": expired}}
        retained = research._source_cooldowns(source_state, checked)
        self.assertEqual(len(retained), research.MAX_SOURCE_COOLDOWNS)
        self.assertTrue(set(retained).issubset(set(urls)))
        old = {"model_transient_until": {"gemini-3.5-flash-lite": expired}}
        self.assertEqual(research._deprioritize_recent_transients(
            ("gemini-3.5-flash-lite", "gemma-4-26b-a4b-it"), old, checked),
            ("gemini-3.5-flash-lite", "gemma-4-26b-a4b-it"))

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

        def generate(_session, _key, model, prompt, budget, _temperature, _role):
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

    def test_gemma_reviewer_falls_back_within_four_call_budget(self):
        models = ("gemini-3.5-flash-lite", "gemma-4-31b-it", "gemini-3.5-flash",
                  "gemma-4-26b-a4b-it")
        called = []

        def generate(_session, _key, model, _prompt, budget, _temperature, role):
            budget[0] += 1
            called.append((model, role))
            if role == "writer":
                return script(URL)
            if model in ("gemma-4-31b-it", "gemini-3.5-flash"):
                raise research.ModelTransient(503)
            return review()

        with patch.object(research, "_model_names", return_value=models), \
             patch.object(research, "discover_articles", return_value=[(URL, "Scam one")]), \
             patch.object(research, "_get_public", return_value=b"html"), \
             patch.object(research, "extract_article", return_value=SOURCE), \
             patch.object(research, "_gemini_json", side_effect=generate), \
             patch.object(research.time, "sleep") as sleep:
            paths = self.run_it(limit=1)
        self.assertEqual(len(paths), 1)
        self.assertEqual(called, [("gemini-3.5-flash-lite", "writer"),
                                  ("gemma-4-31b-it", "reviewer"),
                                  ("gemini-3.5-flash", "reviewer"),
                                  ("gemma-4-26b-a4b-it", "reviewer")])
        self.assertEqual(json.loads(self.state.read_text())["generation_calls"], 4)
        self.assertEqual(json.loads(paths[0].read_text())["review"]["reviewer"],
                         "gemma-4-26b-a4b-it")
        sleep.assert_called_once_with(2)

    def test_gemma_invalid_json_falls_back_without_persisting_raw_response(self):
        models = ("gemini-3.5-flash-lite", "gemma-4-31b-it", "gemini-3.5-flash")

        def generate(_session, _key, model, _prompt, budget, _temperature, role):
            budget[0] += 1
            if role == "writer":
                return script(URL)
            if model == "gemma-4-31b-it":
                raise research.ModelOutputInvalid("invalid JSON API_KEY_UNSAFE")
            return review()

        with patch.object(research, "_model_names", return_value=models), \
             patch.object(research, "discover_articles", return_value=[(URL, "Scam one")]), \
             patch.object(research, "_get_public", return_value=b"html"), \
             patch.object(research, "extract_article", return_value=SOURCE), \
             patch.object(research, "_gemini_json", side_effect=generate):
            paths = self.run_it(limit=1)
        self.assertEqual(len(paths), 1)
        state = json.loads(self.state.read_text())
        self.assertEqual(state["generation_calls"], 3)
        self.assertEqual(state["generation_attempts"][1],
                         {"model": "gemma-4-31b-it", "status": "invalid_output",
                          "detail": "reviewer: invalid model output"})
        self.assertNotIn("API_KEY_UNSAFE", json.dumps(state))
        self.assertEqual(json.loads(paths[0].read_text())["review"]["reviewer"],
                         "gemini-3.5-flash")

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

    def test_failed_original_narration_review_rejects_script(self):
        weak_review = {**review(), "original_narration": False,
                       "notes": "The narration lightly rearranges the FTC article."}
        with patch.object(research, "_model_names", return_value=research.FREE_TIER_CANDIDATES), \
             patch.object(research, "discover_articles", return_value=[(URL, "Scam one")]), \
             patch.object(research, "_get_public", return_value=b"html"), \
             patch.object(research, "extract_article", return_value=SOURCE), \
             patch.object(research, "_gemini_json", side_effect=[script(URL), weak_review]):
            self.assertEqual(self.run_it(limit=1), [])
        state = json.loads(self.state.read_text())
        self.assertIn("original_narration", state["rejected"][0]["reason"])
        self.assertEqual(list(self.content.glob("*.json")), [])

    def test_unsupported_scene_claim_is_still_rejected_by_reviewer_with_owned_quote(self):
        candidate = script()
        candidate["scenes"][0]["heading"] = "HIJACKED LOGOS"
        for scene in candidate["scenes"]:
            scene.pop("evidence_quote")
            scene["evidence_quote_id"] = 1
        weak_review = {**review(), "scene_evidence_complete": False,
                       "notes": "The cited passage says nothing about hijacked logos.",
                       "first_failure_scene": 1}

        with patch.object(research, "_model_names", return_value=research.FREE_TIER_CANDIDATES), \
             patch.object(research, "discover_articles", return_value=[(URL, "Scam one")]), \
             patch.object(research, "_get_public", return_value=b"html"), \
             patch.object(research, "extract_article", return_value=SOURCE), \
             patch.object(research, "_gemini_json", side_effect=[candidate, weak_review]) as generate:
            self.assertEqual(self.run_it(limit=1), [])
        review_prompt = generate.call_args_list[1].args[3]
        self.assertIn('"heading": "HIJACKED LOGOS"', review_prompt)
        self.assertIn('"evidence_quote":', review_prompt)
        self.assertNotIn("evidence_quote_id", review_prompt.split("Proposed script:")[-1])
        state = json.loads(self.state.read_text())
        self.assertEqual(state["rejected"][0]["reason"],
                         "reviewer rejected: scene 1: scene_evidence_complete")
        self.assertEqual(generate.call_count, 2)

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

        def generate(_session, _key, model, prompt, budget, _temperature, _role):
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

        def generate(_session, _key, model, prompt, budget, _temperature, _role):
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
        for status in (429, 500, 502, 503, 504):
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
        research._gemini_json(session, "test-key", "gemma-4-31b-it", "test", [0], 0.1, "reviewer")
        self.assertEqual(session.post.call_args.kwargs["timeout"], (10, 180))

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

    def test_gemma_uses_documented_thinking_levels_without_json_mime(self):
        response = Mock(status_code=200)
        response.json.return_value = {"candidates": [{"finishReason": "STOP", "content": {
            "parts": [{"thought": True, "text": "Internal reasoning is not JSON."},
                      {"text": '```json\n{"approved": true}\n```'}]}}]}
        session = Mock()
        session.post.return_value = response
        for model, role, level in (("gemma-4-31b-it", "reviewer", "high"),
                                   ("gemma-4-26b-a4b-it", "writer", "minimal")):
            with self.subTest(model=model, role=role):
                self.assertEqual(research._gemini_json(session, "test-key", model, "test",
                                                        [0], 0.1, role), {"approved": True})
                config = session.post.call_args.kwargs["json"]["generationConfig"]
                self.assertEqual(config["thinkingConfig"], {"thinkingLevel": level})
                self.assertNotIn("responseMimeType", config)

    def test_gemma_json_parser_rejects_prose_around_fenced_object(self):
        response = Mock(status_code=200)
        response.json.return_value = {"candidates": [{"finishReason": "STOP", "content": {
            "parts": [{"text": 'Here is the result:\n```json\n{"approved": true}\n```'}]}}]}
        session = Mock()
        session.post.return_value = response
        with self.assertRaisesRegex(research.ResearchError, "invalid JSON"):
            research._gemini_json(session, "test-key", "gemma-4-31b-it", "test", [0], 0.1,
                                  "reviewer")

    def test_gemma_catalog_ids_require_live_generate_content_listing(self):
        session = Mock()
        session.get.return_value.json.return_value = {"models": [
            {"name": "models/gemma-4-31b-it", "supportedGenerationMethods": ["generateContent"]},
            {"name": "models/gemma-4-26b-a4b-it", "supportedGenerationMethods": ["generateContent"]},
            {"name": "models/gemini-3.5-flash-lite", "supportedGenerationMethods": ["embedContent"]},
        ]}
        self.assertEqual(research._model_names(session, "test-key"),
                         ("gemma-4-31b-it", "gemma-4-26b-a4b-it"))

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
