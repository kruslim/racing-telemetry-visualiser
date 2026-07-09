"""Runtime decoding of raw telemetry values for the API / WebSocket layers.

The store keeps raw ints for bitfields/enums (fidelity, narrow schema); this
module expands them to human-friendly forms on read.
"""

from __future__ import annotations

from typing import Any

from rtv.catalog.enums import (
    BITFIELD_TABLES,
    ENUM_TABLES,
    decode_bitfield,
    decode_enum,
)
from rtv.catalog.models import Catalog


def decode_flags(name: str, raw: int) -> dict[str, bool] | None:
    """Decode a bitfield variable's raw int to named booleans, if known."""
    table = BITFIELD_TABLES.get(name)
    return decode_bitfield(table, raw) if table is not None else None


def decode_label(name: str, raw: int) -> dict[str, Any] | None:
    """Decode an enum variable's raw int to ``{value, label}``, if known."""
    table = ENUM_TABLES.get(name)
    if table is None:
        return None
    return {"value": int(raw), "label": decode_enum(table, raw)}


def decode_value(catalog: Catalog, name: str, raw: Any) -> Any:
    """Decode a single value according to its catalog descriptor.

    Bitfields -> {flag: bool}; enums -> {value, label}; everything else passes
    through unchanged. Arrays are decoded element-wise.
    """
    var = catalog.get(name)
    if var is None:
        return raw

    if var.decoder == "bitfield":
        if var.is_array and isinstance(raw, (list, tuple)):
            return [decode_flags(name, v) for v in raw]
        return decode_flags(name, raw)

    if var.decoder == "enum":
        if var.is_array and isinstance(raw, (list, tuple)):
            return [decode_label(name, v) for v in raw]
        return decode_label(name, raw)

    return raw
