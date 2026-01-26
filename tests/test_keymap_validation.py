import pytest

from pihub.dispatcher import Dispatcher


def test_validate_keymap_rejects_unknown_do() -> None:
    doc = {
        "scancode_map": {},
        "activities": {"watch": {"rem_ok": [{"do": "wat"}]}},
    }
    with pytest.raises(ValueError, match="unknown do"):
        Dispatcher._validate_keymap(doc)


def test_validate_keymap_rejects_non_dict_action() -> None:
    doc = {
        "scancode_map": {},
        "activities": {"watch": {"rem_ok": ["not-a-dict"]}},
    }
    with pytest.raises(ValueError, match="must be a dict"):
        Dispatcher._validate_keymap(doc)
