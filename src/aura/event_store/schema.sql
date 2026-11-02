-- מאגר האירועים ומצב המשימה (רכיב 7, שלב 7) - סכמת SQLite.
--
-- שתי הטבלאות הראשונות הן append-only ונאכפות במנוע, כמו המאגר הגולמי:
-- אירוע ושינוי מצב הם עובדות שקרו, ועובדה אינה משתנה בדיעבד. הגרסה
-- הסופית של שלב 11 *מוסיפה* שורות עם version='final' ואינה נוגעת
-- בשורות של זמן האמת - וזו הסיבה שאפשר להשוות בין הגרסאות.
--
-- הטבלה השלישית, entity_snapshots, היא מטמון ולא מקור אמת: היא תמונת
-- המצב הנוכחית, והיא תמיד ניתנת לבנייה מחדש מרצף state_changes. לכן
-- דווקא *היא* מותרת בעדכון, ומחיקתה אינה מאבדת דבר.

CREATE TABLE IF NOT EXISTS events (
    id                    TEXT NOT NULL,
    flight_id             TEXT NOT NULL,
    window_id             TEXT NOT NULL,
    type                  TEXT NOT NULL,
    description           TEXT NOT NULL,
    entities              TEXT NOT NULL,  -- JSON: רשימת EntityRef
    new_info              TEXT NOT NULL,
    relation_to_previous  TEXT NOT NULL,
    related_event_id      TEXT,
    confidence            REAL NOT NULL,
    version               TEXT NOT NULL,
    created_at            TEXT NOT NULL,  -- ISO-8601 UTC; מיון לקסיקוגרפי = כרונולוגי
    reconciles_event_id   TEXT,
    reconciliation_action TEXT,
    reconciliation_reason TEXT,
    stored_at             TEXT NOT NULL,

    -- מזהה האירוע ייחודי בתוך הטיסה *ובתוך הגרסה*: שלב 11 מפיק אירוע
    -- סופי שעשוי לשאת את אותו מזהה כמו זה של זמן האמת, והם שתי שורות.
    PRIMARY KEY (flight_id, version, id)
);

CREATE INDEX IF NOT EXISTS events_flight_time_idx ON events (flight_id, version, created_at);
CREATE INDEX IF NOT EXISTS events_type_idx ON events (flight_id, type);

-- הקישור מאירוע למשפטי המקור שלו. טבלה נפרדת ולא JSON בתוך השורה, כי
-- זו שאלה שנשאלת משני הכיוונים: "מאיזה משפט בא האירוע" ולא פחות -
-- "אילו אירועים נגזרו מהמשפט הזה", וזו האחרונה דורשת אינדקס.
CREATE TABLE IF NOT EXISTS event_sources (
    flight_id  TEXT NOT NULL,
    version    TEXT NOT NULL,
    event_id   TEXT NOT NULL,
    message_id TEXT NOT NULL,
    position   INTEGER NOT NULL,  -- סדר המשפטים כפי שהאירוע מנה אותם

    PRIMARY KEY (flight_id, version, event_id, message_id)
);

CREATE INDEX IF NOT EXISTS event_sources_message_idx
    ON event_sources (flight_id, message_id);

CREATE TABLE IF NOT EXISTS state_changes (
    id           TEXT NOT NULL,
    flight_id    TEXT NOT NULL,
    event_id     TEXT NOT NULL,
    entity_id    TEXT NOT NULL,
    entity_type  TEXT NOT NULL,
    attribute    TEXT,
    kind         TEXT NOT NULL,
    old_value    TEXT,
    new_value    TEXT,
    certainty    TEXT NOT NULL,
    version      TEXT NOT NULL,
    applied_at   TEXT NOT NULL,
    -- סדר הכתיבה בתוך אותה חותמת זמן. בלעדיו, שני שינויים על אותו
    -- מאפיין באותה שנייה היו ניתנים לשחזור בשני סדרים שונים, ואחד מהם
    -- נותן מצב שגוי.
    sequence     INTEGER NOT NULL,
    stored_at    TEXT NOT NULL,

    PRIMARY KEY (flight_id, version, id)
);

CREATE INDEX IF NOT EXISTS state_changes_replay_idx
    ON state_changes (flight_id, version, applied_at, sequence);
CREATE INDEX IF NOT EXISTS state_changes_entity_idx
    ON state_changes (flight_id, version, entity_id);

CREATE TRIGGER IF NOT EXISTS events_block_update
BEFORE UPDATE ON events
BEGIN
    SELECT RAISE(ABORT, 'events is append-only: UPDATE is blocked');
END;

CREATE TRIGGER IF NOT EXISTS events_block_delete
BEFORE DELETE ON events
BEGIN
    SELECT RAISE(ABORT, 'events is append-only: DELETE is blocked');
END;

CREATE TRIGGER IF NOT EXISTS state_changes_block_update
BEFORE UPDATE ON state_changes
BEGIN
    SELECT RAISE(ABORT, 'state_changes is append-only: UPDATE is blocked');
END;

CREATE TRIGGER IF NOT EXISTS state_changes_block_delete
BEFORE DELETE ON state_changes
BEGIN
    SELECT RAISE(ABORT, 'state_changes is append-only: DELETE is blocked');
END;

-- תמונת המצב הנוכחית. מטמון: ניתן למחוק ולבנות מחדש מ-state_changes.
CREATE TABLE IF NOT EXISTS entity_snapshots (
    flight_id  TEXT NOT NULL,
    version    TEXT NOT NULL,
    entity_id  TEXT NOT NULL,
    payload    TEXT NOT NULL,  -- JSON: Entity מלא, כולל מאפיינים והיסטוריה
    as_of      TEXT NOT NULL,
    updated_at TEXT NOT NULL,

    PRIMARY KEY (flight_id, version, entity_id)
);
