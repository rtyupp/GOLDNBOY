# Gold XAU/USD Signals Bot  (بوت إشارات الذهب)

نظام يحلل الذهب لحظيًا ويرسل **فقط** الفرص التي تتفق عليها كل الفلاتر إلى محادثتك الخاصة في تيليجرام (والقناة لاحقًا)، ويقول «لا صفقة» ويشرح السبب في كل الحالات الأخرى.

```
SiftingIO WebSocket ─► TickStore (عمر البيانات) ─► CandleBuilder 1m ─► 3m 5m 15m 30m 1H 4H 1D
History (REST/CSV) ──────────────────────────────► MarketData ─► Indicators (ta) ─► Structure ─► Liquidity
─► Supply/Demand ─► Multi-TF ─► 5 Strategies ─► Confluence ─► Historical Probability ─► Risk
─► Gemini (الموجود عندك) ─► BUY / SELL / NO TRADE ─► Telegram + Chart ─► TP1/TP2/SL tracking ─► Journal
```

## 1) التشغيل المحلي (5 دقائق)

```bash
pip install -r requirements.txt
cp .env.example .env        # املأ المفاتيح، ثم:  export $(grep -v '^#' .env | xargs)
python -m goldbot check     # يتأكد من SiftingIO + Telegram + Gemini (والنتائج بالعربي)
python -m goldbot backtest --tf all --days 60 --store   # يبني سجل الاحتمالات التاريخية (مرة أولى)
python -m goldbot run
```

**ملاحظة مهمة:** `probability.block_if_insufficient: true` افتراضيًا. قبل تشغيل `backtest --store` لن يرسل البوت إشارات (السبب يظهر في الـLog: `PROBABILITY_UNAVAILABLE`). إن أردت إرسالًا بدون هذا الشرط غيّرها إلى `false` (سيكتب "Insufficient history" بدل نسبة).

### Telegram (الإشارات تصلك في الخاص)
1. `@BotFather` ← `/newbot` ← خذ `TELEGRAM_BOT_TOKEN`.
2. افتح البوت الذي أنشأته واضغط **Start** (ضروري: تيليجرام لا يسمح للبوت ببدء المحادثة معك).
3. اكتب للبوت `/id` فيرد برقم حسابك ← ضعه في `TELEGRAM_ADMIN_IDS` (يمكن أكثر من رقم مفصولة بفاصلة).
4. الإشارات وتنبيهات TP1/TP2/SL تصل إلى **محادثتك الخاصة**، وكذلك الأوامر. لا حاجة لقناة الآن.
5. لاحقًا للقناة: أضف البوت مشرفًا في القناة، ضع `TELEGRAM_CHANNEL_ID`، وغيّر في `config.yaml`: `telegram.signal_target: channel` (أو `both` للاثنين).

الأوامر: `/status /price /analysis /signal /stats /history /last /market /session /news /chart /ask /id /help`. كل الردود والرسائل واللوغات بالعربي؛ الشارت أيضًا (أسماء الأوامر لاتينية لأن تيليجرام يشترط ذلك). `/chart` يرسل صورة مع التفاصيل، و`/ask` أو أي رسالة عادية يفتحان محادثة تفهم خريطة الكود وحالة السوق.

### Gemini 3.8 Flash (الأقوى مجانًا)
النموذج الافتراضي `gemini-3.8-flash`: بحسب صفحة أسعار Google (سبتمبر 2026) هو أقوى نموذج متاح في **الطبقة المجانية** (المجاني: Flash وFlash-Lite فقط؛ نماذج Pro مثل 3.1 Pro غير مجانية). يُغيَّر من `ai.model` أو `GEMINI_MODEL`.
- **بديل تلقائي:** `ai.fallback_model: gemini-3.7-flash` (مجاني أيضًا) يُستخدم إذا أوقفت Google النموذج الأساسي، مع تنبيه عربي لك مرة واحدة.
- المفتاح مجاني من Google AI Studio (`GEMINI_API_KEY`). أو اربط دالتك الحالية بدون تغيير: `ai.adapter: "your_module:your_function"`.
- Gemini يستلم بطاقة محسوبة ويرد JSON (`BUY/SELL/NO TRADE` + سبب بالعربي)؛ **لا يستطيع تغيير الدخول/SL/TP**. أي خطأ أو رد غير مفهوم = «لا صفقة».
- ⚠️ الطبقة المجانية: حدود طلبات (RPM/RPD) غير مضمونة، وGoogle قد تستخدم محتوى الطلبات لتحسين منتجاتها (البطاقة تحليل سوق فقط بلا بيانات شخصية). البوت لا يستدعي Gemini إلا عند وجود إعداد اجتاز كل الفلاتر، فاستهلاكه قليل جدًا.
- لم أجرّب الاتصال بـ `gemini-3.8-flash` فعليًا (لا إنترنت هنا)؛ نفس نقطة `generateContent` التي تعمل مع 3.5/3.6. شغّل `python -m goldbot check` للتأكد.
- نماذج 2.5 (ومنها flash) مجدولة للإيقاف 16 أكتوبر 2026، لذلك لم أعد أستخدمها.

