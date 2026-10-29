"""Venue-specific behavior uses rules and the confirmed receipt, never a name."""
from datetime import datetime
from unittest.mock import Mock
import unittest

import test_booking_flow as flow
from test_booking_catalog import info


class VenueFlowTests(unittest.TestCase):
    def test_free_nonhour_booking_skips_captcha_and_payment(self):
        engine, solver, payment = Mock(), Mock(), Mock(side_effect=AssertionError("free"))
        gym = info(57, 1, 0)  # ID deliberately differs from the real gym.
        slot = dict(flow.SLOT, court="", time="20:00-21:30", sub=0, price=0)
        code, calls, output, _ = flow.FlowTests().run_flow(datetime(2026, 10, 29, 22),
            resource=gym, times="20:00-21:30", fetch=lambda *a: [slot],
            result_fields={"status": 7, "pay_money": "0.00"}, payment=True,
            pay_mode="http", engine=engine, solver=solver, http_payment=payment)
        self.assertEqual(code, 0)
        self.assertEqual(len(calls), 1)
        engine.assert_not_called(); solver.assert_not_called(); payment.assert_not_called()
        self.assertIn("生效", output)

    def test_zero_fee_pending_receipt_never_creates_payment_order(self):
        payment = Mock(side_effect=AssertionError("must not create zero-fee order"))
        code, calls, output, _ = flow.FlowTests().run_flow(datetime(2026, 10, 29, 22),
            resource=info(57, 1, 0), times="19:10-20:10",
            result_fields={"status": 6, "pay_money": "0.00"}, payment=True,
            pay_mode="http", http_payment=payment)
        self.assertEqual(code, 1)
        self.assertEqual(len(calls), 1)
        payment.assert_not_called()
        self.assertIn("免费", output)

    def test_gym_rule_blocks_multiple_before_any_submission(self):
        code, calls, output, _ = flow.FlowTests().run_flow(datetime(2026, 10, 29, 22),
            resource=info(57, 1, 0), times="20:10-21:10;21:10-22:10")
        self.assertEqual(code, 1)
        self.assertEqual(calls, [])
        self.assertIn("最多", output)

    def test_specific_court_does_not_fall_back_to_another_court(self):
        slots = [dict(flow.SLOT, court="Court 1"), dict(flow.SLOT, court="Court 2")]
        code, calls, _, _ = flow.FlowTests().run_flow(datetime(2026, 10, 29, 22),
            fetch=lambda *a: slots, court="Court 2")
        self.assertEqual(code, 0)
        self.assertEqual([x[2] for x in calls], ["Court 2"])


if __name__ == "__main__":
    unittest.main()
