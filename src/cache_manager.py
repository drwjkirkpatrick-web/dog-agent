#!/usr/bin/env python3
"""
Dog Agent — Command Cache Manager
=================================
In-memory cache with TTL for speeding up Hermes queries and reducing redundant API calls.

Features:
  - Thread-safe cache with TTL per entry
  - LRU eviction when memory limit reached
  - Cache warming on startup
  - Statistics tracking (hit rate, miss rate)
  - HTTP API for cache introspection and management

Usage:
    python src/cache_manager.py                # Normal mode
    python src/cache_manager.py --simulate     # Simulate cache operations
    python src/cache_manager.py --port 9123    # Custom port
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Callable, Dict, Optional, TypeVar

import yaml

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logger = logging.getLogger("cache_manager")
_handler = logging.StreamHandler(sys.stdout)
_handler.setFormatter(logging.Formatter(
    "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
))
logger.addHandler(_handler)
logger.setLevel(logging.INFO)


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
PROJECT_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_DIR / "config.yaml"
CACHE_DB_PATH = PROJECT_DIR / "data" / "cache.db"

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
DEFAULT_CONFIG = {
    "cache": {
        "enabled": True,
        "memory_limit": 100,
        "warm_on_startup": True,
    }
}


def load_config() -> dict:
    """Load configuration from config.yaml."""
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH) as f:
                return yaml.safe_load(f) or {}
        except Exception as e:
            logger.warning(f"Failed to load config: {e}")
    return {}


def get_cfg(path: str, default: Any = None) -> Any:
    """Get config value by dot-delimited path."""
    cfg = load_config()
    for key in path.split("."):
        if isinstance(cfg, dict):
            cfg = cfg.get(key)
        else:
            return default
    return cfg if cfg is not None else default


# ---------------------------------------------------------------------------
# Cache Entry
# ---------------------------------------------------------------------------

@dataclass
class CacheEntry:
    """A single cache entry with TTL."""
    key: str
    data: Any
    created_at: float
    ttl_sec: float
    access_count: int = field(default=0)
    last_accessed: float = field(default_factory=time.time)

    def is_expired(self) -> bool:
        """Check if this entry has exceeded its TTL."""
        return time.time() - self.created_at > self.ttl_sec

    def touch(self) -> None:
        """Update access metadata."""
        self.access_count += 1
        self.last_accessed = time.time()


# ---------------------------------------------------------------------------
# Cache Manager
# ---------------------------------------------------------------------------

T = TypeVar('T')


class CacheManager:
    """
    Thread-safe cache with TTL and LRU eviction.
    
    Implements an OrderedDict for O(1) LRU tracking.
    """

    # Default TTLs for different data types
    DEFAULT_TTLS: Dict[str, float] = {
        "gps:position": 10.0,           # GPS updates frequently
        "health:summary": 60.0,          # Health changes slowly
        "health:vitals": 30.0,           # Vitals moderately
        "behavior:summary": 300.0,       # Behavior changes slowly
        "zone:all": 30.0,                # Zones change rarely
        "zone:status": 30.0,             # Status changes on events
        "environmental:current": 60.0,   # Weather changes slowly
        "environmental:weather": 300.0,  # Weather summaries
    }

    def __init__(self, memory_limit: int = 100):
        self.memory_limit = memory_limit
        self._cache: OrderedDict[str, CacheEntry] = OrderedDict()
        self._lock = threading.RLock()
        self._stats = {
            "hits": 0,
            "misses": 0,
            "evictions": 0,
            "invalidations": 0,
        }
        self._running = True
        self._cleanup_thread = threading.Thread(target=self._cleanup_loop, daemon=True)
        self._cleanup_thread.start()
        logger.info(f"CacheManager initialized (limit={memory_limit})")

    def get(self, key: str, default: T = None) -> T:
        """
        Retrieve a value from cache.
        
        Returns default if:
          - Key not found
          - Entry expired (and removes it)
        """
        with self._lock:
            entry = self._cache.get(key)
            if entry is None:
                self._stats["misses"] += 1
                return default
            
            if entry.is_expired():
                del self._cache[key]
                self._stats["misses"] += 1
                return default
            
            # Update LRU order (move to end)
            self._cache.move_to_end(key)
            entry.touch()
            self._stats["hits"] += 1
            return entry.data

    def set(self, key: str, data: Any, ttl_sec: Optional[float] = None) -> None:
        """
        Store a value in cache.
        
        If ttl_sec not provided, uses default for key prefix or 60s.
        """
        if ttl_sec is None:
            ttl_sec = self._get_default_ttl(key)
        
        with self._lock:
            # Evict oldest if at capacity
            if len(self._cache) >= self.memory_limit and key not in self._cache:
                self._evict_oldest()
            
            entry = CacheEntry(
                key=key,
                data=data,
                created_at=time.time(),
                ttl_sec=ttl_sec,
            )
            self._cache[key] = entry
            self._cache.move_to_end(key)
            logger.debug(f"Cached: {key} (TTL={ttl_sec}s)")

    def delete(self, key: str) -> bool:
        """Remove a key from cache. Returns True if existed."""
        with self._lock:
            if key in self._cache:
                del self._cache[key]
                self._stats["invalidations"] += 1
                return True
            return False

    def clear(self) -> int:
        """Clear all entries. Returns count cleared."""
        with self._lock:
            count = len(self._cache)
            self._cache.clear()
            self._stats["invalidations"] += count
            logger.info(f"Cache cleared: {count} entries")
            return count

    def keys(self) -> list:
        """Return all cache keys."""
        with self._lock:
            return list(self._cache.keys())

    def stats(self) -> dict:
        """Return cache statistics."""
        with self._lock:
            total_requests = self._stats["hits"] + self._stats["misses"]
            hit_rate = self._stats["hits"] / total_requests if total_requests > 0 else 0.0
            
            return {
                "entries": len(self._cache),
                "memory_limit": self.memory_limit,
                "memory_usage_percent": len(self._cache) / self.memory_limit * 100,
                "hits": self._stats["hits"],
                "misses": self._stats["misses"],
                "hit_rate": round(hit_rate, 4),
                "evictions": self._stats["evictions"],
                "invalidations": self._stats["invalidations"],
                "total_requests": total_requests,
            }

    def _get_default_ttl(self, key: str) -> float:
        """Get default TTL based on key prefix."""
        for prefix, ttl in self.DEFAULT_TTLS.items():
            if key.startswith(prefix) or key == prefix:
                return ttl
        return 60.0  # Default 1 minute

    def _evict_oldest(self) -> None:
        """Evict the least-recently-used entry."""
        if self._cache:
            oldest_key = next(iter(self._cache))
            del self._cache[oldest_key]
            self._stats["evictions"] += 1
            logger.debug(f"Evicted: {oldest_key}")

    def _cleanup_loop(self) -> None:
        """Background thread to clean expired entries."""
        while self._running:
            time.sleep(30)  # Run every 30 seconds
            self._cleanup_expired()

    def _cleanup_expired(self) -> int:
        """Remove expired entries. Returns count removed."""
        expired_keys = []
        now = time.time()
        
        with self._lock:
            for key, entry in self._cache.items():
                if now - entry.created_at > entry.ttl_sec:
                    expired_keys.append(key)
            
            for key in expired_keys:
                del self._cache[key]
        
        if expired_keys:
            logger.debug(f"Cleaned {len(expired_keys)} expired entries")
        return len(expired_keys)

    def stop(self) -> None:
        """Stop the cleanup thread."""
        self._running = False
        self._cleanup_thread.join(timeout=1)

    def warm_cache(self) -> None:
        """Pre-populate cache with critical keys."""
        logger.info("Warming cache...")
        # These would normally fetch from other modules
        self.set("cache:warmed_at", datetime.now(timezone.utc).isoformat(), ttl_sec=3600)
        logger.info("Cache warming complete")


# ---------------------------------------------------------------------------
# Decorator for function caching
# ---------------------------------------------------------------------------

def cached(cache_mgr: CacheManager, ttl_sec: Optional[float] = None, key_fn: Optional[Callable] = None):
    """
    Decorator to cache function results.
    
    Usage:
        @cached(cache, ttl_sec=60)
        def get_expensive_data(param):
            return fetch_from_api(param)
    """
    def decorator(func: Callable) -> Callable:
        def wrapper(*args, **kwargs):
            # Generate cache key
            if key_fn:
                cache_key = key_fn(*args, **kwargs)
            else:
                cache_key = f"{func.__name__}:{str(args)}:{str(kwargs)}"
            
            # Try cache first
            result = cache_mgr.get(cache_key)
            if result is not None:
                return result
            
            # Cache miss - compute and store
            result = func(*args, **kwargs)
            cache_mgr.set(cache_key, result, ttl_sec)
            return result
        return wrapper
    return decorator


# ---------------------------------------------------------------------------
# HTTP API
# ---------------------------------------------------------------------------

class CacheHTTPHandler(BaseHTTPRequestHandler):
    """HTTP request handler for cache management API."""

    cache_manager: Optional[CacheManager] = None

    def log_message(self, format, *args):
        logger.debug(f"HTTP: {format % args}")

    def _send_json(self, data: Any, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data, indent=2, default=str).encode())

    def _send_error(self, status: int, message: str) -> None:
        self._send_json({"error": message}, status)

    def do_GET(self):
        path = self.path.strip("/")
        parts = path.split("/")

        if path == "cache/health":
            self._send_json({
                "status": "ok",
                "service": "cache_manager",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })

        elif path == "cache/stats":
            if self.cache_manager:
                self._send_json(self.cache_manager.stats())
            else:
                self._send_error(503, "Cache manager not initialized")

        elif path.startswith("cache/") and len(parts) == 2:
            key = parts[1]
            if self.cache_manager:
                value = self.cache_manager.get(key)
                if value is not None:
                    self._send_json({
                        "key": key,
                        "found": True,
                        "data": value,
                    })
                else:
                    self._send_json({
                        "key": key,
                        "found": False,
                        "data": None,
                    })
            else:
                self._send_error(503, "Cache manager not initialized")

        elif path == "cache/keys":
            if self.cache_manager:
                self._send_json({"keys": self.cache_manager.keys()})
            else:
                self._send_error(503, "Cache manager not initialized")

        else:
            self._send_error(404, f"Unknown endpoint: {path}")

    def do_POST(self):
        path = self.path.strip("/")
        parts = path.split("/")

        # Read body
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length).decode() if content_length > 0 else "{}"
        
        try:
            data = json.loads(body) if body else {}
        except json.JSONDecodeError:
            self._send_error(400, "Invalid JSON body")
            return

        if path.startswith("cache/") and len(parts) == 2:
            key = parts[1]
            if self.cache_manager:
                ttl = data.get("ttl_sec")
                self.cache_manager.set(key, data.get("data"), ttl)
                self._send_json({"status": "stored", "key": key})
            else:
                self._send_error(503, "Cache manager not initialized")

        elif path == "cache/invalidate/all":
            if self.cache_manager:
                count = self.cache_manager.clear()
                self._send_json({"status": "cleared", "entries_removed": count})
            else:
                self._send_error(503, "Cache manager not initialized")

        else:
            self._send_error(404, f"Unknown endpoint: {path}")

    def do_DELETE(self):
        path = self.path.strip("/")
        parts = path.split("/")

        if path.startswith("cache/") and len(parts) == 2:
            key = parts[1]
            if self.cache_manager:
                existed = self.cache_manager.delete(key)
                self._send_json({
                    "status": "deleted" if existed else "not_found",
                    "key": key,
                })
            else:
                self._send_error(503, "Cache manager not initialized")

        else:
            self._send_error(404, f"Unknown endpoint: {path}")

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()


# ---------------------------------------------------------------------------
# Simulation mode
# ---------------------------------------------------------------------------

class CacheSimulator:
    """Simulates cache operations for testing."""

    def __init__(self, cache: CacheManager):
        self.cache = cache
        self.running = True

    def run(self):
        """Run simulation scenarios."""
        logger.info("=== Cache Simulation Mode ===")
        
        # Scenario 1: Basic operations
        logger.info("\n[Scenario 1] Basic set/get operations")
        self.cache.set("gps:position", {"lat": 45.5152, "lon": -122.6784})
        result = self.cache.get("gps:position")
        logger.info(f"  GPS position: {result}")
        
        # Scenario 2: Expiration
        logger.info("\n[Scenario 2] TTL expiration")
        self.cache.set("temp:key", "value", ttl_sec=2)
        logger.info(f"  Immediately: {self.cache.get('temp:key')}")
        time.sleep(2.5)
        logger.info(f"  After 2.5s: {self.cache.get('temp:key')}")
        
        # Scenario 3: LRU eviction
        logger.info("\n[Scenario 3] LRU eviction (small cache)")
        small_cache = CacheManager(memory_limit=3)
        small_cache.set("a", 1)
        small_cache.set("b", 2)
        small_cache.set("c", 3)
        small_cache.set("d", 4)  # Should evict "a"
        logger.info(f"  Keys after overflow: {small_cache.keys()}")
        
        # Scenario 4: Statistics
        logger.info("\n[Scenario 4] Statistics")
        stats = self.cache.stats()
        logger.info(f"  Stats: {json.dumps(stats, indent=2)}")
        
        logger.info("\n=== Simulation Complete ===")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Dog Agent — Command Cache Manager")
    parser.add_argument("--port", type=int, default=9123, help="HTTP API port")
    parser.add_argument("--simulate", action="store_true", help="Run simulation mode")
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose logging")
    args = parser.parse_args()

    if args.verbose:
        logger.setLevel(logging.DEBUG)

    # Load config
    memory_limit = get_cfg("cache.memory_limit", 100)
    warm_on_startup = get_cfg("cache.warm_on_startup", True)

    # Create cache manager
    cache = CacheManager(memory_limit=memory_limit)

    if warm_on_startup:
        cache.warm_cache()

    if args.simulate:
        sim = CacheSimulator(cache)
        sim.run()
        cache.stop()
        return

    # Set up HTTP handler
    CacheHTTPHandler.cache_manager = cache

    # Start HTTP server
    server = HTTPServer(("127.0.0.1", args.port), CacheHTTPHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    logger.info(f"Cache API running on http://127.0.0.1:{args.port}")

    # Run until interrupted
    def signal_handler(sig, frame):
        logger.info(f"Received signal {sig}")
        cache.stop()
        server.shutdown()

    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        cache.stop()
        server.shutdown()
        logger.info("Cache manager stopped")


if __name__ == "__main__":
    main()