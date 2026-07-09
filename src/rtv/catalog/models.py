"""Catalog data models: the runtime description of every telemetry variable."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Literal

from rtv.catalog.enums import BITFIELD_TABLES, ENUM_TABLES
from rtv.catalog.types import IRType, StorageStrategy, mapping_for

DecoderKind = Literal["none", "bitfield", "enum"]


@dataclass(frozen=True)
class VariableDescriptor:
    """Everything we know about a single telemetry variable, from the header."""

    name: str
    ir_type: IRType
    count: int
    unit: str
    desc: str
    count_as_time: bool
    storage: StorageStrategy
    decoder: DecoderKind
    # Column names + DuckDB types this variable expands to (>=1 entries).
    columns: list[tuple[str, str]] = field(default_factory=list)

    # ---- convenience views -------------------------------------------------
    @property
    def type_label(self) -> str:
        return mapping_for(self.ir_type).label

    @property
    def json_type(self) -> str:
        return mapping_for(self.ir_type).json_type

    @property
    def is_array(self) -> bool:
        return self.count > 1

    @property
    def flag_names(self) -> list[str] | None:
        """For bitfields, the ordered list of decodable flag names."""
        if self.decoder != "bitfield":
            return None
        table = BITFIELD_TABLES.get(self.name)
        return list(table.keys()) if table else None

    @property
    def enum_labels(self) -> dict[int, str] | None:
        """For enums, the value->label map."""
        if self.decoder != "enum":
            return None
        return ENUM_TABLES.get(self.name)

    def to_api(self) -> dict:
        """Serialise to the public ``/variables`` JSON contract."""
        out: dict = {
            "name": self.name,
            "ir_type": self.type_label,
            "json_type": self.json_type,
            "count": self.count,
            "unit": self.unit,
            "desc": self.desc,
            "storage": self.storage.value,
            "count_as_time": self.count_as_time,
            "columns": [c for c, _ in self.columns],
        }
        if self.decoder == "bitfield":
            out["decoder"] = "bitfield"
            out["flags"] = self.flag_names
        elif self.decoder == "enum":
            out["decoder"] = "enum"
            out["enum"] = {str(k): v for k, v in (self.enum_labels or {}).items()}
        return out


@dataclass
class Catalog:
    """The full set of variables for a session, plus a schema fingerprint."""

    variables: dict[str, VariableDescriptor]
    source: Literal["live", "ibt"]
    schema_hash: str
    car_id: str | None = None
    track_id: str | None = None

    def __len__(self) -> int:
        return len(self.variables)

    def __contains__(self, name: str) -> bool:
        return name in self.variables

    def __getitem__(self, name: str) -> VariableDescriptor:
        return self.variables[name]

    def get(self, name: str) -> VariableDescriptor | None:
        return self.variables.get(name)

    @property
    def names(self) -> list[str]:
        return list(self.variables)

    def all_columns(self) -> list[tuple[str, str]]:
        """Flat list of every (column_name, duckdb_type) across all variables."""
        cols: list[tuple[str, str]] = []
        for var in self.variables.values():
            cols.extend(var.columns)
        return cols

    def to_api(self) -> dict:
        return {
            "schema_hash": self.schema_hash,
            "source": self.source,
            "car_id": self.car_id,
            "track_id": self.track_id,
            "count": len(self.variables),
            "variables": [v.to_api() for v in self.variables.values()],
        }

    @staticmethod
    def compute_hash(descriptors: list[VariableDescriptor]) -> str:
        """Stable fingerprint of the variable set (name, type, count)."""
        sig = ";".join(
            f"{d.name}:{int(d.ir_type)}:{d.count}"
            for d in sorted(descriptors, key=lambda d: d.name)
        )
        return hashlib.sha1(sig.encode("utf-8")).hexdigest()[:16]
