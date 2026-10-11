import lzma, struct, tempfile, unittest
import pandas as pd
import goldbot.providers.dukascopy as m
from goldbot.providers.dukascopy import DukascopyHistory


class Resp:
    def __init__(self, code, content=b"", headers=None):
        self.status_code, self.content, self.headers = code, content, headers or {}


class Sess:
    def __init__(self, fn):
        self.fn, self.calls, self.headers = fn, [], {}

    def get(self, url, timeout=None):
        self.calls.append(url)
        return self.fn(url)


def blob(n=3):
    return lzma.compress(b"".join(struct.pack(">IIIff", 1000 + 20000 * k, 2650500 + k, 2650000 + k, 1.0, 1.0) for k in range(n)))


# ساعات قديمة مكتملة (تُخزَّن في الكاش)
H0 = pd.Timestamp("2026-10-06 08:00", tz="UTC")
END = H0 + pd.Timedelta(hours=10)


class Base(unittest.TestCase):
    def setUp(self):
        self.sleeps = []
        self._real = m.time.sleep
        m.time.sleep = lambda x: self.sleeps.append(x)

    def tearDown(self):
        m.time.sleep = self._real


class TestHourCache(Base):
    def test_successful_hours_are_not_downloaded_twice(self):
        s = Sess(lambda u: Resp(200, blob()))
        d = DukascopyHistory({"workers": 1}, session=s)
        a = d.fetch("1m", H0, END)
        first = len(s.calls)
        b = d.fetch("1m", H0, END)
        self.assertEqual(len(s.calls), first)                     # الثانية كلها من الكاش
        self.assertEqual(len(a), len(b))
        self.assertEqual(first, 10)

    def test_disk_cache_survives_new_instance(self):
        cd = tempfile.mkdtemp()
        s1 = Sess(lambda u: Resp(200, blob()))
        DukascopyHistory({"workers": 1, "cache_dir": cd}, session=s1).fetch("1m", H0, END)
        s2 = Sess(lambda u: Resp(429))
        df = DukascopyHistory({"workers": 1, "cache_dir": cd}, session=s2).fetch("1m", H0, END)   # حتى لو حُجبنا الآن
        self.assertEqual(s2.calls, [])
        self.assertFalse(df.empty)

    def test_old_missing_hours_are_remembered(self):
        s = Sess(lambda u: Resp(404))
        d = DukascopyHistory({"workers": 1}, session=s)
        d.fetch("1m", H0, END)
        n = len(s.calls)
        d.fetch("1m", H0, END)
        self.assertEqual(len(s.calls), n)

    def test_current_hour_never_cached(self):
        now = pd.Timestamp.now(tz="UTC").floor("h")
        s = Sess(lambda u: Resp(200, blob()))
        d = DukascopyHistory({"workers": 1}, session=s)
        d.fetch("1m", now, now + pd.Timedelta(hours=1))
        d.fetch("1m", now, now + pd.Timedelta(hours=1))
        self.assertEqual(len(s.calls), 2)


class TestThrottling(Base):
    def test_does_not_hammer_when_blocked(self):
        s = Sess(lambda u: Resp(429))
        d = DukascopyHistory({"workers": 1, "retries": 2, "abort_after_throttles": 4}, session=s)
        with self.assertRaises(RuntimeError) as cm:
            d.fetch("1m", H0, H0 + pd.Timedelta(hours=100))
        self.assertLessEqual(len(s.calls), 6)                     # توقف مبكر بدل 200 طلب
        self.assertIn("429", str(cm.exception))
        self.assertEqual(d.progress[0], d.progress[1])

    def test_retry_after_header_is_respected_and_capped(self):
        seq = iter([Resp(429, headers={"Retry-After": "7"}), Resp(200, blob())])
        s = Sess(lambda u: next(seq))
        d = DukascopyHistory({"workers": 1, "retries": 3}, session=s)
        df = d.fetch("1m", H0, H0 + pd.Timedelta(hours=1))
        self.assertFalse(df.empty)
        self.assertTrue(any(abs(x - 7.0) < 0.5 for x in self.sleeps), self.sleeps)
        d2 = DukascopyHistory({"workers": 1, "retries": 2, "abort_after_throttles": 99}, session=Sess(lambda u: Resp(429, headers={"Retry-After": "9999"})))
        with self.assertRaises(RuntimeError):
            d2.fetch("1m", H0, H0 + pd.Timedelta(hours=1))
        self.assertTrue(all(x <= 60.0 for x in self.sleeps))

    def test_http_error_does_not_double_load_via_http_scheme(self):
        s = Sess(lambda u: Resp(429))
        d = DukascopyHistory({"workers": 1, "retries": 3, "abort_after_throttles": 99}, session=s)
        with self.assertRaises(RuntimeError):
            d.fetch("1m", H0, H0 + pd.Timedelta(hours=1))
        self.assertTrue(all(u.startswith("https://") for u in s.calls))
        self.assertEqual(len(s.calls), 3)

    def test_partial_data_is_rejected_but_progress_is_kept(self):
        bad = {H0 + pd.Timedelta(hours=h) for h in range(5, 10)}   # نصف الساعات تفشل
        def fn(u):
            hh = int(u.split("/")[-1][:2])
            return Resp(429) if (H0.replace(hour=hh) in bad) else Resp(200, blob())
        s = Sess(fn)
        d = DukascopyHistory({"workers": 1, "retries": 1, "abort_after_throttles": 99}, session=s)
        with self.assertRaises(RuntimeError) as cm:
            d.fetch("1m", H0, END)
        self.assertIn("جزئي", str(cm.exception))
        d.fn = None
        s.fn = lambda u: Resp(200, blob())                         # الحجب انتهى
        before = len(s.calls)
        df = d.fetch("1m", H0, END)
        self.assertEqual(len(s.calls) - before, 5)                 # يكمل الناقص فقط
        self.assertFalse(df.empty)

    def test_few_errors_among_many_ok_is_accepted(self):
        n = {"i": 0}
        def fn(u):
            n["i"] += 1
            return Resp(500) if n["i"] == 1 else Resp(200, blob())
        d = DukascopyHistory({"workers": 1, "retries": 1}, session=Sess(fn))
        self.assertFalse(d.fetch("1m", H0, H0 + pd.Timedelta(hours=20)).empty)


class TestBarsEquivalence(Base):
    def test_result_matches_tick_level_resample(self):
        s = Sess(lambda u: Resp(200, blob(10)))
        d = DukascopyHistory({"workers": 1}, session=s)
        df = d.fetch("1m", H0, H0 + pd.Timedelta(hours=1))
        ticks = DukascopyHistory.decode_ticks(blob(10), H0)
        ref = ticks["close"].resample("1min").ohlc().dropna()
        self.assertEqual(len(df), len(ref))
        self.assertAlmostEqual(float(df.iloc[0].open), float(ref.iloc[0].open))
        self.assertAlmostEqual(float(df.iloc[-1].close), float(ref.iloc[-1].close))


if __name__ == "__main__":
    unittest.main()
