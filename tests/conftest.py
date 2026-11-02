from pathlib import Path

import pytest
import yaml

from aura.ingestion.fixture_transcripts import (
    FixtureTranscript,
    load_fixture_transcript,
    load_fixture_transcripts,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures"
GOLDEN_FLIGHTS_DIR = FIXTURES_DIR / "golden_flights"
EDGE_CASES_DIR = FIXTURES_DIR / "edge_cases"
MODEL_EVAL_DIR = FIXTURES_DIR / "model_eval"
SAMPLES_DIR = Path(__file__).resolve().parents[1] / "samples"

# כל פיקסצ'ר תגיות שמחזיק משפטים, בלי קשר למשפחה שלו - הקלט של
# tests/test_fixture_ingestion.py.
ALL_FIXTURE_PATHS = sorted(FIXTURES_DIR.glob("*/*.yaml"))


def load_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


@pytest.fixture(params=sorted(GOLDEN_FLIGHTS_DIR.glob("*.yaml")), ids=lambda p: p.stem)
def golden_flight(request) -> dict:
    return load_yaml(request.param)


@pytest.fixture
def golden_flight_paths() -> list[Path]:
    return sorted(GOLDEN_FLIGHTS_DIR.glob("*.yaml"))


@pytest.fixture
def edge_case_path(request) -> Path:
    return EDGE_CASES_DIR / request.param


@pytest.fixture(params=sorted(MODEL_EVAL_DIR.glob("*.yaml")), ids=lambda p: p.stem)
def model_eval_scenario(request) -> dict:
    return load_yaml(request.param)


@pytest.fixture
def model_eval_paths() -> list[Path]:
    return sorted(MODEL_EVAL_DIR.glob("*.yaml"))


@pytest.fixture(params=ALL_FIXTURE_PATHS, ids=lambda p: p.stem)
def fixture_transcript(request) -> FixtureTranscript:
    """המשפטים של פיקסצ'ר אחד, כרצף קלט אחד לטיסה אחת."""
    return load_fixture_transcript(request.param)


@pytest.fixture
def all_fixture_transcripts() -> list[FixtureTranscript]:
    return load_fixture_transcripts(FIXTURES_DIR)


@pytest.fixture
def samples_dir() -> Path:
    return SAMPLES_DIR
