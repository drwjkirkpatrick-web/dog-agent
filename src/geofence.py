#!/usr/bin/env python3
"""
Geofence Module — Dog Agent
============================
Reads GPS position from the GPS daemon HTTP API (localhost:9110/gps),
manages geofence zones loaded from data/zones.json, calculates distances
using the Haversine formula, tracks zone entry/exit events with callbacks,
detects escape when the dog moves beyond the escape radius from home,
and exposes a /geofence HTTP API.

Usage:
    python src/geofence.py               # Normal mode
    python src/geofence.py --test         # Test mode (fake GPS walking away/back)
    python src/geofence.py --config /path/to/config.yaml
    python src/geofence.py --port 9111    # Custom API listen port
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import signal
import sys
import threading
import time
from contextlib import suppress
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

import yaml

try:
    import requests
except ImportError:
    requests = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logger = logging.getLogger("geofence")
_handler = logging.StreamHandler(sys.stdout)
_handler.setFormatter(logging.Formatter(
    "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
))
_handler.flush = sys.stdout.flush  # type: ignore[assignment]
logger.addHandler(_handler)
logger.setLevel(logging.INFO)


# ---------------------------------------------------------------------------
# Events logger — appends to data/events.log
# ---------------------------------------------------------------------------
class EventsLogger:
    """Appends zone events to a log file with timestamps."""

    def __init__(self, path: str) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def log(self, event_type: str, zone_name: str, detail: str = "") -> None:
        """Write a structured event line."""
        timestamp = datetime.now(timezone.utc).isoformat()
        line = json.dumps({
            "timestamp": timestamp,
            "event": event_type,
            "zone": zone_name,
            "detail": detail,
        }, ensure_ascii=False)
        with self._lock:
            with open(self._path, "a") as f:
                f.write(line + "\n")
                f.flush()
        logger.info("EVENT [%s] zone=%s %s", event_type, zone_name, detail)


# ---------------------------------------------------------------------------
# Haversine distance
# ---------------------------------------------------------------------------
def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in meters between two GPS coordinates."""
    R = 6_371_000  # Earth radius in meters
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)

    a = (math.sin(dphi / 2) ** 2
         + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2)
    c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))
    return R * c


# ---------------------------------------------------------------------------
# Zone model
# ---------------------------------------------------------------------------
ZoneCallback = Callable[[str, float, Dict[str, Any]], None]
"""Callback signature: callback(zone_name, distance_meters, position_dict)"""


class Zone:
    """A circular geofence zone."""

    __slots__ = ("name", "lat", "lon", "radius_meters")

    def __init__(self, name: str, lat: float, lon: float, radius_meters: float) -> None:
        self.name = name
        self.lat = lat
        self.lon = lon
        self.radius_meters = radius_meters

    def distance_from(self, lat: float, lon: float) -> float:
        """Return distance in meters from this zone's center."""
        return haversine(self.lat, self.lon, lat, lon)

    def contains(self, lat: float, lon: float) -> bool:
        """Return True if the point is inside (or on) the zone boundary."""
        return self.distance_from(lat, lon) <= self.radius_meters

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "lat": self.lat,
            "lon": self.lon,
            "radius_meters": self.radius_meters,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Zone":
        return cls(
            name=str(d["name"]),
            lat=float(d["lat"]),
            lon=float(d["lon"]),
            radius_meters=float(d["radius_meters"]),
        )

    def __repr__(self) -> str:
        return (f"Zone(name={self.name!r}, lat={self.lat}, lon={self.lon}, "
                f"radius={self.radius_meters}m)")


