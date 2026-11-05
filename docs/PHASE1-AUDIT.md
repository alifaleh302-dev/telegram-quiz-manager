# تقرير المرحلة الأولى — فحص المشروع القائم
## Telegram Quiz Manager → منصة اختبارات تفاعلية (Student Portal + Mini App)

> هذا التقرير هو المخرج الإلزامي للمرحلة الأولى في وثيقة المواصفات v2.0.
> لا يُعدَّل أي كود قبل اعتماده. الغرض: وصف البنية الفعلية، وتحديد الفروق عن
> افتراضات الوثيقة، واقتراح **أقل تعديل ممكن** يحقّق المتطلبات دون كسر القائم.

---

## 1. ملخص تنفيذي

المشروع القائم **ليس** سكربت JSON كما تفترض الوثيقة. هو تطبيق FastAPI متكامل
بخلفية خدمات منفصلة (Services) وتخزين **SQLite بطبقة مجموعات**. جميع الوظائف
المذكورة في البند 18 (بنك الأسئلة، القنوات، الجدولة، النشر، الإحصائيات، الصلاحيات،
النسخ الاحتياطي) **موجودة وتعمل**، واختباراتها **310 اختباراً ناجحاً**.

المكوّن الجديد (Exams + Students + Attempts + Mini App) **غير موجود إطلاقاً**.
بنية المشروع الحالية **مهيّأة تماماً** لإضافته كمجموعة خدمات جديدة دون إعادة بناء.

**التوصية:** إضافة وحدات جديدة (خدمات + جداول منطقية + راوترات) بنمط المشروع نفسه،
دون لمس منطق النشر الحالي. هذا يتوافق مع البند 18 الذي يفصل صراحة بين
**Question Publishing** (قائم) و**Exam Publishing** (جديد).

---

## 2. البنية الفعلية للمشروع

### 2.1 التقنيات المستخدمة فعلاً

| الطبقة | التقنية الفعلية | ملاحظة مقابل الوثيقة |
| --- | --- | --- |
| Backend | Python 3.13 / **FastAPI** | مطابق |
| Frontend (Admin) | Jinja2 + Bootstrap 5 RTL + **HTMX** + Chart.js | مطابق |
| Scheduler | **APScheduler** | مطابق |
| Storage | **SQLite** (ملف واحد `data/tqm.db`) عبر طبقة مجموعات | **الوثيقة تفترض JSON — غير مطابق** |
| Telegram | python-telegram-bot (polling/webhook) + userbot (Kurigram) | مطابق وأكثر |
| Server | Uvicorn | مطابق |
| Auth | جلسات موقّعة (itsdangerous) + Argon2 + CSRF | مطابق |

### 2.2 طبقة التخزين (أهم فرق عن الوثيقة)

ليست ملفات JSON. البنية:

- ملف SQLite واحد: `data/tqm.db` (WAL mode، معاملات `BEGIN IMMEDIATE`، قفل واحد).
- جدول `documents` للقيم القياسية لكل مجموعة.
- جدول صفوف لكل مجموعة قائمة (`questions`, `channels`, `schedules`, `history`,
  `audit_log`, `poll_results`, `poll_answers`, `userbot_accounts`).
- `_schema()` يستخدم `CREATE TABLE IF NOT EXISTS` وتُنفَّذ عند كل فتح اتصال،
  لذلك **إضافة مجموعة جديدة لا تحتاج Migration تقليدي** — يكفي تعريفها في
  `Storage.__init__` وإضافتها إلى `ROWS_TABLES`/`ROWS_KEYS`/`all_stores()`.
- ملفات `data/*.json` ما زالت موجودة كـ**مصدر ترحيل تاريخي** فقط
  (`migrate_from_json`)، وتُقرأ فقط إن كانت المجموعة فارغة.

**الأثر على الخطة:** "Phase 2 — تصميم قاعدة البيانات / Migrations" تتحوّل إلى
تعريف مجموعات جديدة بأمان (Additive)، مع الحفاظ على الترحيل من JSON للتوافق.

### 2.3 الخدمات القائمة (`app/services/`)

`QuestionService`, `ChannelService`, `ScheduleService`, `PublishingService`,
`SettingsService`, `UserService`, `AnalyticsService`, `BackupService`,
`AuditService`, `SecretStore`, `UserbotAccountService`, و`container.build_services`
كنقطة تجميع (Composition Root) تُخزَّن في `app.state.services`.

### 2.4 Telegram الحالي

- `BotManager` (في `app/telegram/bot.py`): مستمع بسيط بأوامر `/start`, `/help`,
  `/status` + معالجة نتائج الاستطلاعات. **لا يوجد Mini App ولا زر قائمة.**
- `TransportRouter`: يوجّه النشر لكل قناة (bot أو userbot).
- التوكن يُخزَّن **مشفَّراً** في مجموعة `secrets` (Fernet)، لا نص مكشوف.

### 2.5 المصادقة والصلاحيات (للمدير فقط حالياً)

