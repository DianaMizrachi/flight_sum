"""Tests for the HTTP API and its published OpenAPI spec.

Two things are checked here: the ingest endpoints behave like the component
behind them (same statuses, same dead-letter reasons), and the spec
committed under docs/ still matches the app.
"""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from aura.api.app import create_app
from aura.api.dependencies import get_ingestion_service, get_raw_store
from aura.ingestion import IngestionService
from aura.raw_store import RawStoreDedupIndex, SQLiteRawTranscriptStore

FIXTURES_DIR = Path(__file__).parent / "fixtures"
EDGE_CASES_DIR = FIXTURES_DIR / "edge_cases"
DOCS_DIR = Path(__file__).resolve().parents[1] / "docs"

FROZEN_NOW = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)

VALID_PAYLOAD = {
    "id": "flight-01-m04",
    "flight_id": "flight-01",
    "timestamp": "2026-09-01T06:12:00Z",
    "source": "operator",
    "channel": "command",
    "text": "בקרה, נץ אחת, קשר עין עם רכב בודד בציר המזרחי.",
}


def load_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


@pytest.fixture
def store() -> SQLiteRawTranscriptStore:
    with SQLiteRawTranscriptStore(":memory:") as opened:
        yield opened


@pytest.fixture
def service(store: SQLiteRawTranscriptStore) -> IngestionService:
    return IngestionService(
        store=store, dedup_index=RawStoreDedupIndex(store), clock=lambda: FROZEN_NOW
    )


@pytest.fixture
def client(service: IngestionService, store: SQLiteRawTranscriptStore) -> TestClient:
    app = create_app()
    app.dependency_overrides[get_ingestion_service] = lambda: service
    app.dependency_overrides[get_raw_store] = lambda: store
    with TestClient(app) as test_client:
        yield test_client


# --- קליטה דרך HTTP -----------------------------------------------------


def test_valid_message_returns_201_and_is_queued(client: TestClient):
    response = client.post("/messages", json=VALID_PAYLOAD)

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "accepted"
    assert body["queued"] is True
    assert body["dedup_strategy"] == "by_id"
    assert body["message"]["id"] == VALID_PAYLOAD["id"]
    assert body["message"]["text"] == VALID_PAYLOAD["text"]
    assert body["dead_letter"] is None
    assert client.get("/queue").json() == {"depth": 1}


def test_resending_the_same_message_returns_200_duplicate(client: TestClient):
    first = client.post("/messages", json=VALID_PAYLOAD)
    second = client.post("/messages", json=VALID_PAYLOAD)

    assert first.status_code == 201
    assert second.status_code == 200
    body = second.json()
    assert body["status"] == "duplicate"
    assert body["queued"] is False
    assert body["duplicate_of"] == VALID_PAYLOAD["id"]
    assert client.get("/queue").json() == {"depth": 1}, "a duplicate is not queued twice"


def test_message_without_an_id_is_accepted_and_deduped_by_hash(client: TestClient):
    payload = {k: v for k, v in VALID_PAYLOAD.items() if k != "id"}

    first = client.post("/messages", json=payload)
    second = client.post("/messages", json=payload)

    assert first.status_code == 201
    assert first.json()["dedup_strategy"] == "by_hash"
    assert second.status_code == 200
    assert second.json()["duplicate_of"] == first.json()["message"]["id"]


def test_malformed_payloads_return_422_with_the_dead_letter_reason(client: TestClient):
    data = load_yaml(EDGE_CASES_DIR / "malformed_and_empty.yaml")

    for case in data["cases"]:
        expected = case["expected"]
        response = client.post("/messages", json=case["raw_input"])

        assert response.status_code == 422, case["case_id"]
        body = response.json()
        assert body["status"] == "dead_letter", case["case_id"]
        assert body["message"] is None, case["case_id"]
        assert body["dead_letter"]["reason"] == expected["reason"], case["case_id"]
        if "missing_field" in expected:
            assert body["dead_letter"]["missing_field"] == expected["missing_field"]


def test_dead_letters_endpoint_returns_rejections_newest_first(client: TestClient):
    data = load_yaml(EDGE_CASES_DIR / "malformed_and_empty.yaml")
    for case in data["cases"]:
        client.post("/messages", json=case["raw_input"])
    client.post("/messages", json=VALID_PAYLOAD)

    response = client.get("/dead-letters")

    assert response.status_code == 200
    records = response.json()
    assert len(records) == len(data["cases"])
    assert records[0]["message_id"] == data["cases"][-1]["raw_input"]["id"], "newest first"
    assert all(record["raw_payload"] for record in records), "the raw payload is kept"