# ---------------------------------------------------------------------------
# Zone manager — thread-safe
# ---------------------------------------------------------------------------
class ZoneManager:
    """Manages a collection of geofence zones with persistence."""

    def __init__(self, zones_path: str) -> None:
        self._path = Path(zones_path)
        self._lock = threading.RLock()
        self._zones: Dict[str, Zone] = {}
        self._load()

    def _load(self) -> None:
        """Load zones from JSON file. Creates empty if missing."""
        if not self._path.exists():
            logger.info("No zones file at %s — starting empty", self._path)
            self._zones = {}
            return
        try:
            with open(self._path) as f:
                data = json.load(f)
            if isinstance(data, list):
                for entry in data:
                    zone = Zone.from_dict(entry)
                    self._zones[zone.name] = zone
            elif isinstance(data, dict):
                # Support both {name: {...}} and list formats
                for name, entry in data.items():
                    if isinstance(entry, dict):
                        entry["name"] = name
                        zone = Zone.from_dict(entry)
                        self._zones[zone.name] = zone
            logger.info("Loaded %d zone(s) from %s", len(self._zones), self._path)
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
            logger.error("Failed to load zones from %s: %s", self._path, e)
            self._zones = {}

    def save(self) -> None:
        """Persist current zones to JSON file."""
        with self._lock:
            data = [z.to_dict() for z in self._zones.values()]
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._path, "w") as f:
                json.dump(data, f, indent=2)
            logger.debug("Saved %d zone(s) to %s", len(data), self._path)

    def add(self, zone: Zone) -> None:
        """Add or update a zone and persist."""
        with self._lock:
            self._zones[zone.name] = zone
            self.save()

    def remove(self, name: str) -> bool:
        """Remove a zone by name. Returns True if removed."""
        with self._lock:
            if name in self._zones:
                del self._zones[name]
                self.save()
                return True
            return False

    def get(self, name: str) -> Optional[Zone]:
        with self._lock:
            return self._zones.get(name)

    def list_all(self) -> List[Zone]:
        with self._lock:
            return list(self._zones.values())

    def has(self, name: str) -> bool:
        with self._lock:
            return name in self._zones


# ---------------------------------------------------------------------------
# Fake GPS position generator (test mode)
# ---------------------------------------------------------------------------
class FakeGPSWalker:
    """Generates GPS positions that walk away from home, pause, then return.

    Simulates a dog leaving home zone, crossing the escape radius, and
    coming back, so all geofence events can be exercised.
    """

    def __init__(self, home_lat: float, home_lon: float) -> None:
        self._home_lat = home_lat
        self._home_lon = home_lon
        self._t0 = time.monotonic()
        self._step = 0

        # Pre-compute a walk path: duration ~6 minutes total
        # Phase 1 (steps 0-29): walk away ~250m over 30 steps
        # Phase 2 (steps 30-34): pause away from home
        # Phase 3 (steps 35-64): walk back home
        self._path: List[Tuple[float, float, float]] = []  # (lat, lon, speed_mps)

        # Walk direction: roughly northwest from home
        dlat = 0.002  # ~222m
        dlon = -0.002  # ~170m
        steps_away = 30
        steps_pause = 5
        steps_back = 30

        for i in range(steps_away):
            frac = (i + 1) / steps_away
            lat = home_lat + dlat * frac
            lon = home_lon + dlon * frac
            speed = 1.2  # m/s walking pace
            self._path.append((lat, lon, speed))

        # Pause
        lat_away = home_lat + dlat
        lon_away = home_lon + dlon
        for _ in range(steps_pause):
            self._path.append((lat_away, lon_away, 0.0))

        # Walk back
        for i in range(steps_back):
            frac = (steps_back - i) / steps_back
            lat = home_lat + dlat * frac
            lon = home_lon + dlon * frac
            speed = 1.0
            self._path.append((lat, lon, speed))

    def read_position(self) -> Dict[str, Any]:
        """Return the next simulated GPS position dict."""
        idx = self._step % len(self._path)
        lat, lon, speed = self._path[idx]
        self._step += 1

        # Simulate fractional movement between waypoints for smoother data
        sub_step = (self._step % 3) / 3.0
        next_idx = (idx + 1) % len(self._path)
        next_lat, next_lon, _ = self._path[next_idx]
        lat += (next_lat - lat) * sub_step * 0.3
        lon += (next_lon - lon) * sub_step * 0.3

        return {
            "lat": lat,
            "lon": lon,
            "altitude": 100.0,
            "speed_mps": speed,
            "speed_knots": speed / 0.514444,
            "heading": 0.0,
            "fix_quality": 1,
            "satellites": 8,
            "valid": True,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }


# ---------------------------------------------------------------------------
# Zone status tracker — tracks entry/exit state per zone
# ---------------------------------------------------------------------------
class ZoneStatus:
    """Tracks whether the dog is currently inside each zone and fires callbacks."""

    def __init__(self, events_logger: EventsLogger) -> None:
        self._lock = threading.Lock()
        self._inside: Set[str] = set()  # zone names currently occupied
        self._on_enter: List[ZoneCallback] = []
        self._on_exit: List[ZoneCallback] = []
        self._on_escape: List[ZoneCallback] = []
        self._events = events_logger

    def register_on_enter(self, cb: ZoneCallback) -> None:
        self._on_enter.append(cb)

    def register_on_exit(self, cb: ZoneCallback) -> None:
        self._on_exit.append(cb)

    def register_on_escape(self, cb: ZoneCallback) -> None:
        self._on_escape.append(cb)

    def update(
        self,
        zone: Zone,
        distance: float,
        position: Dict[str, Any],
    ) -> None:
        """Check if the dog entered or exited *zone* and fire callbacks."""
        is_inside = distance <= zone.radius_meters
        was_inside: bool

        with self._lock:
            was_inside = zone.name in self._inside

            if is_inside and not was_inside:
                # Entered zone
                self._inside.add(zone.name)
                self._events.log("enter", zone.name,
                                 f"distance={distance:.1f}m, "
                                 f"threshold={zone.radius_meters}m")
            elif not is_inside and was_inside:
                # Exited zone
                self._inside.discard(zone.name)
                self._events.log("exit", zone.name,
                                 f"distance={distance:.1f}m, "
                                 f"threshold={zone.radius_meters}m")
        # Fire callbacks outside lock to avoid deadlocks
        if is_inside and not was_inside:
            for cb in self._on_enter:
                try:
                    cb(zone.name, distance, position)
                except Exception:
                    logger.exception("on_enter callback failed for zone %s", zone.name)
        elif not is_inside and was_inside:
            for cb in self._on_exit:
                try:
                    cb(zone.name, distance, position)
                except Exception:
                    logger.exception("on_exit callback failed for zone %s", zone.name)

    def fire_escape(self, zone_name: str, distance: float, position: Dict[str, Any]) -> None:
        """Fire escape callbacks."""
        self._events.log("escape", zone_name,
                         f"distance={distance:.1f}m exceeds escape radius")
        for cb in self._on_escape:
            try:
                cb(zone_name, distance, position)
            except Exception:
                logger.exception("on_escape callback failed for zone %s", zone_name)

    def is_inside(self, zone_name: str) -> bool:
        with self._lock:
            return zone_name in self._inside

    def get_status(self, zone: Zone, current_lat: float, current_lon: float) -> Dict[str, Any]:
        """Return status dict for a single zone given the current position."""
        distance = zone.distance_from(current_lat, current_lon)
        inside = distance <= zone.radius_meters
        return {
            "name": zone.name,
            "lat": zone.lat,
            "lon": zone.lon,
            "radius_meters": zone.radius_meters,
            "distance_meters": round(distance, 2),
            "inside": inside,
        }


