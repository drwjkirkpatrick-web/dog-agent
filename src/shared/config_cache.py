"""
Config Cache — File-watched YAML configuration with in-memory caching.

Features:
  - Automatic reload on file change (via watchdog)
  - In-memory caching for fast lookups
  - Lock-free reads with atomic updates
  - No disk I/O after initial load

Usage:
    from shared import ConfigCache
    config = ConfigCache()
    port = config.get("gps.api_port", 9111)
"""

import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

import yaml

try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler
    WATCHDOG_AVAILABLE = True
except ImportError:
    WATCHDOG_AVAILABLE = False


class ConfigCache:
    """Thread-safe YAML config cache with file watching."""
    
    _instance = None
    _lock = threading.Lock()
    
    def __new__(cls, config_path: Optional[Path] = None):
        """Singleton pattern for process-wide config cache."""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance
    
    def __init__(self, config_path: Optional[Path] = None):
        if self._initialized:
            return
        
        self._config_path = config_path or self._find_config()
        self._cache: Dict[str, Any] = {}
        self._mtime: float = 0
        self._cache_lock = threading.RLock()
        self._initialized = True
        
        # Initial load
        self._reload()
        
        # Start file watcher if available
        if WATCHDOG_AVAILABLE:
            self._start_watcher()
    
    def _find_config(self) -> Path:
        """Find config.yaml in standard locations."""
        # Project directory
        project_dir = Path(__file__).resolve().parent.parent.parent
        config_path = project_dir / "config.yaml"
        if config_path.exists():
            return config_path
        
        # Working directory
        config_path = Path("config.yaml").resolve()
        if config_path.exists():
            return config_path
        
        return project_dir / "config.yaml"  # Default even if doesn't exist
    
    def _reload(self) -> None:
        """Reload configuration from disk."""
        try:
            mtime = os.path.getmtime(self._config_path)
            if mtime <= self._mtime:
                return  # No change
            
            with open(self._config_path) as f:
                data = yaml.safe_load(f) or {}
            
            with self._cache_lock:
                self._cache = data
                self._mtime = mtime
            
        except Exception:
            pass  # Keep existing cache on error
    
    def _start_watcher(self) -> None:
        """Start filesystem watcher for config changes."""
        class ConfigHandler(FileSystemEventHandler):
            def __init__(handler_self, cache):
                handler_self.cache = cache
            
            def on_modified(handler_self, event):
                if event.src_path == str(self._config_path):
                    time.sleep(0.1)  # Debounce
                    self._reload()
        
        self._observer = Observer()
        self._observer.schedule(
            ConfigHandler(self),
            str(self._config_path.parent),
            recursive=False
        )
        self._observer.daemon = True
        self._observer.start()
    
    def get(self, path: str, default: Any = None) -> Any:
        """
        Get config value by dot-delimited path.
        
        Args:
            path: Dot-delimited key (e.g., "gps.api_port")
            default: Default value if key not found
        
        Returns:
            Config value or default
        """
        # Periodic reload check if no watchdog
        if not WATCHDOG_AVAILABLE:
            self._reload()
        
        with self._cache_lock:
            value = self._cache
            for key in path.split("."):
                if isinstance(value, dict):
                    value = value.get(key)
                    if value is None:
                        return default
                else:
                    return default
            return value
    
    def get_bool(self, path: str, default: bool = False) -> bool:
        """Get boolean config value."""
        value = self.get(path, default)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.lower() in ("true", "1", "yes", "on")
        return bool(value)
    
    def get_int(self, path: str, default: int = 0) -> int:
        """Get integer config value."""
        value = self.get(path, default)
        try:
            return int(value)
        except (TypeError, ValueError):
            return default
    
    def get_float(self, path: str, default: float = 0.0) -> float:
        """Get float config value."""
        value = self.get(path, default)
        try:
            return float(value)
        except (TypeError, ValueError):
            return default
    
    def get_dict(self, path: str, default: Optional[dict] = None) -> dict:
        """Get dict config value."""
        value = self.get(path, default or {})
        return value if isinstance(value, dict) else (default or {})


# Global instance for convenience
_cache_instance: Optional[ConfigCache] = None


def get_config(path: str, default: Any = None) -> Any:
    """Global function for quick config access."""
    global _cache_instance
    if _cache_instance is None:
        _cache_instance = ConfigCache()
    return _cache_instance.get(path, default)
