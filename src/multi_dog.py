#!/usr/bin/env python3
"""
Dog Agent — Multi-Dog Household
===============================
Support for multiple dogs with one agent installation.

Features:
  - Per-dog profiles
  - Individual tracking per dog
  - Shared device, separate data
  - Dog switching/sensing which dog is wearing device

Usage:
    python src/multi_dog.py
    python src/multi_dog.py --simulate
"""

from __future__ import annotations

import argparse
import json
import logging
import signal
import sqlite3
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

# Logging
logger = logging.getLogger("multi_dog")
handler = logging.StreamHandler(sys.stdout)
handler.setFormatter(logging.Formatter(
    "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
))
logger.addHandler(handler)
logger.setLevel(logging.INFO)

PROJECT_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_DIR / "config.yaml"
DB_PATH = PROJECT_DIR / "data" / "multi_dog.db"


def load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH) as f:
                return yaml.safe_load(f) or {}
        except Exception as e:
            logger.warning(f"Failed to load config: {e}")
    return {}


def get_cfg(path: str, default: Any = None) -> Any:
    cfg = load_config()
    for key in path.split("."):
        if isinstance(cfg, dict):
            cfg = cfg.get(key)
        else:
            return default
    return cfg if cfg is not None else default


@dataclass
class DogProfile:
    dog_id: str
    name: str
    breed: str
    age_years: float
    weight_kg: float
    collar_color: str  # For visual identification
    is_active: bool
    created_at: datetime


class MultiDogManager:
    """Manages multiple dog profiles."""
    
    def __init__(self):
        self.enabled = get_cfg("multi_dog.enabled", False)
        self.current_dog_id: Optional[str] = None
        
        self._db: Optional[sqlite3.Connection] = None
        self._lock = threading.Lock()
        
        self._init_db()
        self._load_active_dog()
    
    def _init_db(self) -> None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS dogs (
                dog_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                breed TEXT,
                age_years REAL,
                weight_kg REAL,
                collar_color TEXT,
                is_active BOOLEAN DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS dog_switches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                from_dog_id TEXT,
                to_dog_id TEXT,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                reason TEXT
            );
        """)
        self._db.commit()
    
    def _load_active_dog(self) -> None:
        """Load currently active dog from database."""
        cursor = self._db.execute(
            "SELECT dog_id FROM dogs WHERE is_active = 1 LIMIT 1"
        )
        row = cursor.fetchone()
        if row:
            self.current_dog_id = row["dog_id"]
            logger.info(f"Active dog: {self.current_dog_id}")
    
    def add_dog(self, name: str, breed: str = "", age_years: float = 0,
                weight_kg: float = 0, collar_color: str = "") -> str:
        """Add a new dog profile."""
        dog_id = f"dog_{int(time.time())}"
        
        with self._lock:
            self._db.execute(
                """INSERT INTO dogs (dog_id, name, breed, age_years, weight_kg, collar_color, is_active)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (dog_id, name, breed, age_years, weight_kg, collar_color, False)
            )
            self._db.commit()
        
        logger.info(f"Added dog: {name} ({dog_id})")
        return dog_id
    
    def switch_dog(self, dog_id: str, reason: str = "manual") -> bool:
        """Switch to a different dog profile."""
        with self._lock:
            # Deactivate current
            if self.current_dog_id:
                self._db.execute(
                    "UPDATE dogs SET is_active = 0 WHERE dog_id = ?",
                    (self.current_dog_id,)
                )
            
            # Activate new
            self._db.execute(
                "UPDATE dogs SET is_active = 1 WHERE dog_id = ?",
                (dog_id,)
            )
            
            # Log switch
            self._db.execute(
                "INSERT INTO dog_switches (from_dog_id, to_dog_id, reason) VALUES (?, ?, ?)",
                (self.current_dog_id, dog_id, reason)
            )
            
            self._db.commit()
            self.current_dog_id = dog_id
        
        logger.info(f"Switched to dog: {dog_id}")
        return True
    
    def get_current_dog(self) -> Optional[Dict]:
        """Get currently active dog profile."""
        if not self.current_dog_id:
            return None
        
        cursor = self._db.execute(
            "SELECT * FROM dogs WHERE dog_id = ?",
            (self.current_dog_id,)
        )
        row = cursor.fetchone()
        
        if row:
            return dict(row)
        return None
    
    def list_dogs(self) -> List[Dict]:
        """List all registered dogs."""
        cursor = self._db.execute("SELECT * FROM dogs ORDER BY created_at")
        return [dict(row) for row in cursor]
    
    def get_switch_history(self, days: int = 30) -> List[Dict]:
        """Get history of dog switches."""
        cursor = self._db.execute(
            """SELECT * FROM dog_switches
               WHERE timestamp > datetime('now', ?)
               ORDER BY timestamp DESC""",
            (f"-{days} days",)
        )
        return [dict(row) for row in cursor]


