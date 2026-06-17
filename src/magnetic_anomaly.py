#!/usr/bin/env python3
"""
Dog Agent — Magnetic Anomaly / Hazard Detection
===============================================
Uses BNO055 magnetometer to detect unusual magnetic fields and metal hazards.

Features:
  - Baseline magnetic field calibration
  - Anomaly detection for buried cables, metal objects, vehicles
  - Vehicle proximity warning before impact
  - Logging of anomaly events with GPS

Usage:
    python src/magnetic_anomaly.py
    python src/magnetic_anomaly.py --simulate
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
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

# Logging
logger = logging.getLogger("magnetic_anomaly")
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


@dataclass
class MagneticEvent:
    timestamp: datetime
    anomaly_level: str  # low, medium, high, critical
    field_strength_ut: float
    deviation_ut: float
    location: Optional[Dict]
    source_estimate: str


class MagneticAnomalyDetector:
    """Detects magnetic anomalies using BNO055 magnetometer."""
    
    def __init__(self):
        self.enabled = get_cfg("magnetic_anomaly.enabled", False)
        self.imu_address = get_cfg("magnetic_anomaly.imu_address", 0x28)
        self.poll_interval_sec = get_cfg("magnetic_anomaly.poll_interval_sec", 0.2)
        self.calibration_duration_sec = get_cfg("magnetic_anomaly.calibration_duration_sec", 60)
        self.threshold_low_ut = get_cfg("magnetic_anomaly.threshold_low_ut", 20)
        self.threshold_medium_ut = get_cfg("magnetic_anomaly.threshold_medium_ut", 50)
        self.threshold_high_ut = get_cfg("magnetic_anomaly.threshold_high_ut", 100)
        
        self._baseline: Optional[tuple] = None
        self._baseline_history: deque[tuple] = deque(maxlen=500)
        self._events: deque[MagneticEvent] = deque(maxlen=100)
        self._calibration_start: float = 0
        self._lock = threading.Lock()
        self._imu = None
        
        if self.enabled:
            self._init_imu()
            self._calibration_start = time.time()
    
    def _init_imu(self):
        try:
            from bno055 import BNO055
            self._imu = BNO055(self.imu_address)
            logger.info("BNO055 magnetometer initialized for anomaly detection")
        except Exception as e:
            logger.warning(f"Failed to init IMU: {e}")
            self._imu = None
    
    def _read_magnetometer(self) -> Optional[tuple]:
        if not self._imu:
            return None
        try:
            return self._imu.get_magnetometer()
        except Exception as e:
            logger.error(f"Magnetometer read error: {e}")
            return None
    
    def _compute_baseline(self) -> None:
        """Compute baseline from calibration history."""
        if len(self._baseline_history) < 50:
            return
        
        xs = [m[0] for m in self._baseline_history]
        ys = [m[1] for m in self._baseline_history]
        zs = [m[2] for m in self._baseline_history]
        
        with self._lock:
            self._baseline = (sum(xs)/len(xs), sum(ys)/len(ys), sum(zs)/len(zs))
    
    def _classify(self, deviation: float) -> str:
        if deviation >= self.threshold_high_ut:
            return "critical"
        if deviation >= self.threshold_medium_ut:
            return "high"
        if deviation >= self.threshold_low_ut:
            return "medium"
        return "low"
    
    def _estimate_source(self, field: tuple, deviation: float) -> str:
        """Estimate likely source of anomaly."""
        total = math.sqrt(field[0]**2 + field[1]**2 + field[2]**2)
        if deviation > 200:
            return "large_metal_object_or_vehicle"
        if deviation > 100:
            return "power_cable_or_fence"
        if deviation > 50:
            return "buried_utility_or_metal"
        if total > 100:
            return "vehicle_proximity"
        return "background_variation"
    
    def update(self):
        """Process one magnetometer reading."""
        if not self.enabled:
            return
        
        mag = self._read_magnetometer()
        if not mag:
            return
        
        now = time.time()
        
        # Calibration phase
        if self._baseline is None:
            if now - self._calibration_start < self.calibration_duration_sec:
                self._baseline_history.append(mag)
                self._compute_baseline()
                return
            else:
                self._compute_baseline()
                if self._baseline is None:
                    self._baseline = mag  # fallback
                logger.info(f"Magnetic baseline calibrated: {self._baseline}")
        
        bx, by, bz = self._baseline
        dx, dy, dz = mag[0]-bx, mag[1]-by, mag[2]-bz
        deviation = math.sqrt(dx**2 + dy**2 + dz**2)
        total = math.sqrt(mag[0]**2 + mag[1]**2 + mag[2]**2)
        
        level = self._classify(deviation)
        
        if level in ("medium", "high", "critical"):
            location = self._get_location()
            event = MagneticEvent(
                timestamp=datetime.now(timezone.utc),
                anomaly_level=level,
                field_strength_ut=total,
                deviation_ut=deviation,
                location=location,
                source_estimate=self._estimate_source(mag, deviation),
            )
            
            with self._lock:
                self._events.append(event)
            
            logger.warning(f"MAGNETIC ANOMALY {level}: {deviation:.1f}µT ({event.source_estimate})")
            
            if level == "critical":
                self._send_alert(event)
    
    def _get_location(self) -> Optional[Dict]:
        try:
            import requests
            resp = requests.get("http://localhost:9111/gps", timeout=3)
            if resp.status_code == 200:
                return resp.json()
        except:
            pass
        return None
    
    def _send_alert(self, event: MagneticEvent):
        try:
            import requests
            requests.post(
                "http://localhost:9118/alerts/send",
                json={
                    "level": "warning" if event.anomaly_level in ("medium", "high") else "emergency",
                    "message": f"Magnetic anomaly ({event.anomaly_level}): {event.deviation_ut:.1f}µT - {event.source_estimate}",
                    "location": event.location,
                },
                timeout=5
            )
        except Exception as e:
            logger.error(f"Failed to send magnetic alert: {e}")
    
    def get_status(self) -> dict:
        with self._lock:
            recent = [e for e in self._events if (datetime.now(timezone.utc) - e.timestamp).total_seconds() < 3600]
            return {
                "enabled": self.enabled,
                "calibrated": self._baseline is not None,
                "baseline": self._baseline,
                "event_count_1h": len(recent),
                "recent_events": [
                    {
                        "level": e.anomaly_level,
                        "deviation_ut": round(e.deviation_ut, 1),
                        "field_ut": round(e.field_strength_ut, 1),
                        "source": e.source_estimate,
                        "timestamp": e.timestamp.isoformat(),
                    }
                    for e in list(self._events)[-10:]
                ],
            }


class MagneticHTTPHandler(BaseHTTPRequestHandler):
    """HTTP API for magnetic anomaly detector."""
    
    detector: Optional[MagneticAnomalyDetector] = None
    
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
        
        if path == "magnetic/health":
            self._send_json({
                "status": "ok",
                "service": "magnetic_anomaly",
                "enabled": bool(self.detector and self.detector.enabled),
            })
        elif path == "magnetic/status":
            if not self.detector:
                self._send_json({"error": "Detector not initialized"}, 503)
                return
            self._send_json(self.detector.get_status())
        else:
            self._send_json({"error": f"Unknown endpoint: {path}"}, 404)


def main():
    parser = argparse.ArgumentParser(description="Dog Agent — Magnetic Anomaly Detection")
    parser.add_argument("--port", type=int, default=9156, help="HTTP API port")
    parser.add_argument("--simulate", action="store_true", help="Run simulation")
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose logging")
    args = parser.parse_args()
    
    if args.verbose:
        logger.setLevel(logging.DEBUG)
    
    detector = MagneticAnomalyDetector()
    
    if args.simulate:
        logger.info("=== Magnetic Anomaly Simulation ===")
        detector.enabled = True
        detector._baseline = (20.0, 5.0, 45.0)
        events = [
            (25.0, 5.0, 45.0),  # Normal
            (120.0, 5.0, 45.0), # High anomaly
        ]
        for mag in events:
            bx, by, bz = detector._baseline
            dx, dy, dz = mag[0]-bx, mag[1]-by, mag[2]-bz
            deviation = math.sqrt(dx**2 + dy**2 + dz**2)
            logger.info(f"Deviation: {deviation:.1f}µT, level: {detector._classify(deviation)}")
        return
    
    MagneticHTTPHandler.detector = detector
    
    server = HTTPServer(("127.0.0.1", args.port), MagneticHTTPHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    logger.info(f"Magnetic anomaly API on http://127.0.0.1:{args.port}")
    
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
        logger.info("Magnetic anomaly detector stopped")


if __name__ == "__main__":
    main()
