# aura — מערכת תמלול, זיהוי אירועים וסיכום טיסה

מימוש לפי [flight-transcript-work-plan.md](./flight-transcript-work-plan.md).
התמלולים בעברית; ה-LLM קורא ומזהה אירועים בעברית, והסיכומים (רכיב 7/8)
מופקים בעברית.

**מצב נוכחי: שלבים 0-2 הושלמו** - תשתית, חוזים ונתוני בדיקה (שלב 0),
רכיב 1 (קליטת משפטים) עם נרמול, זיהוי כפילויות, dead-letter ותור פנימי
(שלב 1), ורכיב 2 (מאגר התמלול הגולמי) - append-only עם שליפה לפי מזהה,
טיסה וטווח זמן (שלב 2). שלבים 3-12 עדיין לא ממומשים - רק המבנה
(`src/aura/<component>/`) קיים כשלד ריק לכל אחד מהם, לפי מבנה הרכיבים
בתכנית.

## הרצה מקומית

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pytest
ruff check src tests
```

> הערה לסביבה הזו בלבד: יש משתנה סביבה `PYTHONPATH` גלובלי שמצביע על
> `/usr/lib/python3/dist-packages` ומתנגש עם venv (שובר ייבוא `pydantic`).
> יש להריץ `unset PYTHONPATH` לפני הפעלת ה-venv. זו תכונה של המחשב הזה,
> לא של הפרויקט.

תשתית מקומית (מסד נתונים, תור ומודל LLM מקומי): `docker-compose.yml`.
ה-LLM רץ מקומית על חומרת המשתמש (RTX A4500, 16GB VRAM) דרך Ollama -
ראו הערה ב-docker-compose.yml לבחירת מודל ראשוני ולהחלפתו.

## מבנה הריפו

```
src/aura/
  schemas/          # שלב 0 — שמונה החוזים: Message, ContextBundle, LLMResult,
                     # ValidatedResult, MissionState, Event, StateChange, Summary
  taxonomy/          # שלב 0 — טקסונומיית סוגי האירועים (event_types.yaml + loader)
  config.py          # שלב 0 — ספי חלונות, תקציב טוקנים (ראו "ל-אישור" למטה)
  ingestion/         # שלב 1 — רכיב 1: נקודת קליטה, נרמול, dedup, dead-letter, תור
  raw_store/         # שלב 2 — רכיב 2: מאגר append-only (SQLite + מיגרציית Postgres)
  context_builder/   # שלב 3 — טרם ממומש
  llm_engine/        # שלב 4 — טרם ממומש
  validator/         # שלב 5 — טרם ממומש
  state_manager/     # שלב 6 — טרם ממומש
  event_store/       # שלב 7 — טרם ממומש
  summarizer/        # שלב 8 — טרם ממומש
  query_service/      # שלב 9 — טרם ממומש
  api/               # ממשק HTTP + OpenAPI; כרגע חושף רק את הקליטה (שלב 1).
                     # שאילתות התחקור נוספות בשלב 10
  reconciler/        # שלב 11 — טרם ממומש

tests/
  fixtures/golden_flights/   # 3 טיסות זהב מתויגות בעברית (ISR/UAV)
  fixtures/edge_cases/       # כפילויות, איחור, משפטים משובשים, סתירה בין אירועים
  test_ingestion.py          # שלב 1 — הרכיב מול סט הזהב ותרחישי הקצה
  test_raw_store.py          # שלב 2 — המאגר הגולמי: שליפה, סדר, append-only
  test_api.py                # ממשק ה-HTTP וה-OpenAPI שמתפרסם
docs/openapi.{json,yaml}     # הספק המיוצא (נבדק מול האפליקציה ב-test_api.py)
scripts/export_openapi.py    # ייצוא מחדש של הספק
  test_*.py                  # ולידציה של כל הפיקסצ'רים מול הסכמות
```

## רכיב 1 — קליטת משפטים (שלב 1)

```python
from aura.ingestion import IngestionService

