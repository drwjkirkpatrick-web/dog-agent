#!/usr/bin/env python3
"""
Dog Agent — Sensor Fusion Engine
=================================
Kalman-filter-based fusion of GPS, IMU, dead reckoning, and speed estimates.

Features:
  - Fuses GPS, IMU, and dead reckoning
  - Confidence-weighted position estimate
  - Smooths GPS noise and fills signal gaps

Usage:
    python src/sensor_fusion.py
    python src/sensor_fusion.py --simulate
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
from typing import Any, Dict, List, Optional

import numpy as np
import yaml

# Logging
logger = logging.getLogger("sensor_fusion")
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


# GPS helpers
METERS_PER_DEG_LAT = 111320.0


def meters_to_deg_lat(m: float) -> float:
    return m / METERS_PER_DEG_LAT


def meters_to_deg_lon(m: float, lat: float) -> float:
    return m / (METERS_PER_DEG_LAT * math.cos(math.radians(lat)))


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi/2)**2 + math.cos(phi1)*math.cos(phi2)*math.sin(dlambda/2)**2
    return 2 * R * math.asin(math.sqrt(a))


@dataclass
class FusedPose:
    """Fused position estimate output."""
    lat: float
    lon: float
    velocity_north_ms: float
    velocity_east_ms: float
    heading_deg: float
    speed_ms: float
    accuracy_m: float
    timestamp: datetime
    source_weights: Dict[str, float]
    covariance_trace: float
    
    def to_dict(self) -> dict:
        return {
            "lat": round(self.lat, 7),
            "lon": round(self.lon, 7),
            "velocity_north_ms": round(self.velocity_north_ms, 3),
            "velocity_east_ms": round(self.velocity_east_ms, 3),
            "heading_deg": round(self.heading_deg, 1),
            "speed_ms": round(self.speed_ms, 3),
            "accuracy_m": round(self.accuracy_m, 2),
            "timestamp": self.timestamp.isoformat(),
            "source_weights": self.source_weights,
            "covariance_trace": round(self.covariance_trace, 6),
        }


class KalmanFilter:
    """Kalman filter for 2D position + velocity."""
    
    def __init__(self, process_noise: float = 0.01, measurement_noise_pos: float = 5.0,
                 measurement_noise_vel: float = 1.0):
        # State: [lat, lon, velocity_north_ms, velocity_east_ms]
        self.x = np.zeros(4)
        self.P = np.eye(4) * 100.0
        
        self.Q = np.eye(4)
        self.Q[0, 0] = process_noise
        self.Q[1, 1] = process_noise
        self.Q[2, 2] = process_noise * 10
        self.Q[3, 3] = process_noise * 10
        
        self.R_pos = np.eye(2) * measurement_noise_pos
        self.R_vel = np.eye(2) * measurement_noise_vel
        
        # State transition matrix (lat/lon are affected by velocity)
        self.F = np.eye(4)
        
        # Measurement matrices
        self.H_pos = np.zeros((2, 4))
        self.H_pos[0, 0] = 1
        self.H_pos[1, 1] = 1
        
        self.H_vel = np.zeros((2, 4))
        self.H_vel[0, 2] = 1
        self.H_vel[1, 3] = 1
        
        self.initialized = False
    
    def set_initial_state(self, lat: float, lon: float, vn: float = 0, ve: float = 0):
        self.x[0] = lat
        self.x[1] = lon
        self.x[2] = vn
        self.x[3] = ve
        self.initialized = True
    
    def predict(self, dt: float):
        """Prediction step."""
        # Update lat/lon based on velocity over dt
        self.F[0, 2] = meters_to_deg_lat(self.x[2] * dt)
        self.F[1, 3] = meters_to_deg_lon(self.x[3] * dt, self.x[0])
        
        self.x = self.F @ self.x
        self.P = self.F @ self.P @ self.F.T + self.Q
    
    def update_position(self, lat: float, lon: float, accuracy_m: float):
        """GPS position update."""
        if not self.initialized:
            self.set_initial_state(lat, lon)
            return
        
        R = np.eye(2) * max(accuracy_m, 1.0)
        z = np.array([lat, lon])
        y = z - self.H_pos @ self.x
        S = self.H_pos @ self.P @ self.H_pos.T + R
        K = self.P @ self.H_pos.T @ np.linalg.inv(S)
        self.x = self.x + K @ y
        self.P = (np.eye(4) - K @ self.H_pos) @ self.P
    
    def update_velocity(self, vn: float, ve: float, accuracy: float = 0.5):
        """Velocity update from IMU/speed."""
        if not self.initialized:
            return
        
        R = np.eye(2) * accuracy
        z = np.array([vn, ve])
        y = z - self.H_vel @ self.x
        S = self.H_vel @ self.P @ self.H_vel.T + R
        K = self.P @ self.H_vel.T @ np.linalg.inv(S)
        self.x = self.x + K @ y
        self.P = (np.eye(4) - K @ self.H_vel) @ self.P
    
    @property
    def lat(self) -> float:
        return float(self.x[0])
    
    @property
    def lon(self) -> float:
        return float(self.x[1])
    
    @property
    def velocity_north(self) -> float:
        return float(self.x[2])
    
    @property
    def velocity_east(self) -> float:
        return float(self.x[3])


class SensorFusionEngine:
    """Fuses multiple positioning sources."""
    
    def __init__(self):
        self.enabled = get_cfg("sensor_fusion.enabled", False)
        self.update_rate_hz = get_cfg("sensor_fusion.update_rate_hz", 10)
        self.gps_weight = get_cfg("sensor_fusion.gps_weight", 0.6)
        self.imu_weight = get_cfg("sensor_fusion.imu_weight", 0.3)
        self.dr_weight = get_cfg("sensor_fusion.dead_reckoning_weight", 0.1)
        self.process_noise = get_cfg("sensor_fusion.process_noise", 0.01)
        self.measurement_noise_pos = get_cfg("sensor_fusion.measurement_noise_pos", 5.0)
        self.measurement_noise_vel = get_cfg("sensor_fusion.measurement_noise_vel", 1.0)
        
        self._kf = KalmanFilter(
            process_noise=self.process_noise,
            measurement_noise_pos=self.measurement_noise_pos,
            measurement_noise_vel=self.measurement_noise_vel,
        )
        
        self._latest_pose: Optional[FusedPose] = None
        self._pose_history: List[FusedPose] = []
        self._last_update: float = 0
        self._lock = threading.Lock()
    
    def _get_gps(self) -> Optional[Dict]:
        try:
            import requests
            resp = requests.get("http://localhost:9111/gps", timeout=5)
            if resp.status_code == 200:
                return resp.json()
        except:
            pass
        return None
    
    def _get_imu(self) -> Optional[Dict]:
        try:
            import requests
            resp = requests.get("http://localhost:9122/environmental/imu", timeout=5)
            if resp.status_code == 200:
                return resp.json()
        except:
            pass
        return None
    
    def _get_dead_reckoning(self) -> Optional[Dict]:
        try:
            import requests
            resp = requests.get("http://localhost:9139/dr/position", timeout=5)
            if resp.status_code == 200:
                return resp.json()
        except:
            pass
        return None
    
    def update(self):
        """One fusion cycle."""
        if not self.enabled:
            return
        
        now = time.time()
        dt = now - self._last_update if self._last_update > 0 else 0.1
        self._last_update = now
        
        # Always predict using current velocity estimate
        self._kf.predict(dt)
        
        # Get sensor data
        gps = self._get_gps()
        imu = self._get_imu()
        dr = self._get_dead_reckoning()
        
        source_weights = {"gps": 0.0, "imu": 0.0, "dead_reckoning": 0.0}
        
        # GPS update
        if gps and gps.get("valid") and self.gps_weight > 0:
            lat = gps.get("lat")
            lon = gps.get("lon")
            if lat is not None and lon is not None:
                accuracy = gps.get("hdop", 5.0)
                self._kf.update_position(lat, lon, accuracy)
                source_weights["gps"] = self.gps_weight
        
        # IMU velocity update
        if imu and self.imu_weight > 0:
            # Convert accel/heading to north/east velocity (simple integration)
            ax = imu.get("accel_x", 0)
            ay = imu.get("accel_y", 0)
            # Assume accel gives velocity change over dt
            vn = ax * dt
            ve = ay * dt
            self._kf.update_velocity(vn, ve, accuracy=1.0)
            source_weights["imu"] = self.imu_weight
        
        # Dead reckoning update
        if dr and self.dr_weight > 0:
            rel = dr.get("relative", {})
            if rel.get("distance_m") is not None:
                heading = math.radians(rel.get("heading_deg", 0))
                speed = rel.get("distance_m", 0) / max(dt, 0.1)
                vn = speed * math.cos(heading)
                ve = speed * math.sin(heading)
                # Use as weak velocity update
                self._kf.update_velocity(vn, ve, accuracy=2.0)
                source_weights["dead_reckoning"] = self.dr_weight
        
        # Normalize weights
        total = sum(source_weights.values())
        if total > 0:
            source_weights = {k: round(v/total, 2) for k, v in source_weights.items()}
        
        # Heading from velocity vector
        heading = math.degrees(math.atan2(self._kf.velocity_east, self._kf.velocity_north))
        if heading < 0:
            heading += 360
        
        speed = math.sqrt(self._kf.velocity_north**2 + self._kf.velocity_east**2)
        accuracy = math.sqrt(self._kf.P[0,0]**2 + self._kf.P[1,1]**2) * METERS_PER_DEG_LAT
        
        pose = FusedPose(
            lat=self._kf.lat,
            lon=self._kf.lon,
            velocity_north_ms=self._kf.velocity_north,
            velocity_east_ms=self._kf.velocity_east,
            heading_deg=heading,
            speed_ms=speed,
            accuracy_m=accuracy,
            timestamp=datetime.now(timezone.utc),
            source_weights=source_weights,
            covariance_trace=float(np.trace(self._kf.P)),
        )
        
        with self._lock:
            self._latest_pose = pose
            self._pose_history.append(pose)
            if len(self._pose_history) > 1000:
                self._pose_history.pop(0)
    
    def get_pose(self) -> Optional[FusedPose]:
        with self._lock:
            return self._latest_pose
    
    def get_confidence(self) -> dict:
        pose = self.get_pose()
        return {
            "initialized": self._kf.initialized,
            "latest": pose.to_dict() if pose else None,
            "covariance": self._kf.P.tolist() if self._kf.initialized else None,
        }
    
    def get_status(self) -> dict:
        pose = self.get_pose()
        return {
            "enabled": self.enabled,
            "initialized": self._kf.initialized,
            "update_rate_hz": self.update_rate_hz,
            "latest": pose.to_dict() if pose else None,
            "history_count": len(self._pose_history),
        }


class FusionHTTPHandler(BaseHTTPRequestHandler):
    """HTTP API for sensor fusion engine."""
    
    engine: Optional[SensorFusionEngine] = None
    
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
        
        if path == "fusion/health":
            self._send_json({
                "status": "ok",
                "service": "sensor_fusion",
                "enabled": bool(self.engine and self.engine.enabled),
            })
        elif path == "fusion/status":
            if not self.engine:
                self._send_json({"error": "Engine not initialized"}, 503)
                return
            self._send_json(self.engine.get_status())
        elif path == "fusion/position":
            if not self.engine:
                self._send_json({"error": "Engine not initialized"}, 503)
                return
            pose = self.engine.get_pose()
            if pose:
                self._send_json(pose.to_dict())
            else:
                self._send_json({"error": "No fused position available"}, 503)
        elif path == "fusion/confidence":
            if not self.engine:
                self._send_json({"error": "Engine not initialized"}, 503)
                return
            self._send_json(self.engine.get_confidence())
        else:
            self._send_json({"error": f"Unknown endpoint: {path}"}, 404)


def main():
    parser = argparse.ArgumentParser(description="Dog Agent — Sensor Fusion Engine")
    parser.add_argument("--port", type=int, default=9148, help="HTTP API port")
    parser.add_argument("--simulate", action="store_true", help="Run simulation")
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose logging")
    args = parser.parse_args()
    
    if args.verbose:
        logger.setLevel(logging.DEBUG)
    
    engine = SensorFusionEngine()
    
    if args.simulate:
        logger.info("=== Sensor Fusion Simulation ===")
        engine.enabled = True
        # Simulate GPS update
        engine._kf.set_initial_state(45.5231, -122.6765, 0.5, 0.2)
        for _ in range(5):
            engine.update()
            pose = engine.get_pose()
            if pose:
                logger.info(f"Fused: {pose.lat:.6f}, {pose.lon:.6f}, speed={pose.speed_ms:.2f}m/s, weights={pose.source_weights}")
            time.sleep(0.1)
        return
    
    FusionHTTPHandler.engine = engine
    
    server = HTTPServer(("127.0.0.1", args.port), FusionHTTPHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    logger.info(f"Sensor fusion API on http://127.0.0.1:{args.port}")
    
    def update_loop():
        interval = 1.0 / engine.update_rate_hz if engine.update_rate_hz > 0 else 0.1
        while True:
            if engine.enabled:
                engine.update()
            time.sleep(interval)
    
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
        logger.info("Sensor fusion stopped")


if __name__ == "__main__":
    main()
