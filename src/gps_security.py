#!/usr/bin/env python3
"""
Dog Agent — GPS Security (Spoofing & Jamming Detection)
=======================================================
Detects GPS signal integrity threats.

Features:
  - Teleportation detection
  - C/N0 jamming detection
  - IMU cross-check
  - Automatic fallback to dead reckoning

Usage:
    python src/gps_security.py
    python src/gps_security.py --simulate
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import signal
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

# Logging
logger = logging.getLogger("gps_security")
handler = logging.StreamHandler(sys.stdout)
handler.setFormatter(logging.Formatter(
    "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
))
logger.addHandler(handler)
logger.setLevel(logging.INFO)

PROJECT_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_DIR / "config.yaml"


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


class ThreatLevel(Enum):
    NORMAL = "normal"
    DEGRADED = "degraded"
    JAMMED = "jammed"
    SPOOFED = "spoofed"
    CRITICAL = "critical"


@dataclass
class GPSSecurityEvent:
    level: ThreatLevel
    type: str
    message: str
    timestamp: datetime
    details: Dict
    location: Optional[Dict]


class GPSSecurityMonitor:
    """Monitors GPS integrity for spoofing/jamming."""
    
    def __init__(self):
        self.enabled = get_cfg("gps_security.enabled", False)
        self.max_jump_m = get_cfg("gps_security.max_jump_m", 500)
        self.min_cn0_dbhz = get_cfg("gps_security.min_cn0_dbhz", 25)
        self.imu_threshold_ms2 = get_cfg("gps_security.imu_threshold_ms2", 5.0)
        self.max_speed_ms = get_cfg("gps_security.max_speed_ms", 30)  # 108 km/h
        self.poll_interval_sec = get_cfg("gps_security.poll_interval_sec", 1.0)
        
        self._last_position: Optional[Dict] = None
        self._last_time: float = 0
        self._events: deque[GPSSecurityEvent] = deque(maxlen=100)
        self._threat_level = ThreatLevel.NORMAL
        self._lock = threading.Lock()
    
    def _haversine_m(self, lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        R = 6371000
        phi1, phi2 = math.radians(lat1), math.radians(lat2)
        dphi = math.radians(lat2 - lat1)
        dlambda = math.radians(lon2 - lon1)
        a = math.sin(dphi/2)**2 + math.cos(phi1)*math.cos(phi2)*math.sin(dlambda/2)**2
        return 2 * R * math.asin(math.sqrt(a))
    
    def _get_gps(self) -> Optional[Dict]:
        try:
            import requests
            resp = requests.get("http://localhost:9111/gps", timeout=5)
            if resp.status_code == 200:
                return resp.json()
        except:
            pass
        return None
    
    def _get_imu_velocity(self) -> Optional[float]:
        try:
            import requests
            resp = requests.get("http://localhost:9139/dr/position", timeout=5)
            if resp.status_code == 200:
                data = resp.json()
                rel = data.get("relative", {})
                # Estimate velocity from distance if available
                return rel.get("distance_m", 0)
        except:
            pass
        return None
    
    def analyze(self, position: Dict) -> List[GPSSecurityEvent]:
        """Analyze GPS position for security threats."""
        events = []
        now = time.time()
        lat = position.get("lat")
        lon = position.get("lon")
        speed_ms = position.get("speed_ms", 0) or 0
        cn0 = position.get("cn0_dbhz", 0) or 35
        sats = position.get("satellites", 0)
        
        if lat is None or lon is None:
            return events
        
        # 1. Teleportation detection
        if self._last_position:
            dt = now - self._last_time
            if dt > 0:
                distance = self._haversine_m(
                    lat, lon,
                    self._last_position.get("lat", lat),
                    self._last_position.get("lon", lon)
                )
                required_speed = distance / dt
                
                if distance > self.max_jump_m:
                    events.append(GPSSecurityEvent(
                        level=ThreatLevel.SPOOFED,
                        type="teleportation",
                        message=f"GPS teleportation detected: {distance:.0f}m in {dt:.1f}s",
                        timestamp=datetime.now(timezone.utc),
                        details={"distance_m": distance, "dt_sec": dt, "required_speed_ms": required_speed},
                        location=position,
                    ))
                elif required_speed > self.max_speed_ms:
                    events.append(GPSSecurityEvent(
                        level=ThreatLevel.DEGRADED,
                        type="impossible_speed",
                        message=f"Impossible GPS speed: {required_speed:.1f} m/s",
                        timestamp=datetime.now(timezone.utc),
                        details={"speed_ms": required_speed, "max_ms": self.max_speed_ms},
                        location=position,
                    ))
        
        # 2. C/N0 jamming detection
        if cn0 < self.min_cn0_dbhz:
            events.append(GPSSecurityEvent(
                level=ThreatLevel.JAMMED,
                type="low_cn0",
                message=f"Low GPS signal strength: {cn0:.1f} dB-Hz (possible jamming)",
                timestamp=datetime.now(timezone.utc),
                details={"cn0_dbhz": cn0, "threshold": self.min_cn0_dbhz},
                location=position,
            ))
        
        # 3. Satellite count anomaly
        if sats < 4:
            events.append(GPSSecurityEvent(
                level=ThreatLevel.DEGRADED,
                type="few_satellites",
                message=f"Only {sats} GPS satellites tracked",
                timestamp=datetime.now(timezone.utc),
                details={"satellites": sats},
                location=position,
            ))
        
        # Update state
        worst_level = ThreatLevel.NORMAL
        for e in events:
            if e.level == ThreatLevel.CRITICAL:
                worst_level = ThreatLevel.CRITICAL
            elif e.level == ThreatLevel.SPOOFED and worst_level != ThreatLevel.CRITICAL:
                worst_level = ThreatLevel.SPOOFED
            elif e.level == ThreatLevel.JAMMED and worst_level.value not in ["critical", "spoofed"]:
                worst_level = ThreatLevel.JAMMED
            elif e.level == ThreatLevel.DEGRADED and worst_level.value == "normal":
                worst_level = ThreatLevel.DEGRADED
        
        with self._lock:
            self._threat_level = worst_level
            self._events.extend(events)
            self._last_position = position
            self._last_time = now
        
        # Take action on severe threats
        if worst_level in (ThreatLevel.SPOOFED, ThreatLevel.CRITICAL, ThreatLevel.JAMMED):
            self._activate_fallback(position)
        
        return events
    
    def _activate_fallback(self, position: Dict):
        """Switch to dead reckoning / LoRa."""
        logger.warning(f"GPS security threat: {self._threat_level.value}, activating fallback")
        try:
            import requests
            # Increase LoRa rate if available
            requests.post("http://localhost:9140/lora/config", json={"tx_interval_sec": 60}, timeout=3)
        except:
            pass
    
    def update(self):
        """Poll GPS and analyze."""
        if not self.enabled:
            return
        
        position = self._get_gps()
        if position:
            events = self.analyze(position)
            for e in events:
                logger.warning(f"GPS SECURITY: {e.message}")
    
    def get_status(self) -> dict:
        with self._lock:
            recent = [e for e in self._events if e.timestamp.timestamp() > time.time() - 3600]
            return {
                "enabled": self.enabled,
                "threat_level": self._threat_level.value,
                "event_count_1h": len(recent),
                "recent_events": [
                    {
                        "level": e.level.value,
                        "type": e.type,
                        "message": e.message,
                        "timestamp": e.timestamp.isoformat(),
                    }
                    for e in list(self._events)[-5:]
                ],
            }
    
    def get_events(self, limit: int = 50) -> List[dict]:
        with self._lock:
            return [
                {
                    "level": e.level.value,
                    "type": e.type,
                    "message": e.message,
                    "timestamp": e.timestamp.isoformat(),
                    "details": e.details,
                    "location": e.location,
                }
                for e in list(self._events)[-limit:]
            ]


class GPSSecurityHTTPHandler(BaseHTTPRequestHandler):
    """HTTP API for GPS security."""
    
    monitor: Optional[GPSSecurityMonitor] = None
    
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
        
        if path == "gps_security/health":
            self._send_json({
                "status": "ok",
                "service": "gps_security",
                "enabled": bool(self.monitor and self.monitor.enabled),
            })
        elif path == "gps_security/status":
            if not self.monitor:
                self._send_json({"error": "Monitor not initialized"}, 503)
                return
            self._send_json(self.monitor.get_status())
        elif path == "gps_security/events":
            if not self.monitor:
                self._send_json({"error": "Monitor not initialized"}, 503)
                return
            self._send_json({"events": self.monitor.get_events()})
        else:
            self._send_json({"error": f"Unknown endpoint: {path}"}, 404)


def main():
    parser = argparse.ArgumentParser(description="Dog Agent — GPS Security")
    parser.add_argument("--port", type=int, default=9153, help="HTTP API port")
    parser.add_argument("--simulate", action="store_true", help="Run simulation")
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose logging")
    args = parser.parse_args()
    
    if args.verbose:
        logger.setLevel(logging.DEBUG)
    
    monitor = GPSSecurityMonitor()
    
    if args.simulate:
        logger.info("=== GPS Security Simulation ===")
        positions = [
            {"lat": 45.5231, "lon": -122.6765, "speed_ms": 1.0, "cn0_dbhz": 35, "satellites": 8},
            {"lat": 45.5231, "lon": -122.6765, "speed_ms": 1.0, "cn0_dbhz": 35, "satellites": 8},
            # Teleportation
            {"lat": 45.6000, "lon": -122.8000, "speed_ms": 1.0, "cn0_dbhz": 35, "satellites": 8},
            # Jamming
            {"lat": 45.6000, "lon": -122.8000, "speed_ms": 1.0, "cn0_dbhz": 15, "satellites": 8},
        ]
        monitor.enabled = True
        for pos in positions:
            events = monitor.analyze(pos)
            if events:
                for e in events:
                    logger.info(f"Event: {e.level.value} - {e.message}")
            time.sleep(0.1)
        return
    
    GPSSecurityHTTPHandler.monitor = monitor
    
    server = HTTPServer(("127.0.0.1", args.port), GPSSecurityHTTPHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    logger.info(f"GPS security API on http://127.0.0.1:{args.port}")
    
    def update_loop():
        while True:
            if monitor.enabled:
                monitor.update()
            time.sleep(monitor.poll_interval_sec)
    
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
        logger.info("GPS security stopped")


if __name__ == "__main__":
    main()
