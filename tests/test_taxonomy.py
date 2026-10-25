from aura.taxonomy import get_event_type, load_event_taxonomy


def test_taxonomy_loads_and_validates():
    definitions = load_event_taxonomy()
    assert len(definitions) >= 4
    ids = {d.id for d in definitions}
    assert {"contact_with_target", "loss_of_contact", "mission_change", "malfunction"} <= ids


def test_every_definition_has_examples_and_applicable_types():
    for definition in load_event_taxonomy():
        assert definition.examples_he, f"{definition.id} has no examples"
        assert definition.applicable_entity_types, f"{definition.id} has no applicable entity types"


def test_get_event_type_unknown_returns_none():
    assert get_event_type("not_a_real_type") is None
