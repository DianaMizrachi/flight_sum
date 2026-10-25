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
    assert body["implemented_stages"] == [0, 1, 2]


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
        "/messages/{message_id}",
        "/flights",
        "/flights/{flight_id}/messages",
        "/dead-letters",
        "/queue",
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