def test_dead_letters_endpoint_filters_by_flight_and_limit(client: TestClient):
    data = load_yaml(EDGE_CASES_DIR / "malformed_and_empty.yaml")
    for case in data["cases"]:
        client.post("/messages", json=case["raw_input"])

    assert client.get("/dead-letters", params={"flight_id": "flight-edge-03"}).json()
    assert client.get("/dead-letters", params={"flight_id": "nope"}).json() == []
    assert len(client.get("/dead-letters", params={"limit": 2}).json()) == 2


def test_unknown_source_fields_are_passed_through_and_recorded(client: TestClient):
    response = client.post(
        "/messages", json={**VALID_PAYLOAD, "confidence_from_asr": "0.82"}
    )

    assert response.status_code == 201
    assert response.json()["message"]["metadata"]["unmapped_fields"] == "confidence_from_asr"


# --- קליטה מרוכזת ------------------------------------------------------


def test_batch_endpoint_reports_mixed_outcomes_with_200(client: TestClient):
    duplicates = load_yaml(EDGE_CASES_DIR / "duplicates.yaml")
    malformed = load_yaml(EDGE_CASES_DIR / "malformed_and_empty.yaml")
    case = next(c for c in duplicates["cases"] if c["case_id"] == "duplicate-exact-id")
    messages = [*case["raw_inputs"], malformed["cases"][1]["raw_input"]]

    response = client.post("/messages/batch", json={"messages": messages})

    assert response.status_code == 200
    body = response.json()
    assert body["accepted_count"] == 1
    assert body["duplicate_count"] == 1
    assert body["dead_letter_count"] == 1
    assert [r["status"] for r in body["results"]] == ["accepted", "duplicate", "dead_letter"]


def test_batch_endpoint_rejects_an_empty_list(client: TestClient):
    assert client.post("/messages/batch", json={"messages": []}).status_code == 422


def test_health_reports_the_implemented_stages(client: TestClient):
    body = client.get("/health").json()

    assert body["status"] == "ok"
    # רכיבים 3-4 נבנו אך אינם חשופים כנקודות קצה; /status מפריד בין
    # השניים, ואילו /health מדווח רק מה נבנה בבנייה הזאת.
    assert body["implemented_stages"] == [0, 1, 2, 3, 4]


# --- ה-OpenAPI שמתפרסם -------------------------------------------------


def test_swagger_ui_and_spec_are_served(client: TestClient):
    assert client.get("/docs").status_code == 200
    assert client.get("/redoc").status_code == 200
    assert client.get("/openapi.json").status_code == 200


def test_spec_documents_every_endpoint_and_ingest_status_code(client: TestClient):
    spec = client.get("/openapi.json").json()

    assert spec["openapi"].startswith("3.1")
    assert spec["info"]["title"] == "aura API"
    assert set(spec["paths"]) == {
        "/messages",
        "/messages/batch",
        "/messages/upload",
        "/messages/{message_id}",
        "/flights",
        "/flights/{flight_id}/messages",
        "/dead-letters",
        "/queue",
        "/status",
        "/health",
    }
    ingest = spec["paths"]["/messages"]["post"]
    assert set(ingest["responses"]) >= {"200", "201", "422"}
    assert ingest["tags"] == ["ingestion"]
    assert spec["components"]["schemas"]["IngestRequest"]["examples"]


def test_committed_spec_matches_the_app(client: TestClient):
    """docs/openapi.{json,yaml} are committed; regenerate them with
    `python scripts/export_openapi.py` whenever the API changes."""
    live = client.get("/openapi.json").json()

    committed_json = json.loads((DOCS_DIR / "openapi.json").read_text(encoding="utf-8"))
    committed_yaml = yaml.safe_load((DOCS_DIR / "openapi.yaml").read_text(encoding="utf-8"))

    assert committed_json == live, "run scripts/export_openapi.py"
    assert committed_yaml == live, "run scripts/export_openapi.py"


# --- שליפה מהמאגר הגולמי (שלב 2) ---------------------------------------


