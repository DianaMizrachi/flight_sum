from pathlib import Path

import pytest
import yaml

FIXTURES_DIR = Path(__file__).parent / "fixtures"
GOLDEN_FLIGHTS_DIR = FIXTURES_DIR / "golden_flights"
EDGE_CASES_DIR = FIXTURES_DIR / "edge_cases"
MODEL_EVAL_DIR = FIXTURES_DIR / "model_eval"


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