service = IngestionService()
result = service.ingest({
    "id": "flight-01-m01",         # אם חסר - נגזר מה-hash
    "flight_id": "flight-01",
    "timestamp": "2026-09-01T06:00:00Z",
    "source": "operator",
    "channel": "command",
    "text": "בקרה, נץ אחת, הכל תקין.",
})
result.status        # accepted / duplicate / dead_letter
result.message       # Message מנורמל (קיים גם לכפילות, מסומן כ-duplicate)
service.queue.drain()    # ההודעות שהועברו לעיבוד אסינכרוני
service.dead_letters     # הודעות פגומות, עם סיבה והמקור הגולמי
```

| כלל | התנהגות |
| --- | --- |
| נרמול | NFC, הסרת תווי כיווניות/אפס-רוחב, כיווץ רווחים, זמן ל-UTC |
| שמירת המקור | טקסט שעבר נרמול נשמר במקביל ב-`metadata["raw_text"]` |
| כפילות | לפי `id` מהמקור; בהיעדרו לפי hash של טיסה+מקור+זמן+טקסט |
| הודעה פגומה | dead-letter עם `reason` (`missing_required_field`, `empty_text`, `invalid_timestamp`, `schema_invalid`) |
| הודעה באיחור | `timestamp` המקורי נשמר כפי שהוא; `ingested_at` מתעד את זמן הקליטה |

שני דברים נדחו במכוון לשלב שבו יהיה להם צורכן: התור (`MessageQueue`)
והאינדקס (`DedupIndex`) מוגדרים כ-Protocol עם מימוש בזיכרון בלבד - מימוש
מעל Redis ומעל מאגר התמלול הגולמי נכנס בשלבים 2-3, כשיהיה מי שקורא מהתור
ומי שמזהה כפילות אחרי אתחול באמצע טיסה.

## רכיב 2 — מאגר התמלול הגולמי (שלב 2)

```python
from aura.ingestion import IngestionService
from aura.raw_store import RawStoreDedupIndex, SQLiteRawTranscriptStore

store = SQLiteRawTranscriptStore("var/aura-raw.db")        # ":memory:" לבדיקות
service = IngestionService(store=store, dedup_index=RawStoreDedupIndex(store))
service.ingest({...})                      # נכתב למאגר *לפני* שהועבר לתור

store.get("flight-01-m04")                 # שליפה לפי מזהה
store.by_flight("flight-01")               # כל הטיסה, בסדר כרונולוגי
store.by_time_range("flight-01", start, end)   # טווח זמן בתוך טיסה (כולל גבולות)
store.flights(); store.count("flight-01")
```

| כלל | התנהגות |
| --- | --- |
| append-only | אין `update` ואין `delete` ב-API, **והמנוע עצמו חוסם אותם** |
| סדר כרונולוגי | מיון לפי ה-`timestamp` המקורי, כך שהודעה באיחור חוזרת למקומה |
| טקסט | נשמר ונשלף זהה בייט-בייט; הנוסח שלפני הנרמול נשאר ב-`metadata["raw_text"]` |
| כפילות | כתיבת מזהה קיים מעלה `DuplicateMessageError`; `append_many` היא הכול-או-כלום |
| הודעה פגומה | אינה נכתבת למאגר - היא אינה `Message` תקין; המקור הגולמי נשמר ברשומת ה-dead-letter |

**חסימת עדכון ומחיקה "לא רק בקוד".** ב-SQLite האכיפה היא בטריגרים
(`schema.sql`), כך שגם SQL ישר נדחה:

```python
store.execute_sql("UPDATE raw_messages SET text = 'שונה' WHERE id = 'flight-01-m04'")
# AppendOnlyViolationError: raw_messages is append-only: UPDATE is blocked
```

ב-Postgres (היעד ב-docker-compose) האכיפה היא בהרשאות + טריגר:

```bash
docker compose exec -T postgres psql -U aura -d aura \
  < src/aura/raw_store/migrations/001_raw_messages.postgres.sql