def test_ingested_message_is_retrievable_by_id(client: TestClient):
    client.post("/messages", json=VALID_PAYLOAD)

    response = client.get(f"/messages/{VALID_PAYLOAD['id']}")

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == VALID_PAYLOAD["id"]
    assert body["text"] == VALID_PAYLOAD["text"]


def test_unknown_message_id_returns_404(client: TestClient):
    assert client.get("/messages/nope").status_code == 404


def test_flight_transcript_is_returned_in_chronological_order(client: TestClient):
    data = load_yaml(EDGE_CASES_DIR / "late_arrival.yaml")
    client.post("/messages/batch", json={"messages": data["raw_inputs"]})

    response = client.get("/flights/flight-edge-02/messages")

    assert response.status_code == 200
    assert [m["id"] for m in response.json()] == data["expected"][
        "chronological_order_by_timestamp"
    ]


def test_flight_transcript_filters_by_time_range_and_limit(client: TestClient):
    data = load_yaml(EDGE_CASES_DIR / "late_arrival.yaml")
    client.post("/messages/batch", json={"messages": data["raw_inputs"]})

    window = client.get(
        "/flights/flight-edge-02/messages",
        params={"start": "2026-09-05T10:00:05Z", "end": "2026-09-05T10:00:15Z"},
    )
    assert [m["id"] for m in window.json()] == ["flight-edge-02-m02"]

    first_only = client.get("/flights/flight-edge-02/messages", params={"limit": 1})
    assert [m["id"] for m in first_only.json()] == ["flight-edge-02-m01"]


def test_flights_endpoint_lists_stored_flights(client: TestClient):
    client.post("/messages", json=VALID_PAYLOAD)

    assert client.get("/flights").json() == ["flight-01"]


def test_dead_lettered_payloads_never_reach_the_transcript(client: TestClient):
    data = load_yaml(EDGE_CASES_DIR / "malformed_and_empty.yaml")
    for case in data["cases"]:
        client.post("/messages", json=case["raw_input"])

    assert client.get("/flights").json() == []
    assert len(client.get("/dead-letters").json()) == len(data["cases"])


# --- status endpoint and dashboard (part of stage 10) --------------------


def test_status_reports_stages_and_components(client):
    body = client.get("/status").json()
    assert body["status"] == "ok"
    assert body["stages_total"] == 13
    assert body["stages_built"] == [0, 1, 2, 3, 4]
    assert len(body["components"]) == 12
    assert body["uptime_seconds"] >= 0


def test_status_separates_built_from_exposed_over_http(client):
    """Components 3 and 4 are built but are libraries; nothing calls them
    over HTTP yet. Collapsing the two would make the page claim a running
    pipeline that does not exist until stage 6."""
    components = {c["id"]: c for c in client.get("/status").json()["components"]}
    assert components[3]["built"] and not components[3]["exposed_over_http"]
    assert components[4]["built"] and not components[4]["exposed_over_http"]
    assert components[1]["built"] and components[1]["exposed_over_http"]
    assert not components[5]["built"]


def test_status_counts_match_the_store_and_dead_letter_log(client):
    client.post(
        "/messages",
        json={
            "flight_id": "status-flight",
            "timestamp": "2026-10-28T08:00:00Z",
            "source": "operator",
            "text": "בקרה, בדיקת מצב.",
        },
    )
    client.post("/messages", json={"flight_id": "status-flight", "text": ""})

    body = client.get("/status").json()
    stored = client.get("/flights/status-flight/messages").json()
    assert body["store"]["message_count"] == len(stored)
    assert body["store"]["flight_count"] == len(client.get("/flights").json())
    assert body["dead_letters"]["total"] == len(client.get("/dead-letters").json())
    assert body["dead_letters"]["by_reason"], "a rejected message must be attributed to a reason"


def test_status_reports_the_window_thresholds_actually_in_effect(client):
    """The page must show the running config, not the plan's suggestion."""
    from aura.config import DEFAULT_SETTINGS

    settings = client.get("/status").json()["window_settings"]
    window = DEFAULT_SETTINGS.window
    assert settings["silence_threshold_seconds"] == window.silence_threshold_seconds
    assert settings["max_tokens_per_window"] == DEFAULT_SETTINGS.token_budget.max_tokens_per_window