# ---------------------------------------------------------------------------
# Geofence checker thread
# ---------------------------------------------------------------------------
class GeofenceChecker:
    """Periodically fetches GPS position and checks all zones.

    Runs on a background thread at configurable intervals.
    """

    def __init__(
        self,
        zone_manager: ZoneManager,
        zone_status: ZoneStatus,
        home_zone: Zone,
        escape_radius_meters: float,
        check_interval_sec: float,
        gps_api_url: str = "http://127.0.0.1:9110/gps",
        fake_source: Optional[FakeGPSWalker] = None,
    ) -> None:
        self._zone_manager = zone_manager
        self._zone_status = zone_status
        self._home_zone = home_zone
        self._escape_radius = escape_radius_meters
        self._interval = check_interval_sec
        self._gps_url = gps_api_url
        self._fake_source = fake_source
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

        # Track escape alert state to avoid re-firing
        self._escape_alerted = False

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="geofence-checker",
            daemon=True,
        )
        self._thread.start()
        logger.info(
            "Geofence checker started (interval=%ds, escape_radius=%dm)",
            self._interval, self._escape_radius,
        )

    def stop(self) -> None:
        self._stop_event.set()

    def _fetch_position(self) -> Optional[Dict[str, Any]]:
        """Fetch current GPS position from daemon or fake source."""
        if self._fake_source:
            return self._fake_source.read_position()

        if requests is None:
            logger.error("requests library is not installed — cannot fetch GPS data")
            return None

        try:
            resp = requests.get(self._gps_url, timeout=5)
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as e:
            logger.warning("Failed to fetch GPS position from %s: %s", self._gps_url, e)
            return None

    def _run(self) -> None:
        """Main checker loop."""
        while not self._stop_event.is_set():
            try:
                self._check()
            except Exception:
                logger.exception("Unhandled error in geofence check cycle")
            self._stop_event.wait(self._interval)

    def force_check(self) -> Dict[str, Any]:
        """Run a single check cycle inline and return status summary."""
        position = self._fetch_position()
        if position is None or not position.get("valid", False):
            return {"status": "no_fix", "zones": []}

        lat = position["lat"]
        lon = position["lon"]
        return self._evaluate_position(lat, lon, position)

    def _check(self) -> None:
        """One check cycle: fetch position, evaluate zones, detect escape."""
        position = self._fetch_position()
        if position is None or not position.get("valid", False):
            logger.debug("Skipping geofence check — no valid GPS fix")
            return

        lat = position["lat"]
        lon = position["lon"]
        self._evaluate_position(lat, lon, position)

    def _evaluate_position(
        self, lat: float, lon: float, position: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Evaluate position against all zones and escape criteria.

        Returns a status summary dict.
        """
        zones_status = []

        # Check all managed zones (including home)
        for zone in self._zone_manager.list_all():
            distance = zone.distance_from(lat, lon)
            self._zone_status.update(zone, distance, position)
            zones_status.append(self._zone_status.get_status(zone, lat, lon))

        # Escape detection: distance from home zone
        home_dist = self._home_zone.distance_from(lat, lon)
        if home_dist > self._escape_radius:
            if not self._escape_alerted:
                self._escape_alerted = True
                self._zone_status.fire_escape(
                    self._home_zone.name, home_dist, position,
                )
                logger.warning(
                    "⚠️ ESCAPE DETECTED — dog is %.1fm from home "
                    "(threshold: %dm)", home_dist, self._escape_radius,
                )
        else:
            self._escape_alerted = False

        return {
            "status": "ok",
            "position": position,
            "home_distance_meters": round(home_dist, 2),
            "escape_radius_meters": self._escape_radius,
            "escape_alerted": self._escape_alerted,
            "zones": zones_status,
        }


# ---------------------------------------------------------------------------
# HTTP API Handler
# ---------------------------------------------------------------------------
class GeofenceAPIHandler(BaseHTTPRequestHandler):
    """Serves the geofence management API."""

    # Class-level references set by the server
    zone_manager: ZoneManager = None  # type: ignore[assignment]
    zone_status: ZoneStatus = None  # type: ignore[assignment]
    geofence_checker: GeofenceChecker = None  # type: ignore[assignment]

    def do_GET(self) -> None:
        parsed = self._parse_path()

        if parsed["path"] == "/geofence":
            self._handle_get_geofence()
        elif parsed["path"] == "/geofence/zones":
            self._handle_get_zones()
        elif parsed["path"] == "/geofence/health":
            self._json_response({"status": "ok", "service": "geofence"})
        else:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b'{"error": "not found"}')

    def do_POST(self) -> None:
        parsed = self._parse_path()

        if parsed["path"] == "/geofence/zones":
            self._handle_add_zone()
        else:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b'{"error": "not found"}')

    def do_DELETE(self) -> None:
        parsed = self._parse_path()

        if parsed["path"] == "/geofence/zones":
            self._handle_delete_zone(parsed["query"])
        else:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b'{"error": "not found"}')

    # ------------------------------------------------------------------
    # Handler implementations
    # ------------------------------------------------------------------

    def _handle_get_geofence(self) -> None:
        """Return full status: all zones + position + escape info."""
        result = self.geofence_checker.force_check()
        self._json_response(result)

    def _handle_get_zones(self) -> None:
        """Return list of all configured zones."""
        zones = [z.to_dict() for z in self.zone_manager.list_all()]
        self._json_response({"zones": zones, "count": len(zones)})

    def _handle_add_zone(self) -> None:
        """Add a new zone from JSON body."""
        content_length = int(self.headers.get("Content-Length", 0))
        if content_length == 0:
            self._json_response({"error": "empty request body"}, status=400)
            return

        try:
            body = self.rfile.read(content_length)
            data = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            self._json_response({"error": f"invalid JSON: {e}"}, status=400)
            return

        name = data.get("name")
        if not name:
            self._json_response({"error": "missing required field: name"}, status=400)
            return

        try:
            lat = float(data["lat"])
            lon = float(data["lon"])
            radius = float(data.get("radius_meters", 50))
        except (KeyError, TypeError, ValueError) as e:
            self._json_response(
                {"error": f"invalid numeric fields: {e}"},
                status=400,
            )
            return

        zone = Zone(name=str(name), lat=lat, lon=lon, radius_meters=radius)
        self.zone_manager.add(zone)
        logger.info("Zone added via API: %s", zone)
        self._json_response(zone.to_dict(), status=201)

    def _handle_delete_zone(self, query: Dict[str, List[str]]) -> None:
        """Remove a zone by name query parameter."""
        names = query.get("name", [])
        if not names:
            self._json_response({"error": "missing query parameter: name"}, status=400)
            return

        name = names[0]
        removed = self.zone_manager.remove(name)
        if removed:
            logger.info("Zone removed via API: %s", name)
            self._json_response({"removed": True, "name": name})
        else:
            self._json_response(
                {"removed": False, "name": name, "error": "zone not found"},
                status=404,
            )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _parse_path(self) -> Dict[str, Any]:
        """Split path and query parameters."""
        raw = self.path.split("?", 1)
        path = raw[0].rstrip("/")
        query: Dict[str, List[str]] = {}
        if len(raw) > 1 and raw[1]:
            for part in raw[1].split("&"):
                if "=" in part:
                    k, v = part.split("=", 1)
                    query.setdefault(k, []).append(v)
        return {"path": path, "query": query}

    def _json_response(self, data: dict, status: int = 200) -> None:
        body = json.dumps(data, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: Any) -> None:
        """Quiet the default HTTP server logging."""
        logger.debug(f"HTTP: {fmt % args}")


# ---------------------------------------------------------------------------
# Configuration loader
# ---------------------------------------------------------------------------
def load_config(path: str) -> dict:
    """Load YAML config, returning defaults for missing geofence keys."""
    with open(path) as f:
        cfg = yaml.safe_load(f) or {}

    gf_cfg = cfg.get("geofence", {})
    log_cfg = cfg.get("logging", {})
    dog_cfg = cfg.get("dog", {})

    home_cfg = gf_cfg.get("home_zone", {})
    home_lat = home_cfg.get("lat", 45.5152)
    home_lon = home_cfg.get("lon", -122.6784)
    home_radius = home_cfg.get("radius_meters", 50)

    zones_file = log_cfg.get("zones_file", "data/zones.json")

    # Resolve relative paths
    project_root = Path(path).resolve().parent
    if not os.path.isabs(zones_file):
        zones_file = str(project_root / zones_file)

    return {
        "home_zone": Zone("home", float(home_lat), float(home_lon), float(home_radius)),
        "escape_radius_meters": float(gf_cfg.get("escape_radius_meters", 200)),
        "check_interval_sec": float(gf_cfg.get("check_interval_sec", 30)),
        "zones_file": zones_file,
        "api_port": int(cfg.get("hermes", {}).get("api_port", 9110)),
        "dog_name": dog_cfg.get("name", "Dog"),
    }


# ---------------------------------------------------------------------------
# Main daemon
# ---------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(description="Dog Agent Geofence Module")
    parser.add_argument(
        "--config",
        default=None,
        help="Path to config.yaml (default: ../config.yaml relative to this script)",
    )
    parser.add_argument("--test", action="store_true", help="Test mode — fake GPS data")
    parser.add_argument(
        "--port", type=int, default=None,
        help="HTTP API listen port (overrides config)",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Enable debug logging",
    )
    args = parser.parse_args()

    if args.verbose:
        logger.setLevel(logging.DEBUG)

    # Resolve config path
    if args.config:
        config_path = args.config
    else:
        script_dir = Path(__file__).resolve().parent
        config_path = str(script_dir.parent / "config.yaml")

    if os.path.exists(config_path):
        cfg = load_config(config_path)
        logger.info("Loaded config from %s", config_path)
    else:
        logger.warning("No config.yaml found at %s; using defaults", config_path)
        cfg = {
            "home_zone": Zone("home", 45.5152, -122.6784, 50),
            "escape_radius_meters": 200.0,
            "check_interval_sec": 30.0,
            "zones_file": str(
                Path(__file__).resolve().parent.parent / "data" / "zones.json"
            ),
            "api_port": 9110,
            "dog_name": "Dog",
        }

    api_port = args.port if args.port is not None else cfg["api_port"]

    # ------------------------------------------------------------------
    # Initialise components
    # ------------------------------------------------------------------

    # Events logger
    script_dir = Path(__file__).resolve().parent
    events_log_path = str(script_dir.parent / "data" / "events.log")
    events_logger = EventsLogger(events_log_path)
    logger.info("Zone events logged to %s", events_log_path)

    # Zone manager
    zone_manager = ZoneManager(cfg["zones_file"])

    # Ensure home zone is always present
    home_zone = cfg["home_zone"]
    if not zone_manager.has(home_zone.name):
        zone_manager.add(home_zone)
        logger.info("Added default home zone: %s", home_zone)

    # Zone status tracker
    zone_status = ZoneStatus(events_logger)

    # Register default callbacks (log to events.log via the status tracker's built-in logging)
    def on_enter_cb(zone_name: str, distance: float, pos: Dict[str, Any]) -> None:
        logger.info("🐾 %s entered zone '%s' (dist=%.1fm)",
                    cfg["dog_name"], zone_name, distance)

    def on_exit_cb(zone_name: str, distance: float, pos: Dict[str, Any]) -> None:
        logger.info("🐾 %s exited zone '%s' (dist=%.1fm)",
                    cfg["dog_name"], zone_name, distance)

    def on_escape_cb(zone_name: str, distance: float, pos: Dict[str, Any]) -> None:
        logger.warning("⚠️ ESCAPE ALERT: %s is %.1fm from home zone '%s'!",
                       cfg["dog_name"], distance, zone_name)

    zone_status.register_on_enter(on_enter_cb)
    zone_status.register_on_exit(on_exit_cb)
    zone_status.register_on_escape(on_escape_cb)

    # Geofence checker
    fake_source: Optional[FakeGPSWalker] = None
    if args.test:
        logger.info("TEST MODE — simulating dog walking away from and back to home")
        fake_source = FakeGPSWalker(
            home_lat=home_zone.lat,
            home_lon=home_zone.lon,
        )
        # Speed up test: use 1-second intervals
        check_interval = 1.0
        logger.info("Test check interval set to 1s for rapid demonstration")
    else:
        check_interval = cfg["check_interval_sec"]

    checker = GeofenceChecker(
        zone_manager=zone_manager,
        zone_status=zone_status,
        home_zone=home_zone,
        escape_radius_meters=cfg["escape_radius_meters"],
        check_interval_sec=check_interval,
        fake_source=fake_source,
    )
    checker.start()

    # ------------------------------------------------------------------
    # Start HTTP API server
    # ------------------------------------------------------------------
    GeofenceAPIHandler.zone_manager = zone_manager
    GeofenceAPIHandler.zone_status = zone_status
    GeofenceAPIHandler.geofence_checker = checker

    server = HTTPServer(("127.0.0.1", api_port), GeofenceAPIHandler)
    server_thread = threading.Thread(
        target=server.serve_forever,
        name="geofence-api",
        daemon=True,
    )
    server_thread.start()
    logger.info("Geofence API server listening on http://127.0.0.1:%d/geofence", api_port)

    # ------------------------------------------------------------------
    # Graceful shutdown
    # ------------------------------------------------------------------
    def shutdown(signum: int, frame: Any) -> None:
        logger.info("Received signal %d — shutting down...", signum)
        checker.stop()
        server.shutdown()
        logger.info("Geofence module stopped.")
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    # Keep main thread alive
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        shutdown(signal.SIGINT, None)


if __name__ == "__main__":
    main()