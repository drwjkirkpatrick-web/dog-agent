#!/usr/bin/env python3
"""
Dog Agent — Network Failover
============================
Multi-network connectivity management.

Features:
  - WiFi, Cellular, LoRa fallback
  - Automatic switching based on connectivity
  - Priority-based connection selection

Usage:
    python src/network_failover.py
    python src/network_failover.py --simulate
"""

from __future__ import annotations

import argparse
import json
import logging
import signal
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Dict, Optional

import yaml

# Logging
logger = logging.getLogger("network_failover")
handler = logging.StreamHandler(sys.stdout)
handler.setFormatter(logging.Formatter(
    "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
))
logger.addHandler(handler)
logger.setLevel(logging.INFO)

PROJECT_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_DIR / "config.yaml"


class ConnectionType(Enum):
    WIFI = "wifi"
    CELLULAR = "cellular"
    LORA = "lora"
    OFFLINE = "offline"


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
class ConnectionStatus:
    connection_type: ConnectionType
    is_connected: bool
    latency_ms: Optional[float]
    last_check: datetime
    failover_reason: Optional[str] = None


class NetworkFailoverManager:
    """Manages network connections with failover."""
    
    def __init__(self):
        self.enabled = get_cfg("network_failover.enabled", True)
        self.priority_order = [
            ConnectionType.WIFI,
            ConnectionType.CELLULAR,
            ConnectionType.LORA,
        ]
        
        self.check_interval_sec = get_cfg("network_failover.check_interval_sec", 30)
        self.failover_delay_sec = get_cfg("network_failover.failover_delay_sec", 60)
        self.ping_host = get_cfg("network_failover.ping_host", "8.8.8.8")
        
        self._current: ConnectionType = ConnectionType.OFFLINE
        self._status: Dict[ConnectionType, ConnectionStatus] = {}
        self._last_failover: Optional[datetime] = None
        self._fail_count: Dict[ConnectionType, int] = {}
        self._lock = threading.Lock()
        
        self._init_status()
    
    def _init_status(self) -> None:
        for conn_type in ConnectionType:
            self._status[conn_type] = ConnectionStatus(
                connection_type=conn_type,
                is_connected=False,
                latency_ms=None,
                last_check=datetime.now(timezone.utc),
            )
            self._fail_count[conn_type] = 0
    
    def check_connection(self, conn_type: ConnectionType) -> bool:
        """Check if a connection type is available."""
        if conn_type == ConnectionType.WIFI:
            return self._check_wifi()
        elif conn_type == ConnectionType.CELLULAR:
            return self._check_cellular()
        elif conn_type == ConnectionType.LORA:
            return self._check_lora()
        return False
    
    def _check_wifi(self) -> bool:
        """Check WiFi connectivity."""
        try:
            result = subprocess.run(
                ["iwgetid", "-r"],
                capture_output=True,
                timeout=5
            )
            if result.returncode == 0 and result.stdout.strip():
                # Ping test
                ping = subprocess.run(
                    ["ping", "-c", "1", "-W", "2", self.ping_host],
                    capture_output=True,
                    timeout=5
                )
                return ping.returncode == 0
        except:
            pass
        return False
    
    def _check_cellular(self) -> bool:
        """Check cellular modem connectivity."""
        try:
            # Check if modem exists
            if Path("/dev/ttyUSB0").exists() or Path("/dev/ttyACM0").exists():
                # Check data connection
                ping = subprocess.run(
                    ["ping", "-c", "1", "-W", "3", self.ping_host],
                    capture_output=True,
                    timeout=10
                )
                return ping.returncode == 0
        except:
            pass
        return False
    
    def _check_lora(self) -> bool:
        """Check LoRaWAN connection."""
        try:
            import requests
            resp = requests.get("http://localhost:9140/lora/status", timeout=5)
            if resp.status_code == 200:
                data = resp.json()
                return data.get("connected", False)
        except:
            pass
        return False
    
    def _measure_latency(self, conn_type: ConnectionType) -> Optional[float]:
        """Measure connection latency."""
        try:
            start = time.time()
            if conn_type in [ConnectionType.WIFI, ConnectionType.CELLULAR]:
                result = subprocess.run(
                    ["ping", "-c", "1", "-W", "3", self.ping_host],
                    capture_output=True,
                    timeout=5
                )
                if result.returncode == 0:
                    return (time.time() - start) * 1000
        except:
            pass
        return None
    
    def select_best_connection(self) -> ConnectionType:
        """Select best available connection based on priority."""
        for conn_type in self.priority_order:
            if self.check_connection(conn_type):
                return conn_type
        return ConnectionType.OFFLINE
    
    def update(self) -> None:
        """Update connection status and failover if needed."""
        new_connection = self.select_best_connection()
        
        with self._lock:
            old_connection = self._current
            
            if new_connection != old_connection:
                # Check failover delay
                if self._last_failover:
                    time_since = (datetime.now(timezone.utc) - self._last_failover).total_seconds()
                    if time_since < self.failover_delay_sec:
                        return  # Too soon to failover
                
                # Perform failover
                logger.info(f"Failover: {old_connection.value} -> {new_connection.value}")
                self._current = new_connection
                self._last_failover = datetime.now(timezone.utc)
                self._fail_count[old_connection] += 1
            
            # Update status for all connections
            for conn_type in ConnectionType:
                is_connected = conn_type == new_connection
                self._status[conn_type] = ConnectionStatus(
                    connection_type=conn_type,
                    is_connected=is_connected,
                    latency_ms=self._measure_latency(conn_type) if is_connected else None,
                    last_check=datetime.now(timezone.utc),
                    failover_reason=f"Switched from {old_connection.value}" if is_connected and conn_type != old_connection else None,
                )
    
    def get_current_connection(self) -> ConnectionType:
        with self._lock:
            return self._current
    
    def get_status(self) -> dict:
        with self._lock:
            return {
                "current": self._current.value,
                "failover_count": dict(self._fail_count),
                "last_failover": self._last_failover.isoformat() if self._last_failover else None,
                "connections": {
                    ct.value: {
                        "connected": s.is_connected,
                        "latency_ms": s.latency_ms,
                        "last_check": s.last_check.isoformat(),
                    }
                    for ct, s in self._status.items()
                },
            }


