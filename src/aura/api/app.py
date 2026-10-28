"""The aura HTTP API and its OpenAPI / Swagger surface.

Scope note: component 1 (ingestion) is the only component built so far, so
this app publishes the write side of the system. The read endpoints from
stage 10 - timeline, mission state, flight summary, event -> source
transcript and realtime/final version selection - arrive together with the
query service in stage 9; their absence here is intentional.

Swagger UI: ``/docs``   ReDoc: ``/redoc``   Raw spec: ``/openapi.json``
"""

from datetime import datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path as PathLib

from fastapi import (
    APIRouter,
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Path,
    Query,
    Response,
    UploadFile,
    status,
)
from fastapi.responses import FileResponse

from aura.api.dependencies import get_ingestion_service, get_raw_store
from aura.api.schemas import (
    BatchIngestRequest,
    BatchIngestResponse,
    ErrorResponse,
    HealthResponse,
    IngestRequest,
    QueueStatus,
    UploadReport,
    UploadResponse,
)
from aura.api.status import SystemStatus, build_status, resolved_store_path
from aura.ingestion import (
    DeadLetterRecord,
    IngestionService,
    IngestResult,
    parse_transcript_text,
)
from aura.ingestion.queue import InMemoryQueue
from aura.raw_store import RawTranscriptStore
from aura.schemas import Message, MessageStatus

try:
    API_VERSION = version("aura")
except PackageNotFoundError:  # pragma: no cover - only when running from a source tree
    API_VERSION = "0.0.0"

# 422 as a literal: starlette renamed the constant mid-1.x, and the project
# pins only fastapi>=0.110, so both spellings may be present.
HTTP_422 = 422

# תקרה להעלאת קובץ. טיסה של 35 דקות היא ~3KB, ולכן 5MB הוא מרווח גדול
# מאוד שבעיקר מונע העלאה של קובץ שאינו תמלול בכלל.
MAX_UPLOAD_BYTES = 5 * 1024 * 1024

# השלבים שנבנו. רכיבים 3-4 בנויים אך אינם חשופים כנקודות קצה - ראו
# aura.api.status, שמפריד בין "נבנה" לבין "חשוף ב-API".
IMPLEMENTED_STAGES = [0, 1, 2, 3, 4]

STATIC_DIR = PathLib(__file__).parent / "static"
DASHBOARD_FILE = STATIC_DIR / "index.html"

# סטטוס הקליטה -> קוד HTTP. dead-letter הוא 422: ההודעה נרשמה עם סיבה,
# אבל המקור צריך לדעת שהיא לא תיכנס לניתוח.
_STATUS_CODES = {
    MessageStatus.ACCEPTED: status.HTTP_201_CREATED,
    MessageStatus.DUPLICATE: status.HTTP_200_OK,
    MessageStatus.DEAD_LETTER: HTTP_422,
}

DESCRIPTION = """
ממשק ה-HTTP של aura - קליטת משפטי תמלול, זיהוי אירועים וסיכום טיסה.
מימוש לפי `flight-transcript-work-plan.md`.

**מה שמומש עד כה:** שלב 0 (חוזים, טקסונומיה, סט זהב), שלב 1 - רכיב 1,
קליטת משפטים: נרמול, זיהוי כפילויות, dead-letter ותור פנימי, ושלב 2 -
רכיב 2, מאגר התמלול הגולמי: append-only, ושליפה לפי מזהה, טיסה וטווח זמן.

**מה שעוד לא:** שאילתות התחקור (ציר זמן, מצב משימה, סיכום טיסה, מאירוע
לתמלול המקורי, ובחירה בין גרסת זמן אמת לגרסה סופית) נוספות כאן בשלב 10,
מעל שירות השאילתות של שלב 9.

### התנהגות הקליטה

| תוצאה | קוד | מתי |
| --- | --- | --- |
| `accepted` | 201 | ההודעה נקלטה, נורמלה והועברה לתור |
| `duplicate` | 200 | אותה הודעה נקלטה כבר - מסומנת, לא נכנסת לתור שוב |
| `dead_letter` | 422 | הודעה פגומה; נרשמה עם סיבה וניתן לשלוף אותה ב-`/dead-letters` |

הולידציה התוכנית נעשית ברכיב הקליטה ולא בשכבת ה-HTTP, כדי שכל הודעה
פגומה תגיע ל-dead-letter עם סיבה ולא תיעלם בשגיאת תחבורה.
"""

