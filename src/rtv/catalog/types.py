"""The iRacing telemetry type system and its mapping to Python / JSON / DuckDB.

Every telemetry variable has exactly one of six wire types (``irsdk_VarType``).
This module is the single source of truth for how each maps downstream; the
ingest, store and API layers all defer to it.
"""

from __future__ import annotations

from enum import IntEnum, StrEnum
from typing import NamedTuple


class IRType(IntEnum):
    """The six iRacing SDK variable types (``irsdk_VarType`` enum values)."""

    CHAR = 0  # single byte char ('c')
    BOOL = 1  # single byte bool ('?')
    INT = 2  # 32-bit signed int ('i') — also used for enums
    BITFIELD = 3  # 32-bit unsigned int ('I') — decoded into named booleans
    FLOAT = 4  # 32-bit float ('f')
    DOUBLE = 5  # 64-bit float ('d')


class StorageStrategy(StrEnum):
    """How a variable's value is laid out in storage / on the wire."""

    SCALAR = "scalar"  # count == 1
    FLATTENED = "flattened"  # small array -> Name_0..Name_n columns
    LIST = "list"  # large array -> a single LIST column


class TypeMapping(NamedTuple):
    ir_type: IRType
    label: str  # human/JSON label, e.g. "float"
    struct_char: str  # struct/ctypes format char
    n_bytes: int
    python_type: type
    json_type: str  # JSON Schema primitive
    duckdb_type: str  # DuckDB scalar column type
    arrow_type: str  # logical pyarrow type name (resolved lazily in store layer)


# The canonical mapping table. Order matches IRType values 0..5.
TYPE_MAP: dict[IRType, TypeMapping] = {
    IRType.CHAR: TypeMapping(
        IRType.CHAR, "char", "c", 1, str, "string", "VARCHAR", "string"
    ),
    IRType.BOOL: TypeMapping(
        IRType.BOOL, "bool", "?", 1, bool, "boolean", "BOOLEAN", "bool"
    ),
    IRType.INT: TypeMapping(
        IRType.INT, "int", "i", 4, int, "integer", "INTEGER", "int32"
    ),
    IRType.BITFIELD: TypeMapping(
        # Unsigned 32-bit on the wire ('I'); a set high bit (e.g. SessionFlags
        # start_go = 0x80000000) exceeds signed int32, so store as 64-bit.
        IRType.BITFIELD, "bitfield", "I", 4, int, "integer", "BIGINT", "int64"
    ),
    IRType.FLOAT: TypeMapping(
        IRType.FLOAT, "float", "f", 4, float, "number", "FLOAT", "float32"
    ),
    IRType.DOUBLE: TypeMapping(
        IRType.DOUBLE, "double", "d", 8, float, "number", "DOUBLE", "float64"
    ),
}


def mapping_for(ir_type: int | IRType) -> TypeMapping:
    """Return the TypeMapping for an irSDK type value, raising on unknown types."""
    try:
        return TYPE_MAP[IRType(ir_type)]
    except ValueError as exc:  # pragma: no cover - guards against SDK changes
        raise ValueError(f"Unknown irSDK variable type: {ir_type!r}") from exc


def storage_strategy(count: int, flatten_max: int) -> StorageStrategy:
    """Decide how an array variable is stored, given its element count."""
    if count <= 1:
        return StorageStrategy.SCALAR
    if count <= flatten_max:
        return StorageStrategy.FLATTENED
    return StorageStrategy.LIST
