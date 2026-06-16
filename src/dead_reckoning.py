#!/usr/bin/env python3
"""
Dog Agent — Dead Reckoning (Indoor Positioning)
================================================
IMU-based step counting for GPS-denied environments.

Uses BNO055 accelerometer + gyroscope to estimate movement
when GPS is unavailable (indoors, dense forest, urban canyons).

Usage:
    python src/dead_reckoning.py
    python src/dead_reckoning.py --simulate
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
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

# Logging
logger = logging.getLogger("dead_reckoning")
handler = logging.StreamHandler(sys.stdout)
handler.setFormatter(logging.Formatter(
    "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
))
logger.addHandler(handler)
logger.setLevel(logging.INFO)

PROJECT_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_DIR / "config.yaml"

# Constants
STEP_THRESHOLD_G = 1.2  # Minimum acceleration for step detection
STEP_COOLDOWN_MS = 300  # Minimum time between steps
DOG_STRIDE_LENGTH_M = 0.6  # Average stride length
MAX_DRIFT_M = 100  # Maximum accumulated drift before warning


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
class DeadReckoningPosition:
    """Estimated position from dead reckoning."""
    x_m: float  # East from start
    y_m: float  # North from start
    heading_deg: float  # 0 = North, 90 = East
    step_count: int
    total_distance_m: float
    estimated_accuracy_m: float
    timestamp: datetime
    
    def to_dict(self) -> dict:
        return {
            "x_m": round(self.x_m, 2),
            "y_m": round(self.y_m, 2),
            "heading_deg": round(self.heading_deg, 1),
            "step_count": self.step_count,
            "total_distance_m": round(self.total_distance_m, 2),
            "estimated_accuracy_m": round(self.estimated_accuracy_m, 2),
            "timestamp": self.timestamp.isoformat(),
        }


class StepDetector:
    """Detects steps from accelerometer data."""
    
    def __init__(self):
        self.last_step_time = 0
        self.step_count = 0
        self.accel_history: List[Tuple[float, float, float, float]] = []  # x, y, z, time
        self._lock = threading.Lock()
    
    def process_acceleration(self, x: float, y: float, z: float) -> bool:
        """Process accelerometer reading, return True if step detected."""
        current_time = time.time() * 1000  # ms
        magnitude = math.sqrt(x*x + y*y + z*z)
        
        with self._lock:
            self.accel_history.append((x, y, z, current_time))
            
            # Keep last 100ms
            cutoff = current_time - 100
            self.accel_history = [h for h in self.accel_history if h[3] > cutoff]
            
            # Check cooldown
            if current_time - self.last_step_time < STEP_COOLDOWN_MS:
                return False
            
            # Peak detection
            if len(self.accel_history) >= 3:
                prev_mag = math.sqrt(
                    self.accel_history[-2][0]**2 +
                    self.accel_history[-2][1]**2 +
                    self.accel_history[-2][2]**2
                )
                
                if magnitude > STEP_THRESHOLD_G and prev_mag < magnitude:
                    self.step_count += 1
                    self.last_step_time = current_time
                    return True
        
        return False
    
    def get_step_count(self) -> int:
        with self._lock:
            return self.step_count
    
    def reset(self):
        with self._lock:
            self.step_count = 0
            self.accel_history = []


class OrientationTracker:
    """Tracks heading from gyroscope/compass."""
    
    def __init__(self):
        self.heading_deg = 0.0
        self._lock = threading.Lock()
    
    def update_from_imu(self, heading: float):
        """Update heading from IMU (0-360 degrees)."""
        with self._lock:
            self.heading_deg = heading % 360
    
    def get_heading(self) -> float:
        with self._lock:
            return self.heading_deg


class DeadReckoningEngine:
    """Dead reckoning position estimation."""
    
    def __init__(self):
        self.enabled = get_cfg("dead_reckoning.enabled", False)
        self.stride_length_m = get_cfg("dead_reckoning.stride_length_m", DOG_STRIDE_LENGTH_M)
        self.i2c_address = get_cfg("dead_reckoning.bno055_address", 0x28)
        self.calibration_required = get_cfg("dead_reckoning.calibration_required", True)
        
        self.step_detector = StepDetector()
        self.orientation = OrientationTracker()
        
        self._position = DeadReckoningPosition(
            x_m=0, y_m=0, heading_deg=0, step_count=0,
            total_distance_m=0, estimated_accuracy_m=5.0,
            timestamp=datetime.now(timezone.utc),
        )
        self._last_gps_fix: Optional[Dict] = None
        self._lock = threading.Lock()
        self._running = False
        
        if self.enabled:
            self._init_imu()
    
    def _init_imu(self):
        try:
            from bno055 import BNO055
            self._imu = BNO055(self.i2c_address)
            logger.info(f"BNO055 IMU initialized at 0x{self.i2c_address:02X}")
        except ImportError:
            logger.warning("BNO055 library not available")
            self._imu = None
        except Exception as e:
            logger.warning(f"Failed to initialize IMU: {e}")
            self._imu = None
    
    def calibrate_to_gps(self, gps_fix: Dict):
        """Calibrate dead reckoning to known GPS position."""
        with self._lock:
            self._last_gps_fix = gps_fix
            self._position = DeadReckoningPosition(
                x_m=0, y_m=0,
                heading_deg=gps_fix.get("heading", 0),
                step_count=0,
                total_distance_m=0,
                estimated_accuracy_m=5.0,
                timestamp=datetime.now(timezone.utc),
            )
            self.step_detector.reset()
        logger.info("Dead reckoning calibrated to GPS fix")
    
    def update(self):
        """Update position estimate from IMU data."""
        if not self.enabled:
            return
        
        try:
            if self._imu:
                accel = self._imu.get_acceleration()
                heading = self._imu.get_heading()
                
                # Update orientation
                self.orientation.update_from_imu(heading)
                
                # Detect steps
                if self.step_detector.process_acceleration(*accel):
                    self._update_position_from_step()
        except Exception as e:
            logger.error(f"IMU read error: {e}")
    
    def _update_position_from_step(self):
        """Update position when step detected."""
        heading_rad = math.radians(self.orientation.get_heading())
        dx = self.stride_length_m * math.sin(heading_rad)
        dy = self.stride_length_m * math.cos(heading_rad)
        
        with self._lock:
            self._position.x_m += dx
            self._position.y_m += dy
            self._position.step_count = self.step_detector.get_step_count()
            self._position.total_distance_m += self.stride_length_m
            self._position.heading_deg = self.orientation.get_heading()
            self._position.timestamp = datetime.now(timezone.utc)
            
            # Increase uncertainty with distance
            self._position.estimated_accuracy_m = min(
                5.0 + self._position.total_distance_m * 0.1,
                MAX_DRIFT_M
            )
    
    def get_position(self) -> DeadReckoningPosition:
        with self._lock:
            return self._position
    
    def get_absolute_position(self) -> Optional[Dict]:
        """Get absolute lat/lon if calibrated."""
        if not self._last_gps_fix:
            return None
        
        # Simple projection (not accurate over large distances)
        # 1 degree lat ≈ 111km, varies by longitude
        lat_offset = self._position.y_m / 111000
        lon_offset = self._position.x_m / (111000 * math.cos(math.radians(self._last_gps_fix.get("lat", 0))))
        
        return {
            "lat": self._last_gps_fix.get("lat", 0) + lat_offset,
            "lon": self._last_gps_fix.get("lon", 0) + lon_offset,
            "accuracy_m": self._position.estimated_accuracy_m,
        }


class DeadReckoningHTTPHandler(BaseHTTPRequestHandler):
    """HTTP API for dead reckoning."""
    
    engine: Optional[DeadReckoningEngine] = None
    
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
        
        if path == "dr/health":
            self._send_json({
                "status": "ok",
                "service": "dead_reckoning",
                "enabled": bool(self.engine and self.engine.enabled),
            })
        elif path == "dr/position":
            if not self.engine:
                self._send_json({"error": "Engine not initialized"}, 503)
                return
            pos = self.engine.get_position()
            abs_pos = self.engine.get_absolute_position()
            self._send_json({
                "relative": pos.to_dict(),
                "absolute": abs_pos,
            })
        elif path == "dr/stats":
            if not self.engine:
                self._send_json({"error": "Engine not initialized"}, 503)
                return
            pos = self.engine.get_position()
            self._send_json({
                "steps": pos.step_count,
                "distance_m": pos.total_distance_m,
                "accuracy_m": pos.estimated_accuracy_m,
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
        
        if path == "dr/calibrate":
            if not self.engine:
                self._send_json({"error": "Engine not initialized"}, 503)
                return
            self.engine.calibrate_to_gps(data)
            self._send_json({"status": "calibrated"})
        else:
            self._send_json({"error": f"Unknown endpoint: {path}"}, 404)


def main():
    parser = argparse.ArgumentParser(description="Dog Agent — Dead Reckoning")
    parser.add_argument("--port", type=int, default=9139, help="HTTP API port")
    parser.add_argument("--simulate", action="store_true", help="Run simulation")
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose logging")
    args = parser.parse_args()
    
    if args.verbose:
        logger.setLevel(logging.DEBUG)
    
    engine = DeadReckoningEngine()
    
    if args.simulate:
        logger.info("=== Dead Reckoning Simulation ===")
        for i in range(10):
            engine.step_detector.process_acceleration(0, 1.5, 0)
            time.sleep(0.3)
        pos = engine.get_position()
        logger.info(f"Steps: {pos.step_count}, Distance: {pos.total_distance_m:.1f}m")
        return
    
    DeadReckoningHTTPHandler.engine = engine
    
    server = HTTPServer(("127.0.0.1", args.port), DeadReckoningHTTPHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    logger.info(f"Dead reckoning API on http://127.0.0.1:{args.port}")
    
    # Update loop
    def update_loop():
        while True:
            if engine.enabled:
                engine.update()
            time.sleep(0.05)  # 20Hz
    
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
        logger.info("Dead reckoning stopped")


if __name__ == "__main__":
    main()