TAGS_METADATA = [
    {"name": "ingestion", "description": "רכיב 1 - קליטת משפטי תמלול (שלב 1)."},
    {
        "name": "transcript",
        "description": "רכיב 2 - שליפה מהמאגר הגולמי (שלב 2). append-only: אין עדכון ואין מחיקה.",
    },
    {"name": "operations", "description": "בריאות השירות, התור הפנימי ולוג ה-dead-letter."},
]

ingestion_router = APIRouter(tags=["ingestion"])
transcript_router = APIRouter(tags=["transcript"])
operations_router = APIRouter(tags=["operations"])
app_router = APIRouter()


@ingestion_router.post(
    "/messages",
    response_model=IngestResult,
    status_code=status.HTTP_201_CREATED,
    summary="קליטת משפט תמלול בודד",
    response_description="ההודעה נקלטה, נורמלה והועברה לתור הפנימי",
    responses={
        status.HTTP_200_OK: {
            "model": IngestResult,
            "description": "כפילות - ההודעה כבר נקלטה ומסומנת; לא הועברה לתור שוב",
        },
        HTTP_422: {
            "model": IngestResult,
            "description": "ההודעה נרשמה ל-dead-letter עם סיבה; ראו שדה dead_letter",
        },
    },
)
def ingest_message(
    payload: IngestRequest,
    response: Response,
    service: IngestionService = Depends(get_ingestion_service),
) -> IngestResult:
    """קליטת משפט אחד: נרמול (UTC, רווחים, תווי כיווניות), זיהוי כפילות
    לפי מזהה המקור או לפי hash, והעברה לתור הפנימי."""
    result = service.ingest(payload.model_dump(exclude_none=True))
    response.status_code = _STATUS_CODES[result.status]
    return result


@ingestion_router.post(
    "/messages/batch",
    response_model=BatchIngestResponse,
    status_code=status.HTTP_200_OK,
    summary="קליטת רצף משפטים",
    response_description="סיכום לפי סטטוס ותוצאה מלאה לכל הודעה",
    responses={HTTP_422: {"model": ErrorResponse}},
)
def ingest_batch(
    payload: BatchIngestRequest,
    service: IngestionService = Depends(get_ingestion_service),
) -> BatchIngestResponse:
    """קליטת כמה הודעות בקריאה אחת, בסדר שבו נשלחו. תוצאות מעורבות הן
    מצב תקין: הקוד תמיד 200, והסטטוס נמצא בכל תוצאה בנפרד."""
    results = service.ingest_many(item.model_dump(exclude_none=True) for item in payload.messages)
    counts = {item: 0 for item in MessageStatus}
    for result in results:
        counts[result.status] += 1
    return BatchIngestResponse(
        accepted_count=counts[MessageStatus.ACCEPTED],
        duplicate_count=counts[MessageStatus.DUPLICATE],
        dead_letter_count=counts[MessageStatus.DEAD_LETTER],
        results=results,
    )