class MultiDogHTTPHandler(BaseHTTPRequestHandler):
    """HTTP API for multi-dog support."""
    
    manager: Optional[MultiDogManager] = None
    
    def log_message(self, format, *args):
        logger.debug(f"HTTP: {format % args}")
    
    def _send_json(self, data: Any, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data, indent=2).encode())
    
    def do_GET(self):
        path = self.path.strip("/")
        
        if path == "dogs/health":
            self._send_json({
                "status": "ok",
                "service": "multi_dog",
                "enabled": bool(self.manager and self.manager.enabled),
            })
        elif path == "dogs/current":
            if not self.manager:
                self._send_json({"error": "Manager not initialized"}, 503)
                return
            dog = self.manager.get_current_dog()
            if dog:
                self._send_json(dog)
            else:
                self._send_json({"error": "No active dog"}, 404)
        elif path == "dogs/list":
            if not self.manager:
                self._send_json({"error": "Manager not initialized"}, 503)
                return
            self._send_json({"dogs": self.manager.list_dogs()})
        elif path == "dogs/history":
            if not self.manager:
                self._send_json({"error": "Manager not initialized"}, 503)
                return
            history = self.manager.get_switch_history()
            self._send_json({"switches": history})
        else:
            self._send_json({"error": f"Unknown endpoint: {path}"}, 404)
    
    def do_POST(self):
        path = self.path.strip("/")
        
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length).decode() if content_length > 0 else "{}"
        
        try:
            data = json.loads(body) if body else {}
        except json.JSONDecodeError:
            self._send_json({"error": "Invalid JSON"}, 400)
            return
        
        if path == "dogs/add":
            if not self.manager:
                self._send_json({"error": "Manager not initialized"}, 503)
                return
            dog_id = self.manager.add_dog(
                name=data.get("name", "Unnamed"),
                breed=data.get("breed", ""),
                age_years=data.get("age_years", 0),
                weight_kg=data.get("weight_kg", 0),
                collar_color=data.get("collar_color", ""),
            )
            self._send_json({"dog_id": dog_id, "status": "created"})
        
        elif path == "dogs/switch":
            if not self.manager:
                self._send_json({"error": "Manager not initialized"}, 503)
                return
            dog_id = data.get("dog_id")
            if not dog_id:
                self._send_json({"error": "dog_id required"}, 400)
                return
            success = self.manager.switch_dog(dog_id, data.get("reason", "manual"))
            self._send_json({"switched": success, "dog_id": dog_id})
        
        else:
            self._send_json({"error": f"Unknown endpoint: {path}"}, 404)


def main():
    parser = argparse.ArgumentParser(description="Dog Agent — Multi-Dog Household")
    parser.add_argument("--port", type=int, default=9146, help="HTTP API port")
    parser.add_argument("--simulate", action="store_true", help="Run simulation")
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose logging")
    args = parser.parse_args()
    
    if args.verbose:
        logger.setLevel(logging.DEBUG)
    
    manager = MultiDogManager()
    
    if args.simulate:
        logger.info("=== Multi-Dog Simulation ===")
        dog_id = manager.add_dog("Buddy", "Labrador", 3, 25, "blue")
        logger.info(f"Added dog: {dog_id}")
        dogs = manager.list_dogs()
        logger.info(f"Total dogs: {len(dogs)}")
        return
    
    MultiDogHTTPHandler.manager = manager
    
    server = HTTPServer(("127.0.0.1", args.port), MultiDogHTTPHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    logger.info(f"Multi-dog API on http://127.0.0.1:{args.port}")
    
    def signal_handler(sig, frame):
        logger.info(f"Signal {sig} received")
        server.shutdown()
    
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)
    
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        logger.info("Multi-dog module stopped")


if __name__ == "__main__":
    main()
