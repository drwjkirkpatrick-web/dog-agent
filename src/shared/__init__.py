"""
Dog Agent v4.0 — Shared Infrastructure
======================================
High-performance utilities for optimized module operation.

Usage:
    from shared import ConfigCache, ConnectionPool, CircuitBreaker
"""

__version__ = "4.0.0"

from .config_cache import ConfigCache
from .connection_pool import ConnectionPool
from .circuit_breaker import CircuitBreaker
from .shared_memory import SharedMemoryManager
from .asyncio_utils import AsyncioRunner

__all__ = [
    "ConfigCache",
    "ConnectionPool", 
    "CircuitBreaker",
    "SharedMemoryManager",
    "AsyncioRunner",
]