@ingestion_router.post(
    "/messages/upload",
    response_model=UploadResponse,
    status_code=status.HTTP_200_OK,
    summary="העלאת קובץ תמלול מהמחשב",
    response_description="דוח על פענוח הקובץ ותוצאת קליטה לכל שורה",
    responses={
        status.HTTP_400_BAD_REQUEST: {
            "model": ErrorResponse,
            "description": "אין מזהה טיסה, הקובץ ריק, אינו UTF-8, או גדול מהתקרה",
        }
    },
)
async def upload_transcript(
    file: UploadFile = File(
        description="קובץ טקסט בפורמט message_id | timestamp | source | channel | text"
    ),
    flight_id: str | None = Form(
        default=None,
        description="מזהה הטיסה. גובר על הנחיית `# flight_id:` שבקובץ; נדרש אם אין הנחיה",
    ),
    service: IngestionService = Depends(get_ingestion_service),
) -> UploadResponse:
    """קליטת טיסה שלמה מקובץ אחד.

    הקובץ מפוענח לשורות ואז נקלט שורה-שורה דרך אותו רכיב 1 שמשרת את
    ``POST /messages`` - אותו נרמול, אותו זיהוי כפילויות ואותו
    dead-letter. אין כאן מסלול קליטה שני.

    הקוד הוא 200 גם כשחלק מהשורות נדחו: תערובת של נקלט/כפול/dead-letter
    היא התוצאה הצפויה של קובץ אמיתי, לא שגיאה. 400 נשמר למקרים שבהם
    *הקובץ* אינו שמיש - ואז לא נקלטה אף שורה.
    """
    raw = await file.read()
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"file is {len(raw)} bytes; the limit is {MAX_UPLOAD_BYTES}",
        )
    if not raw.strip():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="file is empty")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"file is not valid UTF-8 (byte {exc.start}); Hebrew text must be UTF-8 encoded",
        ) from exc

    parsed = parse_transcript_text(text, flight_id=flight_id, source_name=file.filename)
    if not parsed.flight_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "no flight_id: send it as a form field, or add a '# flight_id: <id>' "
                "line to the file"
            ),
        )
    if not parsed.messages:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"no message lines found in {parsed.total_lines} lines; expected "
                "'message_id | timestamp | source | channel | text'"
            ),
        )

    results = service.ingest_many(parsed.messages)
    counts = {item: 0 for item in MessageStatus}
    for result in results:
        counts[result.status] += 1

    return UploadResponse(
        report=UploadReport(
            filename=file.filename,
            flight_id=parsed.flight_id,
            flight_id_source="request" if flight_id else "directive",
            total_lines=parsed.total_lines,
            comment_lines=parsed.comment_lines,
            blank_lines=parsed.blank_lines,
            message_lines=parsed.message_count,
            short_lines=parsed.short_lines,
        ),
        accepted_count=counts[MessageStatus.ACCEPTED],
        duplicate_count=counts[MessageStatus.DUPLICATE],
        dead_letter_count=counts[MessageStatus.DEAD_LETTER],
        results=results,
    )


@transcript_router.get(
    "/flights",
    response_model=list[str],
    summary="הטיסות שיש להן תמלול שמור",
)
def list_flights(store: RawTranscriptStore = Depends(get_raw_store)) -> list[str]:
    return store.flights()


@transcript_router.get(
    "/flights/{flight_id}/messages",
    response_model=list[Message],
    summary="תמלול טיסה, בסדר כרונולוגי",
    response_description="משפטי הטיסה לפי החותמת המקורית - גם אם נקלטו שלא לפי הסדר",
)
def get_flight_transcript(
    flight_id: str = Path(description="מזהה הטיסה"),
    start: datetime | None = Query(default=None, description="תחילת טווח, ISO-8601 (כולל)"),
    end: datetime | None = Query(default=None, description="סוף טווח, ISO-8601 (כולל)"),
    limit: int | None = Query(default=None, ge=1, le=10000),
    store: RawTranscriptStore = Depends(get_raw_store),
) -> list[Message]:
    """שליפה לפי טיסה, ולפי טווח זמן בתוכה.

    הסינון הוא על ``timestamp`` - החותמת המקורית מהמקור - ולכן הודעה
    שהגיעה באיחור נשלפת בטווח שבו נאמרה, לא בזה שבו נקלטה.
    """
    if start is None and end is None:
        return store.by_flight(flight_id, limit=limit)
    messages = store.by_time_range(flight_id, start, end)
    return messages[:limit] if limit is not None else messages


