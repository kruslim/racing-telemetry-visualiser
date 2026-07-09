"""The common currency between ingestion and storage: columnar frame buffers.

Both the live poller and the .ibt importer accumulate decoded ticks into a
:class:`FrameBuffer` and hand the resulting Arrow table to the store. Building
columnar (not row-by-row) is what makes 60 Hz persistence viable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from rtv.catalog.models import Catalog
from rtv.catalog.types import IRType

# Canonical meta columns present on every telemetry row (the query axes).
META_COLUMNS = ("tick", "session_time", "lap", "wall_time")


@dataclass
class Frame:
    """A single decoded tick (used for live fan-out, not bulk storage)."""

    tick: int
    session_time: float
    lap: int
    values: dict[str, Any]
    wall_time: float | None = None


def _arrow_type(ir_type: IRType):
    import pyarrow as pa

    return {
        IRType.CHAR: pa.string(),
        IRType.BOOL: pa.bool_(),
        IRType.INT: pa.int32(),
        IRType.BITFIELD: pa.int64(),
        IRType.FLOAT: pa.float32(),
        IRType.DOUBLE: pa.float64(),
    }[ir_type]


@dataclass
class FrameBuffer:
    """Accumulates ticks column-wise, then emits a pyarrow Table.

    Column buffers are created lazily from the catalog on first append so the
    schema exactly matches the variables present in the source.
    """

    catalog: Catalog
    _cols: dict[str, list] = field(default_factory=dict, init=False)
    _meta: dict[str, list] = field(default_factory=dict, init=False)
    _n: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        for name in META_COLUMNS:
            self._meta[name] = []
        for var in self.catalog.variables.values():
            for col_name, _ in var.columns:
                self._cols[col_name] = []

    def __len__(self) -> int:
        return self._n

    def append(
        self,
        values: dict[str, Any],
        *,
        tick: int,
        session_time: float,
        lap: int,
        wall_time: float | None = None,
    ) -> None:
        self._meta["tick"].append(int(tick))
        self._meta["session_time"].append(float(session_time))
        self._meta["lap"].append(int(lap))
        self._meta["wall_time"].append(wall_time)

        for var in self.catalog.variables.values():
            raw = values.get(var.name)
            if var.storage.value == "scalar":
                self._cols[var.name].append(raw)
            elif var.storage.value == "flattened":
                seq = raw if isinstance(raw, (list, tuple)) else [None] * var.count
                for i, (col_name, _) in enumerate(var.columns):
                    self._cols[col_name].append(seq[i] if i < len(seq) else None)
            else:  # list
                col_name = var.columns[0][0]
                self._cols[col_name].append(
                    list(raw) if isinstance(raw, (list, tuple)) else None
                )
        self._n += 1

    def clear(self) -> None:
        for buf in self._meta.values():
            buf.clear()
        for buf in self._cols.values():
            buf.clear()
        self._n = 0

    def to_arrow(self):
        """Materialise the buffered rows into a pyarrow Table."""
        import pyarrow as pa

        arrays: list[pa.Array] = []
        names: list[str] = []

        # Meta columns first.
        arrays.append(pa.array(self._meta["tick"], type=pa.int64()))
        names.append("tick")
        arrays.append(pa.array(self._meta["session_time"], type=pa.float64()))
        names.append("session_time")
        arrays.append(pa.array(self._meta["lap"], type=pa.int32()))
        names.append("lap")
        arrays.append(pa.array(self._meta["wall_time"], type=pa.float64()))
        names.append("wall_time")

        # Variable columns, typed from the catalog.
        for var in self.catalog.variables.values():
            atype = _arrow_type(var.ir_type)
            if var.storage.value == "list":
                col_name = var.columns[0][0]
                arrays.append(pa.array(self._cols[col_name], type=pa.list_(atype)))
                names.append(col_name)
            else:
                for col_name, _ in var.columns:
                    arrays.append(pa.array(self._cols[col_name], type=atype))
                    names.append(col_name)

        return pa.Table.from_arrays(arrays, names=names)
