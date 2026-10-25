-- מאגר התמלול הגולמי על Postgres (ה-postgres שב-docker-compose.yml).
--
-- הערה על "חסימת עדכון ומחיקה ברמת ההרשאות": ב-Postgres בעל הטבלה שומר
-- תמיד את הרשאותיו, ולכן REVOKE מבעל הטבלה אינו אפקטיבי. הטבלה נוצרת
-- בבעלות role של מיגרציות (aura_migrator), והשירות מתחבר ב-role נפרד
-- (aura) שמקבל SELECT ו-INSERT בלבד. הטריגר הוא שכבת הגנה שנייה, שתופסת
-- גם חיבור שנעשה בטעות ב-role של המיגרציה.
--
-- הרצה:
--   docker compose exec -T postgres psql -U aura -d aura \
--     < src/aura/raw_store/migrations/001_raw_messages.postgres.sql

CREATE TABLE IF NOT EXISTS raw_messages (
    id                 TEXT PRIMARY KEY,
    flight_id          TEXT NOT NULL,
    timestamp          TIMESTAMPTZ NOT NULL,
    ingested_at        TIMESTAMPTZ NOT NULL,
    source             TEXT NOT NULL,
    channel            TEXT,
    text               TEXT NOT NULL,
    language           TEXT NOT NULL DEFAULT 'he',
    dedup_hash         TEXT,
    status             TEXT NOT NULL,
    dead_letter_reason TEXT,
    metadata           JSONB NOT NULL DEFAULT '{}'::jsonb,
    stored_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS raw_messages_flight_idx ON raw_messages (flight_id);
CREATE INDEX IF NOT EXISTS raw_messages_flight_time_idx ON raw_messages (flight_id, timestamp);
CREATE INDEX IF NOT EXISTS raw_messages_flight_hash_idx ON raw_messages (flight_id, dedup_hash);

CREATE TABLE IF NOT EXISTS dedup_keys (
    key           TEXT PRIMARY KEY,
    message_id    TEXT NOT NULL,
    remembered_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE OR REPLACE FUNCTION append_only_guard() RETURNS trigger AS $guard$
BEGIN
    RAISE EXCEPTION '% is append-only: % is blocked', TG_TABLE_NAME, TG_OP;
END;
$guard$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS raw_messages_append_only ON raw_messages;
CREATE TRIGGER raw_messages_append_only
    BEFORE UPDATE OR DELETE ON raw_messages
    FOR EACH ROW EXECUTE FUNCTION append_only_guard();

DROP TRIGGER IF EXISTS dedup_keys_append_only ON dedup_keys;
CREATE TRIGGER dedup_keys_append_only
    BEFORE UPDATE OR DELETE ON dedup_keys
    FOR EACH ROW EXECUTE FUNCTION append_only_guard();

-- ההרשאות עצמן: רק קריאה והוספה ל-role של השירות.
-- (ה-role נוצר ידנית פעם אחת: CREATE ROLE aura_app LOGIN PASSWORD '...';)
DO $grants$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'aura_app') THEN
        REVOKE ALL ON raw_messages, dedup_keys FROM aura_app;
        GRANT SELECT, INSERT ON raw_messages, dedup_keys TO aura_app;
    END IF;
END
$grants$;
