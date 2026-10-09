# بحث مصادر أسعار الذهب المجانية — 2026-10-09

## الخلاصة التنفيذية

لا يوجد مصدر مجاني مجهول وموثوق يضمن XAU/USD الحقيقي على M1/M5 بلا حساب أو مفتاح أو حدود استخدام. أفضل خيار مجاني فعلي لبوت إشارات هو **OANDA Practice**: حساب تجريبي مجاني، بث أسعار، وواجهة شموع M1/M5، مع حدود API تشغيلية بدل حصة يومية صغيرة. السعر هو سعر OANDA لأداة XAU_USD/CFD وليس سعرًا مركزيًا عالميًا.

## القرار

- **المصدر الأساسي:** OANDA Practice REST candles + pricing stream.
- **الاحتياطي:** SiftingIO إذا بقيت مفاتيحه موجودة أو عند فشل OANDA.
- **مرفوض:** Yahoo Finance وStooq كمصدر لحظي؛ غير موثقين كواجهة XAU/USD M1/M5 مناسبة لبوت.
- **غير كافٍ للإشارات السريعة على المجاني:** Alpha Vantage، Metals.Dev، GoldAPI.io بسبب حصص شهرية صغيرة أو عدم وجود شموع M1/M5 أصلية.
- **مشروط:** Twelve Data؛ يدعم XAU/USD و1m/5m، لكن صفحات التسعير متعارضة حول إتاحة السلعة في Basic وحدود 800 طلب/يوم.
- **غير مناسب مجانًا:** Finnhub؛ شموع الفوركس تتطلب Premium ولا يثبت توفر XAU/USD M1/M5 في Free.

## المصادر الرسمية

- OANDA Practice API: https://developer.oanda.com/rest-live-v20/development-guide/
- OANDA pricing stream: https://developer.oanda.com/rest-live-v20/pricing-ep/
- OANDA instruments/candles: https://developer.oanda.com/rest-live-v20/instrument-ep/
- Twelve Data commodities: https://twelvedata.com/commodities
- Twelve Data pricing: https://twelvedata.com/pricing.md
- Alpha Vantage gold: https://www.alphavantage.co/documentation/#gold-silver-spot
- Finnhub API/pricing: https://finnhub.io/docs/api and https://finnhub.io/pricing
- Metals.Dev docs/pricing: https://metals.dev/docs and https://metals.dev/pricing
- GoldAPI: https://www.goldapi.io/openapi.json
- Yahoo Finance terms/data: https://legal.yahoo.com/us/en/yahoo/terms/otos/index.html
- Stooq: https://stooq.com/q/?s=xauusd and https://stooq.com/terms.html

## حدود مهمة

- OANDA stream ليس كل tick بلا فقد: يرسل بحد أقصى 4 أسعار/ثانية للأداة، لكنه real-time لدى الوسيط.
- OANDA Practice يتطلب Account ID وPersonal Access Token.
- لا توجد استراتيجية تضمن الربح. أفضل تصميم قابل للاختبار هو اتجاه 15m/5m مع تأكيد M1، منع التداول في التعارض والأخبار والسبريد العالي، ووقف ATR ثابت المخاطرة.
- لا يتم تشغيل التداول الحقيقي أو تنفيذ أوامر؛ GOLDNBOY يرسل إشارات فقط.
