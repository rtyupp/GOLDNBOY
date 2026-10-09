# بحث مصادر أسعار الذهب المجانية — 2026-10-09

## الخلاصة التنفيذية

أفضل حل مجاني فعلي وجدته للبث اللحظي هو **Swissquote Public Quotes**: endpoint عام يعيد Bid/Ask لـ XAU/USD وتوقيتًا دون API key أو حساب. لا يقدم شموعًا جاهزة، لذلك يبني GOLDNBOY شموع M1/M5 محليًا من اللقطات. التاريخ التاريخي يبقى من SiftingIO عند الحاجة. هذا سعر مرجعي من مزود وليس ضمان سعر تنفيذ لدى أي وسيط.

## القرار

- **المصدر اللحظي الأساسي:** Swissquote Public Quotes: https://forex-data-feed.swissquote.com/public-quotes/bboquotes/instrument/XAU/USD
- **التاريخ:** SiftingIO REST، بتحميل 28 يومًا عند الحاجة وليس polling لحظيًا.
- **الخيار الاختياري:** OANDA Practice، لكنه يحتاج حسابًا/توكنًا ولا يعتمد عليه الإعداد الافتراضي.
- **مرفوض:** Yahoo Finance وStooq كمصدر لحظي؛ غير موثقين كواجهة XAU/USD M1/M5 مناسبة لبوت.
- **غير كافٍ للإشارات السريعة على المجاني:** Alpha Vantage، Metals.Dev، GoldAPI.io بسبب حصص شهرية صغيرة أو عدم وجود شموع M1/M5 أصلية.
- **مشروط:** Twelve Data؛ يدعم XAU/USD و1m/5m، لكن صفحات التسعير متعارضة حول إتاحة السلعة في Basic وحدود 800 طلب/يوم.
- **غير مناسب مجانًا:** Finnhub؛ شموع الفوركس تتطلب Premium ولا يثبت توفر XAU/USD M1/M5 في Free.

## المصادر الرسمية

- OANDA Practice API: https://developer.oanda.com/rest-live-v20/development-guide/
- OANDA pricing stream: https://developer.oanda.com/rest-live-v20/pricing-ep/
- OANDA instruments/candles: https://developer.oanda.com/rest-live-v20/instrument-ep/
- Swissquote public XAU/USD quote endpoint: https://forex-data-feed.swissquote.com/public-quotes/bboquotes/instrument/XAU/USD
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
- Swissquote لا يوثق في endpoint العام حدًا رقميًا للطلبات؛ لذلك الإعداد المحافظ يطلب كل 5 ثوانٍ، ويطبق backoff عند الفشل، ولا يدّعي أن المصدر feed تنفيذي مضمون.
- لا توجد استراتيجية تضمن الربح. أفضل تصميم قابل للاختبار هو اتجاه 15m/5m مع تأكيد M1، منع التداول في التعارض والأخبار والسبريد العالي، ووقف ATR ثابت المخاطرة.
- لا يتم تشغيل التداول الحقيقي أو تنفيذ أوامر؛ GOLDNBOY يرسل إشارات فقط.
