"""Variable-catalog subsystem.

Pure, I/O-free. Knows the *shapes* of iRacing telemetry variables: the six wire
types, how they map to Python / JSON / DuckDB, how arrays are stored, and the
stable bitfield/enum decode tables. The catalog is built from the binary header
at runtime so it is complete by construction for whatever content is loaded.
"""

from rtv.catalog.builder import build_catalog
from rtv.catalog.models import Catalog, VariableDescriptor
from rtv.catalog.types import IRType, StorageStrategy

__all__ = [
    "IRType",
    "StorageStrategy",
    "VariableDescriptor",
    "Catalog",
    "build_catalog",
]
