"""Build a :class:`Catalog` from binary telemetry headers.

The same builder serves the live SDK (``ir._var_headers``) and ``.ibt`` files
(``ibt._var_headers``) — both expose header entries with the same attributes.
Nothing about the variable set is hard-coded: we read whatever the header
describes, so the catalog is complete for any car/content.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol

from rtv.catalog.enums import ENUM_TABLES
from rtv.catalog.models import Catalog, DecoderKind, VariableDescriptor
from rtv.catalog.types import IRType, StorageStrategy, mapping_for, storage_strategy


class HeaderLike(Protocol):
    """Structural type matching pyirsdk's VarHeader (live and IBT)."""

    name: str
    type: int
    count: int
    count_as_time: bool
    desc: str
    unit: str


@dataclass(frozen=True)
class RawHeader:
    """Concrete header used for tests / synthetic data."""

    name: str
    type: int
    count: int = 1
    count_as_time: bool = False
    desc: str = ""
    unit: str = ""


def _classify_decoder(name: str, ir_type: IRType) -> DecoderKind:
    if ir_type is IRType.BITFIELD:
        return "bitfield"
    if name in ENUM_TABLES:
        return "enum"
    return "none"


def _columns(
    name: str, ir_type: IRType, count: int, strategy: StorageStrategy
) -> list[tuple[str, str]]:
    base = mapping_for(ir_type).duckdb_type
    if strategy is StorageStrategy.SCALAR:
        return [(name, base)]
    if strategy is StorageStrategy.FLATTENED:
        return [(f"{name}_{i}", base) for i in range(count)]
    # LIST: a single list-typed column.
    return [(name, f"{base}[]")]


def _descriptor(header: HeaderLike, flatten_max: int) -> VariableDescriptor:
    ir_type = IRType(int(header.type))
    count = int(getattr(header, "count", 1) or 1)
    strategy = storage_strategy(count, flatten_max)
    name = str(header.name)
    return VariableDescriptor(
        name=name,
        ir_type=ir_type,
        count=count,
        unit=str(getattr(header, "unit", "") or ""),
        desc=str(getattr(header, "desc", "") or ""),
        count_as_time=bool(getattr(header, "count_as_time", False)),
        storage=strategy,
        decoder=_classify_decoder(name, ir_type),
        columns=_columns(name, ir_type, count, strategy),
    )


def build_catalog(
    headers: Iterable[HeaderLike],
    *,
    source: str = "ibt",
    flatten_max: int = 6,
    car_id: str | None = None,
    track_id: str | None = None,
) -> Catalog:
    """Construct a :class:`Catalog` from an iterable of header entries."""
    descriptors = [_descriptor(h, flatten_max) for h in headers]
    variables = {d.name: d for d in descriptors}
    schema_hash = Catalog.compute_hash(descriptors)
    return Catalog(
        variables=variables,
        source="live" if source == "live" else "ibt",
        schema_hash=schema_hash,
        car_id=car_id,
        track_id=track_id,
    )
