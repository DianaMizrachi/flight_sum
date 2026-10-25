from functools import cache
from pathlib import Path

import yaml

from aura.schemas.taxonomy import EventTypeDefinition

_DEFAULT_PATH = Path(__file__).with_name("event_types.yaml")


@cache
def load_event_taxonomy(path: Path | None = None) -> list[EventTypeDefinition]:
    """Load and validate the event taxonomy from YAML.

    Raises pydantic.ValidationError if the file doesn't match
    EventTypeDefinition - this is what makes an incompatible taxonomy
    edit fail fast in CI instead of silently reaching the LLM engine.
    """
    target = path or _DEFAULT_PATH
    raw = yaml.safe_load(target.read_text(encoding="utf-8")) or []
    definitions = [EventTypeDefinition.model_validate(item) for item in raw]

    ids = [d.id for d in definitions]
    if len(ids) != len(set(ids)):
        duplicates = {i for i in ids if ids.count(i) > 1}
        raise ValueError(f"duplicate event type ids in taxonomy: {duplicates}")

    return definitions


def get_event_type(type_id: str) -> EventTypeDefinition | None:
    for definition in load_event_taxonomy():
        if definition.id == type_id:
            return definition
    return None