def test_status_does_not_fail_when_ollama_is_unreachable(client, monkeypatch):
    """A missing model is information for the dashboard, not an outage."""
    from aura.api import status as status_module

    def boom(*args, **kwargs):
        raise OSError("connection refused")

    monkeypatch.setattr(status_module.httpx, "Client", boom)
    body = client.get("/status").json()
    assert body["status"] == "ok"
    assert body["llm"]["reachable"] is False
    assert "connection refused" in body["llm"]["error"]
    assert body["llm"]["available_models"] == []


def test_client_page_is_served_at_the_root(client):
    """`/` is the operator's page: entering sentences, not system internals."""
    response = client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "aura" in response.text


def test_root_is_the_client_and_not_the_ops_dashboard(client):
    """The two pages serve different readers. Work-plan stages, component
    tables and token budgets belong on /ops, never on the page someone uses
    to log a sentence during a debrief."""
    page = client.get("/").text
    for developer_only in ("stages_built", "components", "window_settings", "/status"):
        assert developer_only not in page, f"{developer_only!r} leaked into the client page"


def test_ops_dashboard_is_served_separately(client):
    response = client.get("/ops")
    assert response.status_code == 200
    assert "stages_built" in response.text or "/status" in response.text


@pytest.mark.parametrize("page_path", ["/", "/ops"])
def test_pages_only_call_endpoints_that_exist(client, page_path):
    """Both pages are plain JS with hard-coded paths; a renamed endpoint
    would otherwise break them silently, in a browser, and only for the
    person using the page.

    Matching against the published spec rather than issuing the requests
    keeps this method-agnostic: /messages/upload is POST-only, and a GET
    probe would report it missing when it is not.
    """
    import re

    page = client.get(page_path).text
    # הדפים קוראים ל-API גם דרך עוטף api() וגם דרך fetch ישיר, ובשלושה
    # סוגי מרכאות - החילוץ חייב לכסות את כולם, אחרת הבדיקה "עוברת" בלי
    # לבדוק דבר.
    called = set()
    for call in re.findall(r"""(?:api|fetch)\(\s*["`']([^"`']+)""", page):
        if call.startswith("/"):
            called.add(re.sub(r"\$\{[^}]*\}", "X", call).split("?")[0])
    assert called, "a page with no API calls would make this test vacuous"

    documented = [
        re.compile("^" + re.sub(r"\\\{[^}]*\\\}", "[^/]+", re.escape(spec_path)) + "$")
        for spec_path in client.get("/openapi.json").json()["paths"]
    ]
    for path in sorted(called):
        assert any(pattern.match(path) for pattern in documented), (
            f"{page_path} calls {path}, which is not in the API"
        )


def test_pages_are_excluded_from_the_openapi_spec(client):
    """The spec documents the API; the pages that consume it are not part of it."""
    paths = client.get("/openapi.json").json()["paths"]
    assert "/" not in paths
    assert "/ops" not in paths


# --- file upload --------------------------------------------------------


SAMPLE_FILE = Path(__file__).parent.parent / "samples" / "flight_sim_01.txt"


def upload(client, content: bytes, name: str = "t.txt", flight_id: str | None = None):
    data = {"flight_id": flight_id} if flight_id else None
    return client.post("/messages/upload", files={"file": (name, content, "text/plain")}, data=data)


def test_upload_ingests_the_sample_file_as_documented(client):
    """The sample file states its own expected outcome in a comment:
    23 accepted, 2 duplicates, 4 dead-lettered out of 29 message lines."""
    response = upload(client, SAMPLE_FILE.read_bytes(), "flight_sim_01.txt")

    assert response.status_code == 200
    body = response.json()
    counts = (body["accepted_count"], body["duplicate_count"], body["dead_letter_count"])
    assert counts == (23, 2, 4)
    assert body["report"]["message_lines"] == 29
    assert body["report"]["flight_id"] == "flight-sim-01"
    assert body["report"]["flight_id_source"] == "directive"
    assert body["report"]["short_lines"] == []


def test_uploaded_messages_are_retrievable_and_ordered(client):
    upload(client, SAMPLE_FILE.read_bytes(), "flight_sim_01.txt")

    messages = client.get("/flights/flight-sim-01/messages").json()
    assert len(messages) == 23
    stamps = [m["timestamp"] for m in messages]
    assert stamps == sorted(stamps), "a late line must come back in its spoken order"


