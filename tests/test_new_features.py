import asyncio
import unittest

from goldbot.ai.gemini_layer import GeminiLayer
from goldbot.analysis.news_feed import NewsAggregator
from goldbot.config import Cfg
from goldbot.notify.telegram import CommandBot


class FakeResponse:
    def __init__(self, body):
        self.content = body

    def raise_for_status(self):
        return None


class FakeSession:
    def get(self, url, **kwargs):
        return FakeResponse(b'''<?xml version="1.0"?><rss><channel>
        <item><title>Gold rises on dollar weakness</title><link>https://example.com/gold</link>
        <pubDate>Wed, 07 Oct 2026 12:00:00 GMT</pubDate><description>Markets update</description></item>
        </channel></rss>''')


class FakeTG:
    def __init__(self):
        self.messages, self.photos = [], []
        self.admin_ids = [7]

    def send_message(self, chat, text):
        self.messages.append((chat, text))

    def send_photo(self, chat, photo, caption):
        self.photos.append((chat, photo, caption))


class TestNewFeatures(unittest.TestCase):
    def test_rss_is_normalized_and_deduplicated(self):
        feed = NewsAggregator([{"name": "Test", "url": "https://example.com/rss"}], session=FakeSession())
        self.assertEqual(len(feed.refresh(force=True)), 1)
        self.assertIn("Gold rises", feed.format_ar())
        self.assertEqual(feed.refresh(), feed.items)

    def test_chat_uses_existing_adapter(self):
        cfg = Cfg({"ai": {"enabled": True, "model": "test", "timeout_sec": 2}})
        layer = GeminiLayer(cfg)
        layer.adapter = lambda prompt: "شرح واضح من المساعد"
        self.assertIn("شرح واضح", asyncio.run(layer.chat("ما هو الشارت؟", "السوق")))

    def test_command_bot_can_send_photo_payload(self):
        tg = FakeTG()
        bot = CommandBot(tg, {"/chart": lambda args: {"text": "تفاصيل", "photo": b"png", "caption": "chart"}})
        bot.process_update({"update_id": 1, "message": {"from": {"id": 7}, "chat": {"id": 9}, "text": "/chart 5m"}})
        self.assertEqual(tg.messages, [(9, "تفاصيل")])
        self.assertEqual(tg.photos[0][0], 9)


if __name__ == "__main__":
    unittest.main()