@transcript_router.get(
    "/messages/{message_id}",
    response_model=Message,
    summary="משפט בודד לפי מזהה",
    responses={status.HTTP_404_NOT_FOUND: {"model": ErrorResponse, "description": "אין הודעה כזו"}},
)
def get_message(
    message_id: str = Path(description="מזהה ההודעה"),
    store: RawTranscriptStore = Depends(get_raw_store),
) -> Message:
    """המשפט כפי שנקלט, ללא שינוי - זו הדרך לחזור מאירוע לתמלול המקורי."""
    message = store.get(message_id)
    if message is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="message not found")
    return message


@operations_router.get(
    "/dead-letters",
    response_model=list[DeadLetterRecord],
    summary="שליפת הודעות שנדחו",
    response_description="רשומות dead-letter, מהחדשה לישנה",
)
def list_dead_letters(
    flight_id: str | None = Query(default=None, description="סינון לטיסה אחת"),
    limit: int = Query(default=100, ge=1, le=1000),
    service: IngestionService = Depends(get_ingestion_service),
) -> list[DeadLetterRecord]:
    """לוג ה-dead-letter: כל הודעה שנדחתה, עם הסיבה והמקור הגולמי שלה,
    כדי שאפשר יהיה לתקן במקור ולהזרים מחדש."""
    records = service.dead_letters
    if flight_id is not None:
        records = [record for record in records if record.flight_id == flight_id]
    return list(reversed(records))[:limit]


@operations_router.get(
    "/queue",
    response_model=QueueStatus,
    summary="מצב התור הפנימי",
)
def queue_status(service: IngestionService = Depends(get_ingestion_service)) -> QueueStatus:
    queue = service.queue
    depth = len(queue) if isinstance(queue, InMemoryQueue) else 0
    return QueueStatus(depth=depth)


@operations_router.get("/health", response_model=HealthResponse, summary="בריאות השירות")
def health() -> HealthResponse:
    return HealthResponse(status="ok", version=API_VERSION, implemented_stages=IMPLEMENTED_STAGES)


@operations_router.get(
    "/status",
    response_model=SystemStatus,
    summary="מצב המערכת כולה",
    response_description="שלבים, רכיבים, מאגר, תור, dead-letter, זמינות המודל והספים בתוקף",
)
def system_status(
    store: RawTranscriptStore = Depends(get_raw_store),
    service: IngestionService = Depends(get_ingestion_service),
) -> SystemStatus:
    """התשובה שמאחורי הדשבורד ב-``/``.

    מפריד במפורש בין רכיב ש*נבנה* לרכיב ש*חשוף* ב-API, ובין המודל
    שמוגדר ב-config למודל שבאמת מותקן ב-ollama - שתי הבחנות שבלעדיהן
    דף מצב מציג תמונה אופטימית מהמצב בפועל.
    """
    return build_status(store, service, API_VERSION, resolved_store_path())


@app_router.get(
    "/",
    include_in_schema=False,
    summary="דשבורד מצב המערכת",
)
def dashboard() -> FileResponse:
    return FileResponse(DASHBOARD_FILE, media_type="text/html")


def create_app() -> FastAPI:
    app = FastAPI(
        title="aura API",
        summary="קליטת תמלול טיסה, זיהוי אירועים וסיכום - ממשק HTTP",
        description=DESCRIPTION,
        version=API_VERSION,
        openapi_tags=TAGS_METADATA,
        docs_url="/docs",
        redoc_url="/redoc",
    )
    app.include_router(ingestion_router)
    app.include_router(transcript_router)
    app.include_router(operations_router)
    app.include_router(app_router)
    return app


app = create_app()