- مستخدمو لوحة الإدارة فقط (`admin`/`user`) بجلسات موقّعة + CSRF.
- لا يوجد أي مفهوم "طالب" أو هوية Telegram.
- `GRANTS` في `app/core/permissions.py`: questions, channels, schedules, publish,
  automation, analytics.

---

## 3. الفروق بين الوثيقة والبنية الفعلية (مهم)

| # | ما تفترضه الوثيقة | الواقع | التعديل المقترح (الأقل) |
| --- | --- | --- | --- |
| 1 | تخزين JSON مقسّم لملفات | SQLite بطبقة مجموعات | استخدام مجموعات جديدة، لا JSON |
| 2 | Migrations تقليدية | `CREATE TABLE IF NOT EXISTS` تلقائي | تعريف مجموعات Additive |
| 3 | جداول: users/exams/attempts... | لا يوجد أي منها؛ "users" الحالية = مدراء | **عدم المساس بمجموعة `users`**؛ جداول الطلاب باسم `students` مستقلة |
| 4 | بنك أسئلة منفصل للاختبارات | بنك واحد (`questions`) | إعادة استخدامه حصراً (كما يطلب البند 12.3) |
| 5 | "قاعدة بيانات الطلاب" | لا توجد | مجموعة `students` + `exam_*` + `attempts` |
| 6 | Mini App جديد | غير موجود | صفحات HTML جديدة + التحقق من `initData` |

---

## 4. خطة الدمج المقترحة (خدمات جديدة، بدون لمس القديم)

| الوحدة الجديدة | المسؤولية | تعيد استخدام |
| --- | --- | --- |
| `StudentService` | هوية Telegram، الملف الشخصي، آخر نشاط، الإشعارات | `Storage` |
| `ExamService` | إنشاء/تعديل/نشر/إغلاق/أرشفة الاختبارات + Access Mode | `QuestionService` (اختيار الأسئلة) |
| `ExamAttemptService` | بدء المحاولة، حفظ الإجابات، التسليم، التصحيح، النتائج، المراجعة | `ExamService` |
| `StudentApi` (router) | `/api/student/*` محميّ بـ `initData` | `StudentService` |
| `AdminExamApi` (router) | `/api/admin/exams/*` محميّ بجلسة الإدارة + الصلاحيات | `ExamService` |
| Mini App templates | Home / Library / Exam / Result / My Attempts | هوية Bootstrap RTL |
| Telegram Mini App | زر قائمة + روابط مباشرة + إشعارات | `BotManager` (توسعة) |

**مبدأ الأمان:** التحقق من `initData` على الخادم حصراً (HMAC-SHA256 بمفتاح
مشتق من توكن البوت)، ورفض البيانات القديمة (`auth_date`)، وعدم الثقة بـ
`initDataUnsafe`. جلسة الطالب تُصدر من الخادم بعد التحقق.

**مبدأ سلامة النتائج:** كل محاولة تحفظ **لقطة** من الأسئلة والخيارات والإجابة
الصحيحة (`exam_questions`)، فتعديل بنك الأسئلة لاحقاً لا يغيّر نتائج قديمة.

---

## 5. حالة الاختبارات (خط الأساس)

- `python3 -m pytest` → **310 اختباراً ناجحاً، ~69 ثانية** (بعد تثبيت التبعيات).
- التبعيات لم تكن مثبّتة في بيئة العمل هذه؛ ثُبّتت من `requirements.txt`.

---

## 6. المخاطر والقيود

1. **حدود Telegram:** البوت لا يستطيع مراسلة مستخدم لم يبدأ المحادثة أولاً؛
   الإشعارات الجماعية تحتاج جدولة واحترام `429/retry_after`.
2. **userbot:** إرسال الإشعارات عبر حساب مستخدم قد يخالف سياسة تيليجرام —
   الافتراضي سيكون البوت فقط للإشعارات.
3. **Mini App يتطلب HTTPS** (متوفر على Railway).
4. **التوافق الخلفي:** أي إضافة يجب ألا تغيّر مخطط `questions` الحالي ولا تنسيق
   الاستيراد/التصدير.
5. **الوقت موثوق من الخادم** لا من جهاز الطالب (قاعدة صريحة في البند 6).

---

## 7. نطاق المرحلة التالية المقترح (بعد الاعتماد)

1. مجموعات تخزين جديدة: `students`, `exams`, `exam_questions`, `attempts`,
   `attempt_answers`, `student_exam_state`, `notifications`, `admin_logs`.
2. خدمات: `StudentService`, `ExamService`, `ExamAttemptService`.
3. التحقق من `initData` + جلسة طالب منفصلة.
4. Student API ثم Admin Exam API ثم Mini App ثم تكامل البوت ثم لوحة الإدارة.

كل مرحلة تُختبر (اختبارات آلية + تشغيل فعلي) قبل الانتقال، مع الحفاظ على
الـ310 اختباراً الحالية خضراء.