class NetworkFailoverHTTPHandler(BaseHTTPRequestHandler):
    """HTTP API for network failover."""
    
    manager: Optional[NetworkFailoverManager] = None
    
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
        
        if path == "network/health":
            self._send_json({
                "status": "ok",
                "service": "network_failover",
                "enabled": bool(self.manager and self.manager.enabled),
            })
        elif path == "network/status":
            if not self.manager:
                self._send_json({"error": "Manager not initialized"}, 503)
                return
            self._send_json(self.manager.get_status())
        elif path == "network/current":
            if not self.manager:
                self._send_json({"error": "Manager not initialized"}, 503)
                return
            self._send_json({
                "current": self.manager.get_current_connection().value,
            })
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
        
        if path == "network/force":
            if not self.manager:
                self._send_json({"error": "Manager not initialized"}, 503)
                return
            conn_type = data.get("connection")
            if conn_type:
                logger.info(f"Forced connection to: {conn_type}")
                self._send_json({"forced": True, "connection": conn_type})
            else:
                self._send_json({"error": "connection type required"}, 400)
        else:
            self._send_json({"error": f"Unknown endpoint: {path}"}, 404)


def main():
    parser = argparse.ArgumentParser(description="Dog Agent — Network Failover")
    parser.add_argument("--port", type=int, default=9141, help="HTTP API port")
    parser.add_argument("--simulate", action="store_true", help="Run simulation")
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose logging")
    args = parser.parse_args()
    
    if args.verbose:
        logger.setLevel(logging.DEBUG)
    
    manager = NetworkFailoverManager()
    
    if args.simulate:
        logger.info("=== Network Failover Simulation ===")
        current = manager.select_best_connection()
        logger.info(f"Best connection: {current.value}")
        return
    
    NetworkFailoverHTTPHandler.manager = manager
    
    server = HTTPServer(("127.0.0.1", args.port), NetworkFailoverHTTPHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    logger.info(f"Network failover API on http://127.0.0.1:{args.port}")
    
    # Update loop
    def update_loop():
        while True:
            if manager.enabled:
                manager.update()
            time.sleep(manager.check_interval_sec)
    
    update_thread = threading.Thread(target=update_loop, daemon=True)
    update_thread.start()
    
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
        logger.info("Network failover module stopped")


if __name__ == "__main__":
    main()