```

הטבלה נוצרת בבעלות role של מיגרציות, והשירות מתחבר ב-role שמקבל
`SELECT, INSERT` בלבד - ב-Postgres בעל טבלה שומר תמיד את הרשאותיו, ולכן
`REVOKE` ממנו אינו מספק. המיגרציה עצמה אינה מורצת ב-CI (אין שם מסד); מה
שנבדק אוטומטית הוא מימוש ה-SQLite, שבו רצות כל בדיקות השלב.

**האינדקס הנמשך לזיהוי כפילויות.** `RawStoreDedupIndex` מחליף את האינדקס
בזיכרון של שלב 1, ולכן שירות שעלה מחדש באמצע טיסה עוד מזהה הודעה שכבר
נקלטה (נבדק ב-`test_raw_store.py`, וגם מול שרת חי).

מה שעוד לא: הכתיבה למאגר נעשית סינכרונית בתוך הקליטה. הקריאה מהתור
(עיבוד אסינכרוני בפועל) נכנסת בשלב 3 עם בניית ההקשר.

## ממשק HTTP ו-Swagger

```bash
pip install -e ".[dev,api]"
uvicorn aura.api:app --reload
```

| כתובת | מה יש שם |
| --- | --- |
| `/docs` | Swagger UI, עם דוגמאות הודעה בעברית |
| `/redoc` | ReDoc |
| `/openapi.json` | הספק החי |
| `docs/openapi.yaml` · `docs/openapi.json` | הספק המיוצא, לקריאה ולסקירה בלי להריץ את השירות |

נקודות הקצה הקיימות: הקליטה (רכיב 1) ושליפה מהמאגר הגולמי (רכיב 2).

| שיטה | נתיב | תיאור |
| --- | --- | --- |
| `POST` | `/messages` | קליטת משפט בודד - 201 נקלט / 200 כפילות / 422 dead-letter |
| `POST` | `/messages/batch` | קליטת רצף משפטים; תמיד 200, הסטטוס בכל תוצאה בנפרד |
| `GET` | `/flights` | הטיסות שיש להן תמלול שמור |
| `GET` | `/flights/{flight_id}/messages` | תמלול טיסה בסדר כרונולוגי, עם `start`/`end`/`limit` |
| `GET` | `/messages/{message_id}` | משפט בודד לפי מזהה - מאירוע חזרה לתמלול המקורי |
| `GET` | `/dead-letters` | ההודעות שנדחו, עם סיבה ומקור גולמי (סינון לפי טיסה) |
| `GET` | `/queue` | עומק התור הפנימי |
| `GET` | `/health` | בריאות השירות והשלבים שממומשים בבנייה |

שאילתות התחקור - ציר זמן, מצב משימה, סיכום טיסה, מאירוע לתמלול המקורי
ובחירה בין גרסת זמן אמת לגרסה סופית - נוספות כאן בשלב 10, מעל שירות
השאילתות של שלב 9.

שתי החלטות תכן בשכבה הזו:

- **הולידציה התוכנית נשארת ברכיב הקליטה.** כל שדה במודל הבקשה אופציונלי
  ברמת ה-HTTP, כדי שהודעה חסרת `text` או `flight_id` תגיע ל-dead-letter עם
  סיבה ולא תידחה בשגיאת תחבורה שלא נרשמת בשום מקום. מה נדרש בפועל מתועד
  בתיאור השדות ב-Swagger.
- **הספק מיוצא ונשמר בריפו.** `python scripts/export_openapi.py` מייצר מחדש
  את `docs/openapi.{json,yaml}`, ו-`test_api.py` נכשל כשהם מתיישנים מול
  האפליקציה - באותה רוח של שלב 0, שבו CI נכשל כששינוי סכמה לא עודכן
  בדוגמאות.

## החלטות ברירת מחדל לשלב 0 (ראו "שאלות לבירור" בתכנית)

התכנית מפרטת שאלות שצריך לברר לפני שלב 0. חלקן אושרו בתחילת העבודה,
השאר הם ברירות מחדל סבירות לשלד הראשוני, מסומנות כ-`PLACEHOLDER` או
`PLAN DEFAULT` ב-`src/aura/config.py`, וטעונות אישור לפני הרצה על נתונים
אמיתיים:

| שאלה | החלטה |
| --- | --- |
| דומיין / טרמינולוגיה | סיור UAV/ISR צבאי (מטרות, אזורים, כוחות) |
| מודל LLM | מקומי בלבד, על חומרת המשתמש (ללא API חיצוני) |
| שפת התמלולים | עברית - גם זיהוי האירועים וגם התקצירים |
| סף שתיקה לסגירת חלון | **PLACEHOLDER 20 שניות** - לא צוין בתכנית, לאישור |
| אורך מרבי לחלון / מספר משפטים מרבי | 90 שניות / 15 משפטים (ברירת המחדל מהתכנית) |
| חפיפה בין חלונות | 2 משפטים (מהתכנית) |
| זיהוי סיום טיסה (חוסר הודעות) | 15 דקות (ברירת המחדל מהתכנית, "לאישור") |
| תקציב טוקנים לחלון | **PLACEHOLDER 4000** - לא צוין בתכנית, לאישור |
| מי מתייג את סט הזהב / מאשר טקסונומיה | טרם נקבע - שאלה פתוחה |
| נפח הודעות צפוי לטיסה / אורך טיסה טיפוסי | טרם נקבע - שאלה פתוחה |

## מודל LLM מקומי

החומרה הזמינה: NVIDIA RTX A4500 Laptop (16GB VRAM), 62GB RAM, 24 cores -
מספיקה למודלים מקומיים באיכות סבירה בכימות 4-bit (עד כ-14B פרמטרים
בנוחות, או מודלים גדולים יותר עם offload חלקי). שכבת הלקוח של רכיב 4
("שכבת לקוח מופשטת למודל") היא בדיוק מה שמאפשר להחליף בין מודלים בלי
לשנות את שאר הצינור - הבחירה הראשונית (Ollama + Qwen2.5-14B-Instruct,
ראו docker-compose.yml) היא התחלה, לא מחויבות. מודל ממוקד-עברית כמו
DictaLM 2.0 (מבוסס Mistral-7B, מפותח ע"י דיקטה) שווה הערכה מול קו הבסיס
בסקריפט ההערכה של שלב 4, במיוחד אם איכות ה-JSON המובנה תספיק.
