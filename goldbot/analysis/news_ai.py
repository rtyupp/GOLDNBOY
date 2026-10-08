"""مصدر احتياطي للأخبار: Gemini مع بحث Google على الويب (عند تعطل Forex Factory).
⚠️ أقل موثوقية من التقويم الرسمي (قد يخطئ في الأوقات): يُستخدم فقط كاحتياط ويُعلَّم مصدره في /status."""
from __future__ import annotations
import json, re
import pandas as pd
from goldbot.analysis.news import COLS, _empty, _is_key

PROMPT = (
    "ابحث في الويب عن جدول الأحداث الاقتصادية الأمريكية عالية التأثير على الذهب خلال الأيام السبعة القادمة بدءًا من {now} UTC "
    "(CPI, NFP, FOMC, PCE, قرار الفائدة, خطابات رئيس الفيدرالي, الناتج المحلي, البطالة). "
    "استخدم مصادر رسمية أو تقويمات معروفة (BLS, BEA, Federal Reserve, Investing, ForexFactory). "
    "إن لم تتأكد من الموعد بدقة فلا تُدرج الحدث. حوّل كل الأوقات إلى UTC. "
    'أجب بمصفوفة JSON فقط بلا أي شرح: [{{"date_utc":"YYYY-MM-DD HH:MM","name":"CPI m/m","impact":"high"}}]'
)


def parse_ai_events(text: str, currencies=("USD",)) -> pd.DataFrame:
    t = re.sub(r"^```(?:json)?|```$", "", (text or "").strip(), flags=re.M).strip()
    m = re.search(r"\[.*\]", t, re.S)
    if not m:
        return _empty()
    try:
        items = json.loads(m.group(0))
    except ValueError:
        return _empty()
    rows = []
    for it in items if isinstance(items, list) else []:
        try:
            dt = pd.to_datetime(it.get("date_utc"), utc=True)
            name = str(it.get("name", "")).strip()
            impact = str(it.get("impact", "high")).lower()
            if name and not pd.isna(dt) and _is_key(name, impact if impact in ("high", "medium") else "high", "USD", currencies):
                rows.append({"dt": dt, "name": name, "impact": impact, "currency": "USD", "forecast": "", "previous": ""})
        except Exception:
            continue
    return pd.DataFrame(rows, columns=COLS) if rows else _empty()


def fetch_via_gemini(gemini, now: pd.Timestamp, currencies=("USD",)) -> pd.DataFrame:
    """متزامن. يستخدم أداة google_search في Gemini."""
    body = {"contents": [{"role": "user", "parts": [{"text": PROMPT.format(now=now.strftime("%Y-%m-%d %H:%M"))}]}],
            "tools": [{"google_search": {}}], "generationConfig": {"temperature": 0.0}}
    resp = gemini.generate(body)
    parts = resp["candidates"][0].get("content", {}).get("parts", [])
    text = "".join(p.get("text", "") for p in parts if "text" in p)
    df = parse_ai_events(text, currencies)
    return df[df["dt"] >= now - pd.Timedelta(hours=1)].reset_index(drop=True)
