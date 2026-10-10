# GOLDNBOY — بوت إشارات الذهب XAUUSD

بوت تيليجرام لتحليل الذهب بالأسعار اللحظية والاستراتيجيات والمؤشرات الفنية وإدارة المخاطر والتتبع. القرار محلي بالكامل ولا يعتمد على الذكاء الاصطناعي أو الأخبار.

## التشغيل

```bash
pip install -r requirements.txt
cp .env.example .env
python -m goldbot check
python -m goldbot backtest --tf all --days 60 --store
python -m goldbot run
```

## واجهة تيليجرام

تظهر لوحة مفاتيح سفلية ثابتة عبر `ReplyKeyboardMarkup` بأزرار السعر، الحالة، التحليل، نظرة السوق، آخر إعداد والمساعدة. رسالة البداية: **ارحبو تراحيب المطر**.

## القرار والاستراتيجيات

المسار: Swissquote/Dukascopy → شموع ومؤشرات → هيكل السوق والسيولة والمناطق → الاستراتيجيات → التقاطع → الاحتمال التاريخي → المخاطر → Telegram. تشمل الاستراتيجيات الحالية Trend Pullback وLiquidity Sweep و**SMC Sweep + FVG/OB** وBreakout وReversal وVWAP، مع الحفاظ على BOS/CHoCH والسيولة وإدارة TP1/TP2/SL.

استراتيجية **SMC Sweep + FVG/OB** لا تدخل إلا بعد سحب سيولة مؤكد، ثم BOS/CHoCH، ثم إعادة اختبار FVG أو Order Block حديثة، مع فلتر Premium/Discount وجلسة لندن/نيويورك. التفاصيل والمصادر المرجعية موثقة في [REFERENCE_REVIEW.md](REFERENCE_REVIEW.md).

## الاختبار التاريخي

يدعم backtest السبريد والانزلاق السعري عبر إعدادات `backtest.spread` و`backtest.slippage`، ويستخدم نفس مسار التحليل الحي لتقليل تسريب المستقبل. النتائج تقديرية وليست ضمانًا للربحية.

## Render

يعمل عبر Docker و`render.yaml`. أضف `TELEGRAM_BOT_TOKEN` و`TELEGRAM_ADMIN_IDS` في Environment، ولا ترفع `.env` أو أي أسرار.

## المشاريع المرجعية

تم فحص المشاريع الأربعة المذكورة في الطلب. لم يُنسخ كود من مستودعات بلا ترخيص واضح؛ جرى الاعتماد على مكونات GOLDNBOY الحالية المرخّصة/المملوكة للمشروع، مع الحفاظ على قابلية الاختبار.
