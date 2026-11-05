"""Venue API contracts and referers without live requests."""
import copy
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs

import auto_grab


class RequestHeaderTests(unittest.TestCase):
    def call(self, path, **kwargs):
        transport = Mock()
        with patch.object(auto_grab, "get_transport", return_value=transport):
            auto_grab._req(path, cookie="synthetic", **kwargs)
        return transport.request_json.call_args

    def test_venue_gets_use_their_own_resource_id(self):
        for path, resource in [("resource-info", 59), ("resource-info-margin", 84)]:
            call = self.call("/site/reservation/" + path, params={"resource_id": resource})
            self.assertEqual(call.kwargs["headers"]["Referer"],
                             auto_grab.BASE + f"/v2/reserve/reserveDetail?id={resource}")

    def test_launch_uses_form_resource_without_changing_payload(self):
        form = {"resource_id": "85", "data": '[{"period":1,"sub_resource_id":2}]'}
        original = copy.deepcopy(form)
        call = self.call("/site/reservation/launch", form=form, params={"resource_id": 57})
        self.assertEqual(call.kwargs["headers"]["Referer"],
                         auto_grab.BASE + "/v2/reserve/reserveDetail?id=85")
        self.assertEqual(parse_qs(call.kwargs["data"].decode()), {key: [value] for key, value in form.items()})
        self.assertEqual(form, original)

    def test_global_calls_do_not_inherit_a_venue_or_guess_from_other_ids(self):
        self.call("/site/reservation/resource-info", params={"resource_id": 84})
        for path in ("/site/captcha/click-get", "/site/reservation/my-list", "/site/payment-order/my-list"):
            call = self.call(path, params={"resource_id": 59, "app_id": 616, "id": 123})
            self.assertEqual(call.kwargs["headers"]["Referer"], auto_grab.BASE + "/v2/reserve/reserveHall")

    def test_invalid_or_missing_resource_ids_use_the_hall(self):
        for value in (None, "", "bad", "84\r\nCookie: wrong", True, -1, 0, 1.5):
            call = self.call("/site/reservation/resource-info", params={"resource_id": value})
            self.assertEqual(call.kwargs["headers"]["Referer"], auto_grab.BASE + "/v2/reserve/reserveHall")

    def test_live_availability_only_uses_requested_date(self):
        cells = [{"time_id": index, "sub_id": 2, "abscissa": "1号场", "yaxis": "20:10-21:10",
                  "date": day, "row": {"status": 5, "margin": 1, "total": 1}}
                 for index, day in enumerate(("2026-11-04", "2026-11-05"), 1)]
        with patch.object(auto_grab, "_req", return_value={"e": 0, "d": {"synthetic": cells}}):
            slots = auto_grab.fetch_slots("fake", 57, "2026-11-05")
        self.assertEqual([slot["period"] for slot in slots], [2])

    def test_live_availability_without_a_date_is_not_used_for_booking(self):
        cell = {"time_id": 1, "sub_id": 2, "abscissa": "1号场", "yaxis": "20:10-21:10",
                "row": {"status": 5, "margin": 1, "total": 1}}
        with patch.object(auto_grab, "_req", return_value={"e": 0, "d": {"synthetic": [cell]}}):
            self.assertEqual(auto_grab.fetch_slots("fake", 57, "2026-11-05"), [])


if __name__ == "__main__":
    unittest.main()