### الأخبار
املأ `data/events.csv` (UTC): `datetime_utc,name,impact`. قبل الخبر 30 دقيقة = `NO TRADE`، وبعده 15 دقيقة انتظار. يمكنك وضع CSV على رابط raw (GitHub مثلًا) وتحديد `EVENTS_CSV_URL` فيُعاد تحميله كل ساعة. `/status` يحذّرك إن كان الملف فارغًا.

يوجد الآن مجمّع أخبار RSS/Atom اختياري في `news.sources` يعرض آخر العناوين والمصادر والروابط في `/news` ويغذي المساعد. هذه الأخبار للاطلاع والشرح فقط؛ لا تُستخدم تلقائيًا كحظر تداول، ويبقى `events.csv` مصدر الحظر الزمني الموثوق. يمكنك حذف أي مصدر أو إضافة مصدر RSS آخر من `config/config.yaml`.

### المساعد الذكي والشارت عند الطلب
المساعد يستخدم نفس طبقة Gemini الحالية لكنه في وضع محادثة منفصل. يرسل له خريطة وحدات `goldbot` ومقتطف التوثيق وحالة السوق والأخبار، وليس مفاتيح البيئة أو ملفات الأسرار. لذلك يستطيع شرح `Pipeline` والمؤشرات والأوامر وأسباب «لا صفقة» ضمن السياق المتاح. لا يستطيع تعديل مستويات المخاطر أو تنفيذ صفقة. شغّل `/ask اشرح لي سبب القرار الحالي` أو أرسل سؤالًا عاديًا. استخدم `/chart 5m` أو `/chart 15m` لإرسال الشارت مع ملخص الاتجاه والهيكل والسيولة والخطة إن وجدت.

## 2) النشر على Render (أو أي سيرفر)

1. ارفع المجلد إلى GitHub (الملف `.env` ممنوع، `.gitignore` يحميه).
2. Render ← New ← **Blueprint** ← اختر المستودع (يقرأ `render.yaml`)، أو New Web Service ← Docker.
3. أضف المتغيرات في Environment (القائمة في `.env.example`).
4. أول مرة: افتح Shell في Render ونفّذ `python -m goldbot backtest --tf all --days 60 --store` ثم أعد التشغيل.

**تنبيهات صادقة عن Render:**
- الخطة المجانية **تنام** بعد 15 دقيقة بلا طلبات HTTP فينقطع الـWebSocket. للتشغيل 24/7 استخدم خطة مدفوعة (starter) أو VPS (`docker compose up -d`).
- الـDisk (حفظ الـJournal وسجل الاحتمالات بين النشرات) متاح في الخطط المدفوعة فقط. بدونه تُمسح قاعدة البيانات مع كل نشر.
- يوجد نقطة `/health` للفحص (تُفعَّل عند وجود `PORT`).

## 3) ما الذي تم اختباره فعلًا (74 اختبارًا آليًا ناجحًا)

| المجال | ما تم التحقق منه |
|---|---|
| WebSocket | الـsubscribe حسب بروتوكول SiftingIO، Ping دوري، إعادة اتصال تلقائية، رفض الـticks القديمة/المتأخرة/المكررة، خطأ المصادقة |
| الشموع | بناء 1m من الـticks، إغلاق الشمعة، تجاهل tick متأخر، **عدم إرجاع شمعة قيد التكوّن** |
| المؤشرات | سببية (لا تستخدم المستقبل)، RSI/ATR/VWAP وكل الأعمدة المطلوبة |
| الهيكل/السيولة | HH/HL/LH/LL، BOS، CHoCH، swings مؤكدة فقط، Sweep ≠ Breakout، Equal Highs |
| **عدم تسريب المستقبل** | التحليل عند الزمن t **متطابق** سواء وُجدت بيانات بعده أم لا |
| المخاطر | SL منطقي، RR، رفض SL واسع/Spread عالٍ |
| التتبع | TP1 → BE، TP2، SL، **كل حدث يُرسل مرة واحدة فقط** (BUY يخرج على Bid وSELL على Ask) |
| الاحتمالية | لا نسبة بدون عينة كافية، لا استخدام صفقات من المستقبل، Wilson interval |
| Gemini | تحليل الرد، Fail-closed، تمرير دالتك الحالية |
| Telegram | الإرسال للخاص/القناة/الاثنين، `/id`، تنسيق الإشارة العربي، الأوامر، تجاهل غير المشرفين، فشل الإرسال لا يسجّل إشارة |
| Gemini (3.8 Flash + بديل 3.7) | النموذج الافتراضي، استدعاء REST، اكتشاف الإيقاف (404) وتنبيه مرة واحدة، النموذج البديل |
| العربية | ترجمة كل الأسباب والأوامر والرسائل، وتشكيل الحروف العربية في الشارت |
| Backtest | محاكاة الصفقة (SL قبل TP داخل الشمعة، أثر الـSpread، لا استخدام شموع قبل الدخول)، المقاييس |
| ML | يرفض التفعيل بدون بيانات، **ضجيج عشوائي لا يُفعَّل**، فصل زمني Train/Val/Test |

