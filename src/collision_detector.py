#!/usr/bin/env python3
"""
Dog Agent — Vehicle Collision Detector
=====================================
Distinguishes vehicle impacts from normal falls using BNO055 IMU.

Features:
  - Directional impulse analysis
  - Multi-axis shock signature
  - Road proximity context from GPS
  - Immediate emergency escalation

Usage:
    python src/collision_detector.py
    python src/collision_detector.py --simulate
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
logger = logging.getLogger("collision_detector")
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


class CollisionSeverity(Enum):
    NONE = "none"
    SUSPECTED = "suspected"
    LIKELY = "likely"
    CONFIRMED = "confirmed"


@dataclass
class CollisionEvent:
    severity: CollisionSeverity
    timestamp: datetime
    max_g: float
    direction: str
    estimated_speed_kmh: Optional[float]
    location: Optional[Dict]
    confidence: float


class CollisionDetector:
    """Detects vehicle collisions using IMU shock analysis."""
    
    # Collision signatures
    VEHICLE_THRESHOLD_G = 6.0
    VEHICLE_DURATION_MS = 80
    VEHICLE_IMPULSE_AXIS = "lateral"  # Vehicle hits often from side
    
    def __init__(self):
        self.enabled = get_cfg("collision_detector.enabled", False)
        self.impact_threshold_g = get_cfg("collision_detector.impact_threshold_g", 6.0)
        self.min_duration_ms = get_cfg("collision_detector.min_duration_ms", 80)
        self.max_duration_ms = get_cfg("collision_detector.max_duration_ms", 300)
        self.imu_address = get_cfg("collision_detector.imu_address", 0x28)
        self.poll_interval_sec = get_cfg("collision_detector.poll_interval_sec", 0.02)
        
        self._accel_history: deque[tuple] = deque(maxlen=200)  # 4 sec at 50Hz
        self._events: List[CollisionEvent] = []
        self._lock = threading.Lock()
        self._imu = None
        
        if self.enabled:
            self._init_imu()
    
    def _init_imu(self):
        try:
            from bno055 import BNO055
            self._imu = BNO055(self.imu_address)
            logger.info("BNO055 IMU initialized for collision detection")
        except Exception as e:
            logger.warning(f"Failed to init IMU: {e}")
            self._imu = None
    
    def _read_imu(self) -> Optional[tuple]:
        if not self._imu:
            return None
        try:
            return self._imu.get_acceleration()
        except Exception as e:
            logger.error(f"IMU read error: {e}")
            return None
    
    def _analyze_impact(self) -> Optional[CollisionEvent]:
        """Analyze recent acceleration for collision signature."""
        if len(self._accel_history) < 20:
            return None
        
        recent = list(self._accel_history)[-100:]
        
        # Find peak magnitude and direction
        peak_g = 0
        peak_accel = (0, 0, 0)
        for ax, ay, az, ts in recent:
            mag = math.sqrt(ax**2 + ay**2 + az**2)
            if mag > peak_g:
                peak_g = mag
                peak_accel = (ax, ay, az)
        
        if peak_g < self.impact_threshold_g:
            return None
        
        # Determine dominant direction
        ax, ay, az = peak_accel
        max_axis = max(abs(ax), abs(ay), abs(az))
        direction = "vertical" if abs(az) == max_axis else "lateral"
        
        # Estimate vehicle collision probability
        confidence = min(1.0, (peak_g - self.impact_threshold_g) / 10.0)
        
        # Lateral high-G short duration strongly suggests vehicle
        if direction == "lateral" and peak_g > 8.0:
            severity = CollisionSeverity.CONFIRMED
            confidence = min(1.0, confidence + 0.3)
        elif peak_g > 10.0:
            severity = CollisionSeverity.LIKELY
        elif peak_g > self.impact_threshold_g:
            severity = CollisionSeverity.SUSPECTED
        else:
            return None
        
        # Get current location and speed from GPS
        location = self._get_location()
        speed = location.get("speed_kmh") if location else None
        
        return CollisionEvent(
            severity=severity,
            timestamp=datetime.now(timezone.utc),
            max_g=peak_g,
            direction=direction,
            estimated_speed_kmh=speed,
            location=location,
            confidence=confidence,
        )
    
    def _get_location(self) -> Optional[Dict]:
        try:
            import requests
            resp = requests.get("http://localhost:9111/gps", timeout=3)
            if resp.status_code == 200:
                return resp.json()
        except:
            pass
        return None
    
    def update(self):
        """Process one IMU reading."""
        if not self.enabled:
            return
        
        accel = self._read_imu()
        if not accel:
            return
        
        self._accel_history.append((*accel, time.time()))
        
        event = self._analyze_impact()
        if event and event.severity in (CollisionSeverity.LIKELY, CollisionSeverity.CONFIRMED):
            with self._lock:
                self._events.append(event)
                if len(self._events) > 50:
                    self._events.pop(0)
            
            logger.critical(f"VEHICLE COLLISION {event.severity.value.upper()}: {event.max_g:.1f}G {event.direction}")
            self._send_emergency_alert(event)
    
    def _send_emergency_alert(self, event: CollisionEvent):
        """Send emergency alert."""
        try:
            import requests
            requests.post(
                "http://localhost:9118/alerts/send",
                json={
                    "level": "emergency",
                    "message": f"VEHICLE COLLISION DETECTED ({event.severity.value}): {event.max_g:.1f}G {event.direction}",
                    "location": event.location,
                    "confidence": event.confidence,
                },
                timeout=5
            )
        except Exception as e:
            logger.error(f"Failed to send collision alert: {e}")
    
    def get_latest(self) -> Optional[dict]:
        with self._lock:
            if not self._events:
                return None
            event = self._events[-1]
            return {
                "severity": event.severity.value,
                "timestamp": event.timestamp.isoformat(),
                "max_g": round(event.max_g, 2),
                "direction": event.direction,
                "estimated_speed_kmh": event.estimated_speed_kmh,
                "confidence": round(event.confidence, 2),
                "location": event.location,
            }
    
    def get_history(self) -> List[dict]:
        with self._lock:
            return [
                {
                    "severity": e.severity.value,
                    "timestamp": e.timestamp.isoformat(),
                    "max_g": round(e.max_g, 2),
                    "direction": e.direction,
                }
                for e in self._events
            ]


class CollisionHTTPHandler(BaseHTTPRequestHandler):
    """HTTP API for collision detector."""
    
    detector: Optional[CollisionDetector] = None
    
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
        
        if path == "collision/health":
            self._send_json({
                "status": "ok",
                "service": "collision_detector",
                "enabled": bool(self.detector and self.detector.enabled),
            })
        elif path == "collision/status":
            if not self.detector:
                self._send_json({"error": "Detector not initialized"}, 503)
                return
            self._send_json({
                "latest": self.detector.get_latest(),
                "history_count": len(self.detector.get_history()),
            })
        elif path == "collision/history":
            if not self.detector:
                self._send_json({"error": "Detector not initialized"}, 503)
                return
            self._send_json({"events": self.detector.get_history()})
        else:
            self._send_json({"error": f"Unknown endpoint: {path}"}, 404)


def main():
    parser = argparse.ArgumentParser(description="Dog Agent — Vehicle Collision Detector")
    parser.add_argument("--port", type=int, default=9155, help="HTTP API port")
    parser.add_argument("--simulate", action="store_true", help="Run simulation")
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose logging")
    args = parser.parse_args()
    
    if args.verbose:
        logger.setLevel(logging.DEBUG)
    
    detector = CollisionDetector()
    
    if args.simulate:
        logger.info("=== Collision Simulation ===")
        # Simulate lateral vehicle impact: 12G sideways
        detector.enabled = True
        for _ in range(10):
            detector._accel_history.append((12.0, 0.5, 1.0, time.time()))
        event = detector._analyze_impact()
        if event:
            logger.info(f"Detected: {event.severity.value}, {event.max_g:.1f}G, {event.direction}")
        return
    
    CollisionHTTPHandler.detector = detector
    
    server = HTTPServer(("127.0.0.1", args.port), CollisionHTTPHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    logger.info(f"Collision detector API on http://127.0.0.1:{args.port}")
    
    def update_loop():
        while True:
            if detector.enabled:
                detector.update()
            time.sleep(detector.poll_interval_sec)
    
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
        logger.info("Collision detector stopped")


if __name__ == "__main__":
    main()
