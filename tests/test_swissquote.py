import unittest
from goldbot.providers.swissquote import SwissquotePublic


class TestSwissquote(unittest.TestCase):
    def test_parse_median_bid_ask_and_timestamp(self):
        payload = [
            {"topo": {"server": "Live5"}, "ts": 1791538512070,
             "spreadProfilePrices": [{"spreadProfile": "elite", "bid": 4184.583, "ask": 4185.087}]},
            {"topo": {"server": "Live7"}, "ts": 1791538512070,
             "spreadProfilePrices": [{"spreadProfile": "elite", "bid": 4184.583, "ask": 4185.087}]},
        ]
        t = SwissquotePublic.parse(payload, 1791538513000)
        self.assertIsNotNone(t)
        self.assertAlmostEqual(t.bid, 4184.583)
        self.assertAlmostEqual(t.ask, 4185.087)
        self.assertEqual(t.ts_ms, 1791538512070)

    def test_invalid_payload_is_ignored(self):
        self.assertIsNone(SwissquotePublic.parse([], 1))
        self.assertIsNone(SwissquotePublic.parse([{"spreadProfilePrices": []}], 1))
