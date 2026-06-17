#!/usr/bin/env python3
"""
Dog Agent — Smart Geofence Learning
====================================
Machine-learning geofence that learns safe zones from routine.

Features:
  - Cluster GPS history to find common stay locations
  - Suggest and auto-learn zones
  - Adaptive radius based on GPS confidence
  - Reduced false positives

Usage:
    python src/smart_geofence.py
    python src/smart_geofence.py --simulate
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
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

# Logging
logger = logging.getLogger("smart_geofence")
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
class Zone:
    id: str
    lat: float
    lon: float
    radius_m: float
    visits: int
    total_minutes: float
    learned: bool
    last_visit: Optional[datetime]
    
    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "lat": round(self.lat, 6),
            "lon": round(self.lon, 6),
            "radius_m": round(self.radius_m, 1),
            "visits": self.visits,
            "total_minutes": round(self.total_minutes, 1),
            "learned": self.learned,
            "last_visit": self.last_visit.isoformat() if self.last_visit else None,
        }


class SmartGeofence:
    """Learns safe zones from GPS history."""
    
    def __init__(self):
        self.enabled = get_cfg("smart_geofence.enabled", False)
        self.min_points = get_cfg("smart_geofence.min_points", 20)
        self.default_radius_m = get_cfg("smart_geofence.radius_m", 100)
        self.dbscan_eps_m = get_cfg("smart_geofence.dbscan_eps_m", 50)
        self.min_stay_min = get_cfg("smart_geofence.min_stay_min", 10)
        self.poll_interval_sec = get_cfg("smart_geofence.poll_interval_sec", 30)
        
        self._zones: List[Zone] = []
        self._suggested: List[Zone] = []
        self._pending_points: List[Tuple[float, float, datetime]] = []
        self._lock = threading.Lock()
        self._last_position: Optional[Tuple[float, float, datetime]] = None
        
        # Load configured zones
        self._load_configured()
    
    def _load_configured(self):
        """Load manually configured zones."""
        for z in get_cfg("geofence.zones", []):
            self._zones.append(Zone(
                id=z.get("id", "manual"),
                lat=z.get("lat", 0),
                lon=z.get("lon", 0),
                radius_m=z.get("radius_m", self.default_radius_m),
                visits=0,
                total_minutes=0,
                learned=False,
                last_visit=None,
            ))
    
    def _haversine_m(self, lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        R = 6371000
        phi1, phi2 = math.radians(lat1), math.radians(lat2)
        dphi = math.radians(lat2 - lat1)
        dlambda = math.radians(lon2 - lon1)
        a = math.sin(dphi/2)**2 + math.cos(phi1)*math.cos(phi2)*math.sin(dlambda/2)**2
        return 2 * R * math.asin(math.sqrt(a))
    
    def _cluster_points(self, points: List[Tuple[float, float]]) -> List[Tuple[float, float, int]]:
        """Simple DBSCAN-like clustering (no sklearn dependency)."""
        visited = set()
        clusters: List[List[Tuple[float, float]]] = []
        
        for i, p1 in enumerate(points):
            if i in visited:
                continue
            cluster = [p1]
            visited.add(i)
            
            for j, p2 in enumerate(points):
                if j in visited or i == j:
                    continue
                if self._haversine_m(p1[0], p1[1], p2[0], p2[1]) <= self.dbscan_eps_m:
                    cluster.append(p2)
                    visited.add(j)
            
            if len(cluster) >= max(3, self.min_points // 4):
                clusters.append(cluster)
        
        result = []
        for cluster in clusters:
            avg_lat = sum(p[0] for p in cluster) / len(cluster)
            avg_lon = sum(p[1] for p in cluster) / len(cluster)
            # Radius = max distance from center
            radius = max(self._haversine_m(avg_lat, avg_lon, p[0], p[1]) for p in cluster)
            result.append((avg_lat, avg_lon, len(cluster), max(radius, self.default_radius_m)))
        
        return result
    
    def learn(self):
        """Cluster pending points and suggest zones."""
        with self._lock:
            if len(self._pending_points) < self.min_points:
                return
            
            points = [(lat, lon) for lat, lon, _ in self._pending_points]
            clusters = self._cluster_points(points)
            
            for lat, lon, count, radius in clusters:
                # Check if near existing zone
                existing = False
                for zone in self._zones:
                    if self._haversine_m(lat, lon, zone.lat, zone.lon) < zone.radius_m:
                        existing = True
                        break
                
                if not existing:
                    zone_id = f"learned_{lat:.4f}_{lon:.4f}"
                    self._suggested.append(Zone(
                        id=zone_id,
                        lat=lat,
                        lon=lon,
                        radius_m=radius,
                        visits=count,
                        total_minutes=0,
                        learned=True,
                        last_visit=self._pending_points[-1][2],
                    ))
            
            # Clear pending points after learning
            self._pending_points = []
    
    def update(self):
        """Process current GPS position."""
        if not self.enabled:
            return
        
        try:
            import requests
            resp = requests.get("http://localhost:9111/gps", timeout=5)
            if resp.status_code != 200:
                return
            data = resp.json()
            lat = data.get("lat")
            lon = data.get("lon")
            if lat is None or lon is None:
                return
            
            now = datetime.now(timezone.utc)
            
            with self._lock:
                self._pending_points.append((lat, lon, now))
                if len(self._pending_points) > 1000:
                    self._pending_points.pop(0)
                
                # Update visits and stay time
                for zone in self._zones:
                    d = self._haversine_m(lat, lon, zone.lat, zone.lon)
                    if d <= zone.radius_m:
                        if zone.last_visit:
                            minutes = (now - zone.last_visit).total_seconds() / 60
                            if minutes < 30:
                                zone.total_minutes += minutes
                        zone.visits += 1
                        zone.last_visit = now
                        break
                
                self._last_position = (lat, lon, now)
        except Exception as e:
            logger.error(f"Update error: {e}")
    
    def approve_suggested(self, zone_id: str) -> bool:
        """Move suggested zone to active zones."""
        with self._lock:
            for i, zone in enumerate(self._suggested):
                if zone.id == zone_id:
                    self._zones.append(zone)
                    self._suggested.pop(i)
                    return True
            return False
    
    def get_zones(self) -> List[dict]:
        with self._lock:
            return [z.to_dict() for z in self._zones]
    
    def get_suggested(self) -> List[dict]:
        with self._lock:
            return [z.to_dict() for z in self._suggested]


class GeofenceHTTPHandler(BaseHTTPRequestHandler):
    """HTTP API for smart geofence."""
    
    geofence: Optional[SmartGeofence] = None
    
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
        
        if path == "geofence/health":
            self._send_json({
                "status": "ok",
                "service": "smart_geofence",
                "enabled": bool(self.geofence and self.geofence.enabled),
            })
        elif path == "geofence/zones":
            if not self.geofence:
                self._send_json({"error": "Not initialized"}, 503)
                return
            self._send_json({"zones": self.geofence.get_zones()})
        elif path == "geofence/suggest":
            if not self.geofence:
                self._send_json({"error": "Not initialized"}, 503)
                return
            self.geofence.learn()
            self._send_json({"suggested": self.geofence.get_suggested()})
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
        
        if path == "geofence/approve":
            if not self.geofence:
                self._send_json({"error": "Not initialized"}, 503)
                return
            zone_id = data.get("zone_id")
            success = self.geofence.approve_suggested(zone_id)
            self._send_json({"approved": success})
        else:
            self._send_json({"error": f"Unknown endpoint: {path}"}, 404)


def main():
    parser = argparse.ArgumentParser(description="Dog Agent — Smart Geofence")
    parser.add_argument("--port", type=int, default=9152, help="HTTP API port")
    parser.add_argument("--simulate", action="store_true", help="Run simulation")
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose logging")
    args = parser.parse_args()
    
    if args.verbose:
        logger.setLevel(logging.DEBUG)
    
    geofence = SmartGeofence()
    
    if args.simulate:
        logger.info("=== Smart Geofence Simulation ===")
        # Generate points around two clusters
        import random
        geofence.enabled = True
        for _ in range(50):
            lat = 45.5231 + random.uniform(-0.0005, 0.0005)
            lon = -122.6765 + random.uniform(-0.0005, 0.0005)
            geofence._pending_points.append((lat, lon, datetime.now(timezone.utc)))
        geofence.learn()
        logger.info(f"Suggested zones: {len(geofence.get_suggested())}")
        return
    
    GeofenceHTTPHandler.geofence = geofence
    
    server = HTTPServer(("127.0.0.1", args.port), GeofenceHTTPHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    logger.info(f"Smart geofence API on http://127.0.0.1:{args.port}")
    
    def update_loop():
        while True:
            if geofence.enabled:
                geofence.update()
            time.sleep(geofence.poll_interval_sec)
    
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
        logger.info("Smart geofence stopped")


if __name__ == "__main__":
    main()
