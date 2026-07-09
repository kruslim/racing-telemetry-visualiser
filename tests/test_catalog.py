"""Catalog: type mapping, array strategies, decoder classification."""

from __future__ import annotations

from rtv.catalog.types import IRType, StorageStrategy, mapping_for, storage_strategy
from tests.helpers import synthetic_catalog


def test_all_six_types_map_cleanly():
    expected = {
        IRType.CHAR: ("char", "VARCHAR", "string"),
        IRType.BOOL: ("bool", "BOOLEAN", "boolean"),
        IRType.INT: ("int", "INTEGER", "integer"),
        IRType.BITFIELD: ("bitfield", "BIGINT", "integer"),
        IRType.FLOAT: ("float", "FLOAT", "number"),
        IRType.DOUBLE: ("double", "DOUBLE", "number"),
    }
    for ir_type, (label, duck, json_type) in expected.items():
        m = mapping_for(ir_type)
        assert m.label == label
        assert m.duckdb_type == duck
        assert m.json_type == json_type


def test_storage_strategy_thresholds():
    assert storage_strategy(1, 6) is StorageStrategy.SCALAR
    assert storage_strategy(4, 6) is StorageStrategy.FLATTENED
    assert storage_strategy(64, 6) is StorageStrategy.LIST


def test_builder_columns_and_decoders():
    cat = synthetic_catalog()

    speed = cat["Speed"]
    assert speed.storage is StorageStrategy.SCALAR
    assert speed.columns == [("Speed", "FLOAT")]
    assert speed.decoder == "none"

    tyres = cat["TyrePressure"]
    assert tyres.storage is StorageStrategy.FLATTENED
    assert [c for c, _ in tyres.columns] == [
        "TyrePressure_0",
        "TyrePressure_1",
        "TyrePressure_2",
        "TyrePressure_3",
    ]

    caridx = cat["CarIdxLapDistPct"]
    assert caridx.storage is StorageStrategy.LIST
    assert caridx.columns == [("CarIdxLapDistPct", "FLOAT[]")]

    assert cat["SessionFlags"].decoder == "bitfield"
    assert "green" in (cat["SessionFlags"].flag_names or [])
    assert cat["SessionState"].decoder == "enum"
    assert cat["SessionState"].enum_labels[4] == "racing"


def test_schema_hash_is_stable_and_order_independent():
    a = synthetic_catalog()
    b = synthetic_catalog()
    assert a.schema_hash == b.schema_hash


def test_catalog_api_contract():
    cat = synthetic_catalog()
    api = cat.to_api()
    assert api["count"] == len(cat)
    names = {v["name"] for v in api["variables"]}
    assert {"Speed", "TyrePressure", "SessionFlags", "CarIdxLapDistPct"} <= names
    flags_var = next(v for v in api["variables"] if v["name"] == "SessionFlags")
    assert flags_var["decoder"] == "bitfield"
    assert "green" in flags_var["flags"]
