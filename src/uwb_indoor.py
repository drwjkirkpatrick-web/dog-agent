#!/usr/bin/env python3
"""
Dog Agent — UWB Indoor Positioning
==================================
Ultra-wideband indoor positioning using DWM1000/DWM3000 anchors.

Features:
  - Sub-meter indoor positioning when GPS unavailable
  - Triangulation from fixed anchors
  - Integration with dead reckoning

Usage:
    python src/uwb_indoor.py
    python src/uwb_indoor.py --simulate
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

import yaml

# Logging
logger = logging.getLogger("uwb_indoor")
handler = logging.StreamHandler(sys.stdout)
handler.setFormatter(logging.Formatter(
    "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
))
logger.addHandler(handler)
logger.setLevel(logging.INFO)

PROJECT_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_DIR / "config.yaml"

LIGHT_SPEED_MPS = 299702547  # Speed of light in air


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
class UWBPose:
    """Estimated indoor position."""
    x_m: float
    y_m: float
    z_m: Optional[float]
    accuracy_m: float
    anchor_count: int
    timestamp: datetime
    
    def to_dict(self) -> dict:
        return {
            "x_m": round(self.x_m, 3),
            "y_m": round(self.y_m, 3),
            "z_m": round(self.z_m, 3) if self.z_m is not None else None,
            "accuracy_m": round(self.accuracy_m, 3),
            "anchor_count": self.anchor_count,
            "timestamp": self.timestamp.isoformat(),
        }


class UWBAnchor:
    """Fixed UWB anchor definition."""
    
    def __init__(self, id: str, x: float, y: float, z: Optional[float] = None):
        self.id = id
        self.x = x
        self.y = y
        self.z = z


class DWM1000Interface:
    """Interface to DWM1000/DWM3000 over SPI or serial."""
    
    def __init__(self, spi_bus: int, spi_device: int):
        self.spi_bus = spi_bus
        self.spi_device = spi_device
        self._spi = None
        
    def _init(self):
        try:
            import spidev
            self._spi = spidev.SpiDev()
            self._spi.open(self.spi_bus, self.spi_device)
            self._spi.max_speed_hz = 1000000
            logger.info(f"DWM1000 SPI initialized bus={self.spi_bus} device={self.spi_device}")
        except Exception as e:
            logger.warning(f"Failed to init DWM1000 SPI: {e}")
            self._spi = None
    
    def poll_distances(self) -> Dict[str, float]:
        """Poll distances to visible anchors."""
        if not self._spi:
            return {}
        
        try:
            # Simplified: real implementation reads ranging data from DWM1000
            return {}
        except Exception as e:
            logger.error(f"DWM1000 poll error: {e}")
            return {}


class UWBIndoorPositioning:
    """UWB indoor positioning engine."""
    
    def __init__(self):
        self.enabled = get_cfg("uwb_indoor.enabled", False)
        self.spi_bus = get_cfg("uwb_indoor.spi_bus", 0)
        self.spi_device = get_cfg("uwb_indoor.spi_device", 0)
        self.poll_interval_sec = get_cfg("uwb_indoor.poll_interval_sec", 0.2)
        
        self.anchors: List[UWBAnchor] = []
        for a in get_cfg("uwb_indoor.anchors", []):
            self.anchors.append(UWBAnchor(
                id=a.get("id", "unknown"),
                x=a.get("x", 0),
                y=a.get("y", 0),
                z=a.get("z"),
            ))
        
        self._interface = DWM1000Interface(self.spi_bus, self.spi_device)
        self._last_pose: Optional[UWBPose] = None
        self._pose_history: List[UWBPose] = []
        self._lock = threading.Lock()
        
        if self.enabled:
            self._interface._init()
    
    def _trilaterate(self, distances: Dict[str, float]) -> Optional[UWBPose]:
        """Simple trilateration using least squares."""
        if len(distances) < 3:
            return None
        
        # Filter anchors with known positions and valid distances
        known = []
        for anchor in self.anchors:
            d = distances.get(anchor.id)
            if d and d > 0:
                known.append((anchor, d))
        
        if len(known) < 3:
            return None
        
        # Least squares trilateration
        A = []
        b = []
        ref = known[0][0]
        for anchor, d in known[1:]:
            A.append([
                2 * (anchor.x - ref.x),
                2 * (anchor.y - ref.y),
            ])
            b.append(
                ref.x**2 - anchor.x**2 +
                ref.y**2 - anchor.y**2 +
                d**2 - distances.get(ref.id, 0)**2
            )
        
        try:
            import numpy as np
            x = np.linalg.lstsq(np.array(A), np.array(b), rcond=None)[0]
            est_x = x[0]
            est_y = x[1]
            
            # Estimate accuracy from residuals
            residuals = []
            for anchor, d in known:
                est_dist = math.sqrt((est_x - anchor.x)**2 + (est_y - anchor.y)**2)
                residuals.append(abs(est_dist - d))
            accuracy = sum(residuals) / len(residuals) if residuals else 1.0
            
            return UWBPose(
                x_m=est_x,
                y_m=est_y,
                z_m=None,
                accuracy_m=max(accuracy, 0.1),
                anchor_count=len(known),
                timestamp=datetime.now(timezone.utc),
            )
        except ImportError:
            logger.warning("numpy not available, falling back to simple centroid")
            x_sum = sum(a.x for a, _ in known)
            y_sum = sum(a.y for a, _ in known)
            return UWBPose(
                x_m=x_sum / len(known),
                y_m=y_sum / len(known),
                z_m=None,
                accuracy_m=2.0,
                anchor_count=len(known),
                timestamp=datetime.now(timezone.utc),
            )
    
    def update(self):
        """Poll UWB and update position estimate."""
        if not self.enabled:
            return
        
        distances = self._interface.poll_distances()
        if not distances:
            return
        
        pose = self._trilaterate(distances)
        if pose:
            with self._lock:
                self._last_pose = pose
                self._pose_history.append(pose)
                if len(self._pose_history) > 100:
                    self._pose_history.pop(0)
    
    def get_latest(self) -> Optional[UWBPose]:
        with self._lock:
            return self._last_pose
    
    def get_stats(self) -> dict:
        pose = self.get_latest()
        return {
            "enabled": self.enabled,
            "anchors": len(self.anchors),
            "current": pose.to_dict() if pose else None,
        }


class UWBHTTPHandler(BaseHTTPRequestHandler):
    """HTTP API for UWB indoor positioning."""
    
    uwb: Optional[UWBIndoorPositioning] = None
    
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
        
        if path == "uwb/health":
            self._send_json({
                "status": "ok",
                "service": "uwb_indoor",
                "enabled": bool(self.uwb and self.uwb.enabled),
            })
        elif path == "uwb/position":
            if not self.uwb:
                self._send_json({"error": "UWB not initialized"}, 503)
                return
            pose = self.uwb.get_latest()
            if pose:
                self._send_json(pose.to_dict())
            else:
                self._send_json({"error": "No UWB position available"}, 503)
        elif path == "uwb/stats":
            if not self.uwb:
                self._send_json({"error": "UWB not initialized"}, 503)
                return
            self._send_json(self.uwb.get_stats())
        else:
            self._send_json({"error": f"Unknown endpoint: {path}"}, 404)


def main():
    parser = argparse.ArgumentParser(description="Dog Agent — UWB Indoor Positioning")
    parser.add_argument("--port", type=int, default=9151, help="HTTP API port")
    parser.add_argument("--simulate", action="store_true", help="Run simulation")
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose logging")
    args = parser.parse_args()
    
    if args.verbose:
        logger.setLevel(logging.DEBUG)
    
    uwb = UWBIndoorPositioning()
    
    if args.simulate:
        logger.info("=== UWB Indoor Simulation ===")
        # Simulate distances to 3 anchors
        uwb.anchors = [
            UWBAnchor("A1", 0, 0),
            UWBAnchor("A2", 5, 0),
            UWBAnchor("A3", 2.5, 5),
        ]
        distances = {"A1": 2.5, "A2": 3.2, "A3": 2.8}
        pose = uwb._trilaterate(distances)
        if pose:
            logger.info(f"Estimated position: {pose.to_dict()}")
        return
    
    UWBHTTPHandler.uwb = uwb
    
    server = HTTPServer(("127.0.0.1", args.port), UWBHTTPHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    logger.info(f"UWB API on http://127.0.0.1:{args.port}")
    
    # Update loop
    def update_loop():
        while True:
            if uwb.enabled:
                uwb.update()
            time.sleep(uwb.poll_interval_sec)
    
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
        logger.info("UWB module stopped")


if __name__ == "__main__":
    main()
