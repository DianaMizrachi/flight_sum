-- מפתח ראשי לפי (flight_id, id) במקום id לבדו (תיקון למיגרציה 001).
--
-- מזהה ההודעה מגיע מהמקור, והוא ייחודי בתוך הטיסה שלו - לא במאגר כולו.
-- שני מקורות של שתי טיסות יכולים שניהם למנות "m01", וכשהמפתח היה גלובלי
-- התוצאה הייתה שגיאה שקטה: רכיב 1 אישר את ההודעה (מפתח הכפילות שלו
-- משויך לטיסה), המאגר דחה אותה על התנגשות מזהה, והקליטה החזירה
-- ``duplicate`` על הודעה חדשה לגמרי. המקביל ב-SQLite נמצא ב-schema.sql
-- ובמיגרציה האוטומטית שב-sqlite_store.py.
--
-- הרצה:
--   docker compose exec -T postgres psql -U aura -d aura \
--     < src/aura/raw_store/migrations/002_flight_scoped_message_id.postgres.sql

-- הטבלה append-only, אך זו DDL ולא עדכון שורה: הטריגר יורה על UPDATE או
-- DELETE של שורה, והשורות עצמן אינן נגעות כאן.
DO $migrate$
DECLARE
    primary_key_name text;
    primary_key_columns text[];
BEGIN
    SELECT conname,
           (SELECT array_agg(attname ORDER BY attnum)
              FROM pg_attribute
             WHERE attrelid = c.conrelid AND attnum = ANY (c.conkey))
      INTO primary_key_name, primary_key_columns
      FROM pg_constraint c
     WHERE c.conrelid = 'raw_messages'::regclass AND c.contype = 'p';

    IF primary_key_columns IS NULL THEN
        RAISE EXCEPTION 'raw_messages has no primary key; run 001 first';
    END IF;

    IF primary_key_columns = ARRAY['id'] THEN
        EXECUTE format('ALTER TABLE raw_messages DROP CONSTRAINT %I', primary_key_name);
        ALTER TABLE raw_messages ADD PRIMARY KEY (flight_id, id);
        RAISE NOTICE 'raw_messages: primary key is now (flight_id, id)';
    ELSE
        RAISE NOTICE 'raw_messages: primary key already %', primary_key_columns;
    END IF;
END
$migrate$;

-- שליפה לפי מזהה על פני כל המאגר אינה מכוסה במפתח, שבו flight_id היא
-- העמודה השמאלית; בלי האינדקס הזה היא סריקה מלאה.
CREATE INDEX IF NOT EXISTS raw_messages_id_idx ON raw_messages (id);

-- שליפה לפי טיסה לבדה מכוסה מעתה בעמודה השמאלית של המפתח.
DROP INDEX IF EXISTS raw_messages_flight_idx;