**فحص التسريب في الـBacktest:** على بيانات تجريبية فيها اتجاهات خرجت نتائج جيدة (نسبة ربح ~71%) — وهذا **ليس دليل ربحية** لأنها بيانات مصنوعة. أعدت التشغيل على سير عشوائي بلا أي ميزة فخرجت النتيجة المجمّعة **سالبة** (نسبة ربح 35-37%، توقّع سالب)، وهذا المتوقع إن لم يكن هناك تسريب.

## 4) ما لم يُختبر ولا أستطيع ادّعاءه

- **لم أتصل بخوادم SiftingIO/Telegram/Gemini الحقيقية** (بيئة البناء بلا إنترنت). الـWebSocket والـREST مبنيان على توثيقهم الرسمي ومختبران بخادم وهمي. شغّل `python -m goldbot check` أولًا.
- مكتبة `ta` لم تُثبَّت هنا؛ جُرّبت الصيغ الداخلية المكافئة. إن فشلت `ta` مع نسخة pandas عندك يتحول البوت تلقائيًا للحساب الداخلي ويكتب تحذيرًا.
- `vectorbt` اختياري (`vectorbt_cross_check`) وغير مجرَّب. الـBacktest الأساسي مكتوب داخليًا ليستخدم **نفس كود التداول الحي**.
- **نتائج الـBacktest على بياناتك الحقيقية غير معروفة لي.** لا تثق بالاستراتيجيات قبل تشغيلها ومراجعة `reports/` (حسب الجلسة/الإعداد/النظام). لا شيء هنا يضمن ربحًا.
- TA-Lib وTradingAgents لم تُدمج (كما طلبت): فقط الفكرة (Technical/Market/Risk analysts ← Gemini كـTrader) في `goldbot/ai/agents.py`.

## 5) قيود يجب أن تعرفها

- الذهب الفوري ليس له حجم حقيقي: **Volume = عدد الـticks** (يظهر في VWAP وRelative Volume).
- مواعيد الجلسات ثابتة بتوقيت UTC (لا ضبط تلقائي للتوقيت الصيفي) — عدّلها في `sessions` مرتين في السنة.
- الشمعة اليومية تبدأ 00:00 UTC (بعض الوسطاء 21:00/22:00).
- الخطة المجانية لـSiftingIO REST: 10,000 طلب/شهر. البوت يخزّن التاريخ محليًا ويحمّل فقط الجزء الناقص (~1 طلب لكل 1.4 يوم عند أول تحميل).
- في الـBacktest الـSpread ثابت (0.30) ولا يوجد تقويم أخبار تاريخي.
- `Confidence` في رسالة Telegram هو **Confluence Score** وليس احتمال ربح. الاحتمال يظهر فقط من عينة تاريخية فعلية مع حجمها (n).

## 6) الملفات

```
config/config.yaml        كل الإعدادات (المزوّدات قابلة للاستبدال من هنا)
goldbot/providers/        siftingio_ws, siftingio_rest, csv_history, synthetic(اختبار فقط)
goldbot/core/             tick_store, candle_builder, timeframes, market_data, history_store
goldbot/analysis/         indicators, structure, liquidity, zones, sessions, levels, news, analyzer(MTF)
goldbot/strategies/       Trend Pullback, Liquidity Sweep, Breakout, Reversal, VWAP
goldbot/engine/           confluence, probability, risk, no_trade, pipeline
goldbot/ai/               gemini_layer (بدون تغيير موديلك), agents (أفكار TradingAgents)
goldbot/notify/           telegram (+أوامر), chart (matplotlib)
goldbot/tracking/         journal (SQLite), tracker (TP1/TP2/SL)
goldbot/backtest/         engine + تقارير (JSON/CSV) ؛ goldbot/ml/ (اختياري)
tests/                    74 اختبارًا:  python -m unittest discover -s tests -t .
```
