#!/usr/bin/env python3
"""Write every tagged fixture as an uploadable transcript under samples/.

The fixtures under ``tests/fixtures/`` hold the project's tagged
sentences, but they are YAML and the client takes a ``.txt`` in the
pipe-delimited format - so until now the only file a user could actually
load in the dashboard was the hand-written ``samples/flight_sim_01.txt``.
This script closes that gap: one ``.txt`` per fixture, each carrying its
own ``# flight_id:`` directive, so loading it needs nothing typed.

The files are committed, and ``tests/test_fixture_samples.py`` fails when
they drift from the fixtures - the same arrangement as the exported
OpenAPI spec under ``docs/``.

    python scripts/export_fixture_transcripts.py
    python scripts/export_fixture_transcripts.py --check   # CI / drift

Each file's header states what uploading it should produce, counted by
running the sentences through component 1 here, so the header cannot
claim an outcome the service does not deliver.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aura.ingestion import (  # noqa: E402
    IngestionService,
    parse_transcript_text,
    render_transcript_text,
)
from aura.ingestion.fixture_transcripts import (  # noqa: E402
    FixtureTranscript,
    load_fixture_transcripts,
)
from aura.raw_store import RawStoreDedupIndex, SQLiteRawTranscriptStore  # noqa: E402
from aura.schemas import MessageStatus  # noqa: E402

FIXTURES_DIR = ROOT / "tests" / "fixtures"
OUTPUT_DIR = ROOT / "samples" / "fixtures"

FAMILY_TITLES = {
    "golden_flights": "טיסת זהב מתוייגת",
    "model_eval": "תרחיש להשוואת מודלים",
    "edge_cases": "מקרה גבול",
}


def body(transcript: FixtureTranscript) -> str:
    """שורות ההודעה בלבד, עם הנחיית מזהה הטיסה ובלי הכותרת.

    הכותרת מצהירה על תוצאת ההעלאה, והתוצאה נמדדת מהגוף הזה - לכן הוא
    נבנה קודם. הערות אינן הודעות, ולכן הכותרת אינה משנה את הספירה.
    """
    return render_transcript_text(transcript.messages, flight_id=transcript.flight_id)


def outcome(transcript: FixtureTranscript) -> tuple[int, int, int]:
    """מה שהעלאת *הקובץ* אמורה להחזיר, לפי רכיב 1 עצמו.

    הספירה נמדדת דרך הקורא ולא מהפיקסצ'ר ישירות, כי פורמט הקובץ נותן
    לכל שורה את מזהה הטיסה של הקובץ - ולכן שורה שבפיקסצ'ר נשלחה בלי
    מזהה טיסה *נקלטת* בהעלאה. כותרת שהייתה מצהירה על התוצאה הישירה
    הייתה מבטיחה לקורא דבר אחד ומייצרת אחר.
    """
    parsed = parse_transcript_text(body(transcript), source_name=f"{transcript.name}.txt")
    with SQLiteRawTranscriptStore(":memory:") as store:
        service = IngestionService(store=store, dedup_index=RawStoreDedupIndex(store))
        statuses = [result.status for result in service.ingest_many(parsed.messages)]
    return (
        statuses.count(MessageStatus.ACCEPTED),
        statuses.count(MessageStatus.DUPLICATE),
        statuses.count(MessageStatus.DEAD_LETTER),
    )


def header_lines(transcript: FixtureTranscript) -> list[str]:
    family = transcript.path.parent.name
    accepted, duplicates, dead = outcome(transcript)
    lines = [
        f"aura - {FAMILY_TITLES.get(family, family)}: {transcript.name}",
        "",
        "נוצר אוטומטית מ-"
        f"tests/fixtures/{family}/{transcript.path.name}"
        " על ידי scripts/export_fixture_transcripts.py.",
        "אין לערוך את הקובץ ישירות - ערכו את הפיקסצ'ר והריצו את הסקריפט מחדש.",
        "",
    ]
    if transcript.title:
        lines += [transcript.title.strip().replace("\n", " "), ""]
    lines += [
        "פורמט: message_id | timestamp | source | channel | text",
        f"{len(transcript.messages)} שורות הודעה.",
    ]
    if transcript.rejections:
        lines.append("")
        lines.append("שורות שהפיקסצ'ר מצהיר עליהן כדחויות:")
        for message_id, declared in transcript.rejections.items():
            field = declared.get("missing_field")
            detail = f" ({field})" if field else ""
            lines.append(f"  {message_id} -> {declared['reason']}{detail}")
        if accepted:
            lines += [
                "",
                "שימו לב: מזהה הטיסה הוא שדה של הקובץ כולו, ולכן שורה שנשלחה",
                "בלי מזהה טיסה מקבלת אותו מההנחיה למטה - ונקלטת. זו הסיבה",
                "שהספירה כאן אינה זהה לדחיות שמעליה.",
            ]
    lines += [
        "",
        "תוצאה צפויה בהעלאה דרך הקליינט:"
        f" {accepted} נקלטות, {duplicates} כפילויות, {dead} ל-dead-letter.",
        "מזהה הטיסה למטה הוא הנחיה שהקורא מפרש, ולכן אין צורך להקליד אותו.",
    ]
    return lines


def render(transcript: FixtureTranscript) -> str:
    return render_transcript_text(
        transcript.messages,
        flight_id=transcript.flight_id,
        header_lines=header_lines(transcript),
    )


def export(check: bool = False) -> int:
    transcripts = load_fixture_transcripts(FIXTURES_DIR)
    if not transcripts:
        print(f"no fixtures found under {FIXTURES_DIR}", file=sys.stderr)
        return 1

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    expected: dict[Path, str] = {
        OUTPUT_DIR / f"{transcript.name}.txt": render(transcript) for transcript in transcripts
    }

    stale = sorted(path for path in OUTPUT_DIR.glob("*.txt") if path not in expected)
    drifted = [path for path, text in expected.items() if _read(path) != text]

    if check:
        for path in drifted:
            print(f"out of date: {path.relative_to(ROOT)}", file=sys.stderr)
        for path in stale:
            print(f"no longer has a fixture: {path.relative_to(ROOT)}", file=sys.stderr)
        if drifted or stale:
            print("run: python scripts/export_fixture_transcripts.py", file=sys.stderr)
            return 1
        print(f"{len(expected)} sample transcripts are up to date")
        return 0

    for path, text in expected.items():
        path.write_text(text, encoding="utf-8")
    for path in stale:
        path.unlink()
        print(f"removed {path.relative_to(ROOT)} (its fixture is gone)")
    for path in sorted(expected):
        print(f"wrote {path.relative_to(ROOT)}")
    return 0


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail instead of writing, if a sample has drifted from its fixture",
    )
    return export(check=parser.parse_args().check)


if __name__ == "__main__":
    raise SystemExit(main())
