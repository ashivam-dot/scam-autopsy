"""The daily refill has a hard stock cap and independent retry triggers."""

import unittest
from datetime import date

from scam_autopsy.refill_decision import decide


TUESDAY = date(2026, 10, 6)
MONDAY = date(2026, 10, 5)


class RefillDecisionTests(unittest.TestCase):
    def test_low_stock_runs_even_after_success(self):
        result = decide(today=TUESDAY, force=False, research_status="passed", ready_queue=3)
        self.assertTrue(result.refill)
        self.assertEqual(result.limit, 2)
        self.assertEqual(result.reasons, ("low_stock",))

    def test_failure_retries_when_stock_is_above_low_mark(self):
        result = decide(today=TUESDAY, force=False, research_status="failed", ready_queue=5)
        self.assertTrue(result.refill)
        self.assertEqual(result.limit, 2)
        self.assertIn("previous_research_failed", result.reasons)

    def test_monday_and_force_each_trigger_but_respect_cap(self):
        monday = decide(today=MONDAY, force=False, research_status="passed", ready_queue=7)
        forced = decide(today=TUESDAY, force=True, research_status="passed", ready_queue=7)
        self.assertEqual((monday.limit, forced.limit), (1, 1))
        self.assertTrue(monday.refill and forced.refill)
        full = decide(today=MONDAY, force=True, research_status="failed", ready_queue=8)
        self.assertFalse(full.refill)
        self.assertEqual(full.limit, 0)

    def test_healthy_stock_skips_non_monday(self):
        result = decide(today=TUESDAY, force=False, research_status="passed", ready_queue=4)
        self.assertFalse(result.refill)
        self.assertEqual(result.limit, 0)


if __name__ == "__main__":
    unittest.main()
