"""Bitfield and enum decoding."""

from __future__ import annotations

from rtv.catalog.decode import decode_flags, decode_label, decode_value
from rtv.catalog.enums import SESSION_FLAGS, SESSION_STATE, decode_bitfield, decode_enum
from tests.helpers import synthetic_catalog


def test_decode_bitfield_named_booleans():
    raw = SESSION_FLAGS["green"] | SESSION_FLAGS["one_lap_to_green"]
    decoded = decode_bitfield(SESSION_FLAGS, raw)
    assert decoded["green"] is True
    assert decoded["one_lap_to_green"] is True
    assert decoded["yellow"] is False


def test_decode_enum_label_and_fallback():
    assert decode_enum(SESSION_STATE, 4) == "racing"
    assert decode_enum(SESSION_STATE, 999) == "value:999"


def test_decode_flags_label_helpers():
    assert decode_flags("SessionFlags", SESSION_FLAGS["green"])["green"] is True
    assert decode_flags("NotABitfield", 1) is None
    assert decode_label("SessionState", 4) == {"value": 4, "label": "racing"}
    assert decode_label("NotAnEnum", 1) is None


def test_decode_value_uses_catalog():
    cat = synthetic_catalog()
    flags = decode_value(cat, "SessionFlags", SESSION_FLAGS["green"])
    assert flags["green"] is True
    label = decode_value(cat, "SessionState", 4)
    assert label["label"] == "racing"
    assert decode_value(cat, "Speed", 61.2) == 61.2