def test_upload_records_the_source_line_for_traceability(client):
    """Every event must be traceable to its sentence; a file-fed sentence
    should also be traceable to the line it came from."""
    body = upload(client, SAMPLE_FILE.read_bytes(), "flight_sim_01.txt").json()
    accepted = [r for r in body["results"] if r["message"]]
    assert accepted
    for result in accepted:
        metadata = result["message"]["metadata"]
        assert metadata["source_file"] == "flight_sim_01.txt"
        assert metadata["source_line"].isdigit()


def test_upload_reuses_the_same_ingestion_rules(client):
    """No second ingestion path: normalisation and dedup must still apply."""
    content = (
        "# flight_id: upload-rules\n"
        "u-m01 | 2026-10-28T08:00:00Z | operator | command | בקרה,  נץ   אחת,  כפול.\n"
        "u-m01 | 2026-10-28T08:00:00Z | operator | command | בקרה,  נץ   אחת,  כפול.\n"
    ).encode()
    body = upload(client, content).json()

    assert (body["accepted_count"], body["duplicate_count"]) == (1, 1)
    message = body["results"][0]["message"]
    assert message["text"] == "בקרה, נץ אחת, כפול.", "whitespace must be normalised"
    assert message["metadata"]["raw_text"] == "בקרה,  נץ   אחת,  כפול."


def test_form_flight_id_overrides_the_directive_in_the_file(client):
    body = upload(client, SAMPLE_FILE.read_bytes(), "s.txt", flight_id="override-01").json()
    assert body["report"]["flight_id"] == "override-01"
    assert body["report"]["flight_id_source"] == "request"
    assert client.get("/flights/override-01/messages").json()


def test_upload_without_any_flight_id_is_rejected_whole(client):
    content = b"x | 2026-10-28T08:00:00Z | operator | command | test\n"
    response = upload(client, content)

    assert response.status_code == 400
    assert "flight_id" in response.json()["detail"]
    assert client.get("/flights").json() == [], "a rejected file must ingest nothing"


def test_upload_rejects_a_file_with_no_message_lines(client):
    response = upload(client, b"# only a comment\n\n# and another\n", flight_id="f")
    assert response.status_code == 400
    assert "no message lines" in response.json()["detail"]


def test_upload_rejects_an_empty_file(client):
    assert upload(client, b"   \n\n", flight_id="f").status_code == 400


def test_upload_rejects_non_utf8_so_hebrew_cannot_arrive_mangled(client):
    """Hebrew in cp1255 would decode to plausible-looking rubbish rather
    than fail, so the encoding is checked rather than guessed."""
    response = upload(client, "שלום | x".encode("cp1255"), flight_id="f")
    assert response.status_code == 400
    assert "UTF-8" in response.json()["detail"]


def test_upload_reports_short_lines_but_still_ingests_them(client):
    """A malformed line must reach the dead-letter log with a reason, not
    be dropped by the parser."""
    content = (
        "# flight_id: short-lines\n"
        "s-m01 | 2026-10-28T08:00:00Z | operator | command | תקין.\n"
        "s-m02 | 2026-10-28T08:00:30Z\n"
    ).encode()
    body = upload(client, content).json()

    assert body["report"]["short_lines"] == [3]
    assert body["accepted_count"] == 1
    assert body["dead_letter_count"] == 1
    assert body["results"][1]["dead_letter"]["reason"] == "missing_required_field"


def test_upload_keeps_a_pipe_inside_the_sentence(client):
    content = (
        "# flight_id: pipe-test\n"
        "p-m01 | 2026-10-28T08:00:00Z | operator | command | טקסט עם | צינור בתוכו.\n"
    ).encode()
    body = upload(client, content).json()
    assert body["results"][0]["message"]["text"] == "טקסט עם | צינור בתוכו."


def test_upload_is_documented_in_the_spec(client):
    spec = client.get("/openapi.json").json()
    assert "/messages/upload" in spec["paths"]
    body = spec["paths"]["/messages/upload"]["post"]["requestBody"]
    assert "multipart/form-data" in body["content"]


