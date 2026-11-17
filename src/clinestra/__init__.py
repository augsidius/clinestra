"""Clinestra: an open-source harness for longitudinal clinical care.

    from clinestra import Harness, Actor
    h = Harness()
"""
from ._version import __version__
from .base import Actor
from .errors import ClinestraError, Invalid, InvariantViolation, NotFound, PermissionDenied
from .harness import Harness
from .providers import BlobStore, EvidenceProvider, LLMProvider, LocalBlobStore, MemoryBlobStore
from .spec import SPECS
from .storage import MemoryStore, PostgresStore, SQLiteStore  # psycopg is imported only when a PostgresStore is made

__all__ = ["__version__", "Harness", "Actor", "SPECS", "MemoryStore", "SQLiteStore", "PostgresStore", "ClinestraError", "NotFound",
           "PermissionDenied", "InvariantViolation", "Invalid", "EvidenceProvider", "LLMProvider",
           "BlobStore", "MemoryBlobStore", "LocalBlobStore"]
