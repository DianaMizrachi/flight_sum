-- מאגר התמלול הגולמי (רכיב 2, שלב 2) - סכמת SQLite.
--
-- append-only נאכף במנוע עצמו ולא בקוד הפייתון: הטריגרים למטה מפילים כל
-- UPDATE או DELETE על הטבלה, גם אם מישהו ייגש אליה ישירות. המקבילה
-- ל-Postgres (הרשאות ברמת role + טריגר) נמצאת ב-migrations/.

CREATE TABLE IF NOT EXISTS raw_messages (
    id                 TEXT NOT NULL,
    flight_id          TEXT NOT NULL,
    timestamp          TEXT NOT NULL,  -- ISO-8601 ב-UTC; מיון לקסיקוגרפי = מיון כרונולוגי
    ingested_at        TEXT NOT NULL,
    source             TEXT NOT NULL,
    channel            TEXT,
    text               TEXT NOT NULL,
    language           TEXT NOT NULL,
    dedup_hash         TEXT,
    status             TEXT NOT NULL,
    dead_letter_reason TEXT,
    metadata           TEXT NOT NULL,  -- JSON
    stored_at          TEXT NOT NULL,  -- מתי המאגר כתב את השורה

    -- המזהה ייחודי *בתוך הטיסה*, לא במאגר כולו. הוא מגיע מהמקור, ומקורות
    -- של שתי טיסות שונות יכולים שניהם למנות "m01" - זה לא התנגשות אלא
    -- המצב הרגיל. כשהמפתח היה גלובלי, הטיסה השנייה חזרה מרכיב 1 כ-
    -- duplicate למרות שתוכנה חדש לגמרי (ראו migrations/002).
    PRIMARY KEY (flight_id, id)
);

-- אינדקסים לשלושת מפתחות השליפה מהתכנית. שליפה לפי טיסה לבדה כבר מכוסה
-- בעמודה השמאלית של ה-PRIMARY KEY, ולכן אין כאן אינדקס נפרד עליה;
-- שליפה לפי מזהה *בתוך* טיסה מכוסה במפתח כולו. האינדקס על המזהה לבדו
-- הוא לשליפה על פני כל המאגר (``get`` בלי ``flight_id``), שאחרת הייתה
-- סריקה מלאה - העמודה אינה השמאלית במפתח.
CREATE INDEX IF NOT EXISTS raw_messages_id_idx ON raw_messages (id);
CREATE INDEX IF NOT EXISTS raw_messages_flight_time_idx ON raw_messages (flight_id, timestamp);
CREATE INDEX IF NOT EXISTS raw_messages_flight_hash_idx ON raw_messages (flight_id, dedup_hash);

CREATE TRIGGER IF NOT EXISTS raw_messages_block_update
BEFORE UPDATE ON raw_messages
BEGIN
    SELECT RAISE(ABORT, 'raw_messages is append-only: UPDATE is blocked');
END;

CREATE TRIGGER IF NOT EXISTS raw_messages_block_delete
BEFORE DELETE ON raw_messages
BEGIN
    SELECT RAISE(ABORT, 'raw_messages is append-only: DELETE is blocked');
END;

-- מפתחות זיהוי הכפילויות של רכיב 1, כדי שהכפילות תזוהה גם אחרי אתחול
-- השירות באמצע טיסה. append-only מאותה סיבה.
CREATE TABLE IF NOT EXISTS dedup_keys (
    key           TEXT PRIMARY KEY,
    message_id    TEXT NOT NULL,
    remembered_at TEXT NOT NULL
);

CREATE TRIGGER IF NOT EXISTS dedup_keys_block_update
BEFORE UPDATE ON dedup_keys
BEGIN
    SELECT RAISE(ABORT, 'dedup_keys is append-only: UPDATE is blocked');
END;

CREATE TRIGGER IF NOT EXISTS dedup_keys_block_delete
BEFORE DELETE ON dedup_keys
BEGIN
    SELECT RAISE(ABORT, 'dedup_keys is append-only: DELETE is blocked');
END;