def test_client_translates_every_upload_error_the_api_can_return(client):
    """The client shows guidance, not the API's own wording.

    The API's details are written for someone reading Swagger ("send it as
    a form field..."). Leaking one into the client leaves the user with
    developer text and no idea what to do - which is exactly what happened
    before this test existed. So every 400 the upload endpoint can produce
    must be matched by the page's translation table.
    """
    import re

    page = client.get("/").text
    needles = re.findall(r'\["([^"]+)",\s*"', page)
    assert needles, "the client's upload-error table was not found"

    provoke = {
        "no flight id anywhere": (b"x | 2026-10-28T08:00:00Z | op | c | t\n", None),
        "only comments": (b"# nothing here\n", "f"),
        "empty file": (b"  \n", "f"),
        "not utf-8": ("שלום | x".encode("cp1255"), "f"),
        "too large": (b"a" * (5 * 1024 * 1024 + 1), "f"),
    }
    for label, (content, flight_id) in provoke.items():
        response = upload(client, content, flight_id=flight_id)
        assert response.status_code == 400, label
        detail = str(response.json()["detail"])
        assert any(needle in detail for needle in needles), (
            f"{label}: the client cannot translate {detail!r}"
        )


# --- completing a rejected line ----------------------------------------

INCOMPLETE_FILE = (
    "# flight_id: repair-01\n"
    "r-m01 | 2026-10-28T13:00:00Z | operator | command | בקרה, קשר עם מטרה אלפא.\n"
    "r-m02 | 2026-10-28T13:00:40Z |          | command | בקרה, המטרה נעה מזרחה.\n"
    "r-m03 |                      | operator | command | בקרה, איבדנו קשר.\n"
    "r-m04 | 2026-10-28T13:02:00Z | operator | command |\n"
).encode()


def test_rejected_line_keeps_every_field_it_did_have(client):
    """The client offers to fill in what a rejected line is missing, and it
    can only pre-fill the rest from the dead-letter record. If raw_payload
    ever stopped carrying the partial fields, the repair form would quietly
    render empty rows and the user would retype work the file already had."""
    body = upload(client, INCOMPLETE_FILE, "incomplete.txt").json()
    rejected = [r["dead_letter"] for r in body["results"] if r["dead_letter"]]
    assert len(rejected) == 3

    by_id = {record["raw_payload"].get("id"): record for record in rejected}

    missing_source = by_id["r-m02"]["raw_payload"]
    assert missing_source["timestamp"] == "2026-10-28T13:00:40Z"
    assert missing_source["text"] == "בקרה, המטרה נעה מזרחה."
    assert not missing_source.get("source")

    missing_time = by_id["r-m03"]["raw_payload"]
    assert missing_time["source"] == "operator"
    assert missing_time["text"] == "בקרה, איבדנו קשר."
    assert not missing_time.get("timestamp")

    missing_text = by_id["r-m04"]["raw_payload"]
    assert missing_text["timestamp"] and missing_text["source"]
    assert not missing_text["text"].strip()


def test_rejected_line_reports_which_line_of_the_file_it_was(client):
    body = upload(client, INCOMPLETE_FILE, "incomplete.txt").json()
    lines = [
        r["dead_letter"]["raw_payload"]["metadata"]["source_line"]
        for r in body["results"]
        if r["dead_letter"]
    ]
    assert lines == ["3", "4", "5"]


def test_completing_a_rejected_line_is_accepted_and_keeps_its_id(client):
    """The round trip the repair button performs."""
    upload(client, INCOMPLETE_FILE, "incomplete.txt")

    completed = client.post(
        "/messages",
        json={
            "flight_id": "repair-01",
            "id": "r-m04",
            "timestamp": "2026-10-28T13:02:00Z",
            "source": "operator",
            "channel": "command",
            "text": "בקרה, נץ אחת, חוזרים למסד.",
        },
    )
    assert completed.status_code == 201
    assert completed.json()["status"] == "accepted"
    assert completed.json()["message"]["id"] == "r-m04"

    texts = [m["text"] for m in client.get("/flights/repair-01/messages").json()]
    assert "בקרה, נץ אחת, חוזרים למסד." in texts


def test_a_line_still_missing_a_field_is_rejected_again_with_the_reason(client):
    """Partially completing a row must not sneak it through."""
    response = client.post(
        "/messages",
        json={"flight_id": "repair-01", "id": "r-m03", "source": "operator", "text": "בקרה."},
    )
    assert response.json()["status"] == "dead_letter"
    assert response.json()["dead_letter"]["reason"] == "missing_required_field"
