#!/usr/bin/env python3
"""
Web Dashboard — Dog Agent
===========================
Flask-based web interface for non-technical users to monitor their dog's status.

Features:
  - Live map with GPS trail (Leaflet.js)
  - Health vitals graphs (Chart.js)
  - Behavior timeline
  - Photo gallery placeholder
  - Settings panel
  - Real-time updates via Server-Sent Events (SSE)
  - Responsive design for mobile
  - Basic HTTP authentication

API Endpoints:
  - GET / — Main dashboard
  - GET /api/status — System status JSON
  - GET /api/location — Current location
  - GET /api/history — GPS history
  - GET /api/health — Health vitals
  - GET /api/behavior — Behavior data
  - GET /api/events — SSE stream for real-time updates

Configuration (config.yaml):
  web_dashboard:
    enabled: true
    port: 9137
    auth_password: "your_password_here"
    refresh_interval_sec: 5

Usage:
    python src/web_dashboard.py
    python src/web_dashboard.py --config /path/to/config.yaml
    python src/web_dashboard.py --port 9138
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import logging
import os
import signal
import sys
import threading
import time
from collections import deque
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import yaml

from flask import Flask, Response, jsonify, render_template, request, session, stream_with_context

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logger = logging.getLogger("web_dashboard")
_handler = logging.StreamHandler(sys.stdout)
_handler.setFormatter(logging.Formatter(
    "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
))
logger.addHandler(_handler)
logger.setLevel(logging.INFO)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_CONFIG_PATH = os.path.join(PROJECT_DIR, "config.yaml")
DATA_DIR = os.path.join(PROJECT_DIR, "data")
GPS_TRACK_DIR = os.path.join(DATA_DIR, "gps_tracks")
HEALTH_LOG_DIR = os.path.join(DATA_DIR, "health_logs")
BEHAVIOR_DIR = os.path.join(DATA_DIR, "behavior")
PHOTO_DIR = os.path.join(DATA_DIR, "photos")

# Ensure directories exist
os.makedirs(GPS_TRACK_DIR, exist_ok=True)
os.makedirs(HEALTH_LOG_DIR, exist_ok=True)
os.makedirs(BEHAVIOR_DIR, exist_ok=True)
os.makedirs(PHOTO_DIR, exist_ok=True)

# Default configuration
DEFAULT_CONFIG = {
    "web_dashboard": {
        "enabled": True,
        "port": 9137,
        "auth_password": "",
        "refresh_interval_sec": 5,
        "session_secret": None,  # Will be generated if not set
        "max_history_points": 1000,  # Max GPS points to return
    },
    "dog": {
        "name": "Fido",
        "breed": "Labrador Retriever",
    },
    "geofence": {
        "home_zone": {"lat": 45.5152, "lon": -122.6784, "radius_meters": 50},
    },
}


def load_config(config_path: str = DEFAULT_CONFIG_PATH) -> Dict[str, Any]:
    """Load configuration from YAML file."""
    if os.path.exists(config_path):
        try:
            with open(config_path) as f:
                config = yaml.safe_load(f) or {}
            return {**DEFAULT_CONFIG, **config}
        except Exception as e:
            logger.error(f"Failed to load config: {e}")
    return DEFAULT_CONFIG


def get_cfg(config: Dict, path: str, default=None):
    """Get nested config value by dot-separated path."""
    keys = path.split(".")
    value = config
    for k in keys:
        if isinstance(value, dict):
            value = value.get(k)
        else:
            return default
    return value if value is not None else default


# ---------------------------------------------------------------------------
# Data Store
# ---------------------------------------------------------------------------
class DataStore:
    """Thread-safe in-memory data cache with persistence helpers."""

    def __init__(self, max_history: int = 1000):
        self._lock = threading.Lock()
        self.max_history = max_history
        
        # GPS track (lat, lon, timestamp)
        self.gps_history: deque = deque(maxlen=max_history)
        self.current_location: Dict[str, Any] = {"lat": 0.0, "lon": 0.0, "valid": False}
        
        # Health vitals
        self.health_history: deque = deque(maxlen=max_history)
        self.current_vitals: Dict[str, Any] = {
            "heart_rate_bpm": 0,
            "temperature_c": 0,
            "activity_level": 0,
            "timestamp": None,
        }
        
        # System status
        self.system_status: Dict[str, Any] = {
            "online": True,
            "battery_percent": 0,
            "gps_fix": False,
            "satellites": 0,
            "last_update": None,
        }
        
        # Behavior events
        self.behavior_events: deque = deque(maxlen=100)
        
        # Alerts
        self.alerts: deque = deque(maxlen=50)

    def update_gps(self, lat: float, lon: float, timestamp: Optional[datetime] = None):
        """Update current location and add to history."""
        if timestamp is None:
            timestamp = datetime.now(timezone.utc)
        
        with self._lock:
            self.current_location = {
                "lat": lat,
                "lon": lon,
                "valid": True,
                "timestamp": timestamp.isoformat(),
            }
            self.gps_history.append({
                "lat": lat,
                "lon": lon,
                "timestamp": timestamp.isoformat(),
            })
            self.system_status["last_update"] = timestamp.isoformat()

    def update_vitals(self, vitals: Dict[str, Any]):
        """Update health vitals."""
        timestamp = datetime.now(timezone.utc)
        
        with self._lock:
            self.current_vitals = {
                **vitals,
                "timestamp": timestamp.isoformat(),
            }
            self.health_history.append(self.current_vitals.copy())

    def update_system_status(self, status: Dict[str, Any]):
        """Update system status."""
        with self._lock:
            self.system_status.update(status)
            self.system_status["last_update"] = datetime.now(timezone.utc).isoformat()

    def add_behavior_event(self, event_type: str, description: str, data: Optional[Dict] = None):
        """Add a behavior event."""
        with self._lock:
            self.behavior_events.append({
                "type": event_type,
                "description": description,
                "data": data or {},
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })

    def add_alert(self, level: str, message: str, data: Optional[Dict] = None):
        """Add an alert."""
        with self._lock:
            self.alerts.append({
                "level": level,
                "message": message,
                "data": data or {},
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "acknowledged": False,
            })

    def get_gps_history(self, limit: Optional[int] = None) -> List[Dict]:
        """Get GPS history."""
        with self._lock:
            history = list(self.gps_history)
            if limit:
                history = history[-limit:]
            return history

    def get_health_history(self, limit: Optional[int] = None) -> List[Dict]:
        """Get health vitals history."""
        with self._lock:
            history = list(self.health_history)
            if limit:
                history = history[-limit:]
            return history

    def get_behavior_events(self, limit: int = 50) -> List[Dict]:
        """Get recent behavior events."""
        with self._lock:
            return list(self.behavior_events)[-limit:]

    def get_alerts(self, limit: int = 20) -> List[Dict]:
        """Get recent alerts."""
        with self._lock:
            return list(self.alerts)[-limit:]

    def get_summary(self) -> Dict[str, Any]:
        """Get full data summary."""
        with self._lock:
            return {
                "location": self.current_location.copy(),
                "vitals": self.current_vitals.copy(),
                "system": self.system_status.copy(),
                "gps_points": len(self.gps_history),
                "health_readings": len(self.health_history),
                "behavior_events": len(self.behavior_events),
                "alerts": len(self.alerts),
            }


# Global data store
data_store = DataStore()


# ---------------------------------------------------------------------------
# Data Fetcher (from other daemons)
# ---------------------------------------------------------------------------
class DataFetcher:
    """Fetches data from other dog-agent daemons."""

    def __init__(self, config: Dict):
        self.config = config
        self.hermes_port = get_cfg(config, "hermes.api_port", 9110)
        self.base_url = f"http://127.0.0.1:{self.hermes_port}"
        self.running = False
        self.thread: Optional[threading.Thread] = None

    def start(self):
        """Start the data fetcher thread."""
        self.running = True
        self.thread = threading.Thread(target=self._fetch_loop, daemon=True)
        self.thread.start()
        logger.info("Data fetcher started")

    def stop(self):
        """Stop the data fetcher thread."""
        self.running = False
        if self.thread:
            self.thread.join(timeout=5)
        logger.info("Data fetcher stopped")

    def _fetch_loop(self):
        """Main fetch loop."""
        refresh_interval = get_cfg(self.config, "web_dashboard.refresh_interval_sec", 5)
        
        while self.running:
            try:
                self._fetch_gps()
                self._fetch_health()
                self._fetch_system_status()
                time.sleep(refresh_interval)
            except Exception as e:
                logger.error(f"Error in fetch loop: {e}")
                time.sleep(1)

    def _fetch_gps(self):
        """Fetch GPS data from daemon."""
        try:
            import requests
            resp = requests.get(f"{self.base_url}/gps", timeout=2)
            if resp.status_code == 200:
                data = resp.json()
                if data.get("valid"):
                    data_store.update_gps(
                        lat=data.get("lat", 0),
                        lon=data.get("lon", 0),
                    )
        except Exception as e:
            logger.debug(f"Failed to fetch GPS: {e}")

    def _fetch_health(self):
        """Fetch health data from daemon."""
        try:
            import requests
            resp = requests.get(f"{self.base_url}/health", timeout=2)
            if resp.status_code == 200:
                data = resp.json()
                data_store.update_vitals({
                    "heart_rate_bpm": data.get("heart_rate_bpm", 0),
                    "temperature_c": data.get("temperature_c", 0),
                    "activity_level": data.get("activity_level", 0),
                })
        except Exception as e:
            logger.debug(f"Failed to fetch health: {e}")

    def _fetch_system_status(self):
        """Fetch system status from various daemons."""
        try:
            import requests
            # Try power manager for battery
            try:
                resp = requests.get(f"http://127.0.0.1:9120/status", timeout=1)
                if resp.status_code == 200:
                    data = resp.json()
                    data_store.update_system_status({
                        "battery_percent": data.get("battery_percent", 0),
                    })
            except:
                pass
        except Exception as e:
            logger.debug(f"Failed to fetch system status: {e}")


# ---------------------------------------------------------------------------
# Flask App
# ---------------------------------------------------------------------------
def create_app(config: Dict) -> Flask:
    """Create and configure the Flask application."""
    
    app = Flask(__name__,
        template_folder="templates/dashboard",
        static_folder="templates/dashboard/static"
    )
    
    # Configuration
    app.config["SECRET_KEY"] = get_cfg(config, "web_dashboard.session_secret") or base64.b64encode(os.urandom(32)).decode()
    app.config["AUTH_PASSWORD"] = get_cfg(config, "web_dashboard.auth_password", "")
    app.config["DOG_NAME"] = get_cfg(config, "dog.name", "Fido")
    app.config["DOG_BREED"] = get_cfg(config, "dog.breed", "Dog")
    app.config["HOME_LAT"] = get_cfg(config, "geofence.home_zone.lat", 45.5152)
    app.config["HOME_LON"] = get_cfg(config, "geofence.home_zone.lon", -122.6784)
    app.config["REFRESH_INTERVAL"] = get_cfg(config, "web_dashboard.refresh_interval_sec", 5)
    
    return app


def require_auth(f: Callable) -> Callable:
    """Decorator to require authentication."""
    @wraps(f)
    def decorated(*args, **kwargs):
        password = request.app.config.get("AUTH_PASSWORD", "")
        if not password:
            return f(*args, **kwargs)
        
        # Check session
        if session.get("authenticated"):
            return f(*args, **kwargs)
        
        # Check basic auth
        auth = request.headers.get("Authorization")
        if auth and auth.startswith("Basic "):
            try:
                decoded = base64.b64decode(auth[6:]).decode("utf-8")
                if decoded == f"admin:{password}" or decoded == password:
                    return f(*args, **kwargs)
            except:
                pass
        
        # Return 401
        return Response(
            "Authentication required",
            401,
            {"WWW-Authenticate": 'Basic realm="Dog Agent Dashboard"'}
        )
    return decorated


def create_routes(app: Flask):
    """Create Flask routes."""

    @app.route("/")
    @require_auth
    def index():
        """Main dashboard page."""
        return render_template("index.html",
            dog_name=app.config["DOG_NAME"],
            dog_breed=app.config["DOG_BREED"],
            home_lat=app.config["HOME_LAT"],
            home_lon=app.config["HOME_LON"],
            refresh_interval=app.config["REFRESH_INTERVAL"],
        )

    @app.route("/login", methods=["GET", "POST"])
    def login():
        """Login page."""
        if request.method == "POST":
            password = request.form.get("password", "")
            if password == app.config["AUTH_PASSWORD"]:
                session["authenticated"] = True
                return jsonify({"success": True})
            return jsonify({"success": False, "error": "Invalid password"}), 401
        return render_template("login.html")

    @app.route("/logout")
    def logout():
        """Logout."""
        session.pop("authenticated", None)
        return jsonify({"success": True})

    @app.route("/api/status")
    @require_auth
    def api_status():
        """Get system status."""
        return jsonify(data_store.get_summary())

    @app.route("/api/location")
    @require_auth
    def api_location():
        """Get current location."""
        return jsonify(data_store.current_location)

    @app.route("/api/history")
    @require_auth
    def api_history():
        """Get GPS history."""
        limit = request.args.get("limit", type=int, default=500)
        return jsonify({
            "points": data_store.get_gps_history(limit),
            "count": len(data_store.gps_history),
        })

    @app.route("/api/health")
    @require_auth
    def api_health():
        """Get health vitals."""
        return jsonify({
            "current": data_store.current_vitals,
            "history": data_store.get_health_history(100),
        })

    @app.route("/api/behavior")
    @require_auth
    def api_behavior():
        """Get behavior events."""
        limit = request.args.get("limit", type=int, default=50)
        return jsonify({
            "events": data_store.get_behavior_events(limit),
        })

    @app.route("/api/alerts")
    @require_auth
    def api_alerts():
        """Get alerts."""
        limit = request.args.get("limit", type=int, default=20)
        return jsonify({
            "alerts": data_store.get_alerts(limit),
        })

    @app.route("/api/events")
    @require_auth
    def api_events():
        """Server-Sent Events stream for real-time updates."""
        def generate():
            last_data = None
            while True:
                data = data_store.get_summary()
                if data != last_data:
                    yield f"data: {json.dumps(data)}\n\n"
                    last_data = data
                time.sleep(app.config["REFRESH_INTERVAL"])
        
        return Response(
            stream_with_context(generate()),
            mimetype="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            }
        )

    @app.errorhandler(404)
    def not_found(e):
        return jsonify({"error": "Not found"}), 404

    @app.errorhandler(500)
    def server_error(e):
        return jsonify({"error": "Internal server error"}), 500


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Dog Agent Web Dashboard")
    parser.add_argument("--config", "-c", type=str, default=DEFAULT_CONFIG_PATH,
                        help="Path to config file")
    parser.add_argument("--port", "-p", type=int, default=None,
                        help="Port to listen on (overrides config)")
    parser.add_argument("--host", type=str, default="0.0.0.0",
                        help="Host to bind to")
    parser.add_argument("--debug", action="store_true",
                        help="Enable debug mode")
    
    args = parser.parse_args()
    
    # Load configuration
    config = load_config(args.config)
    
    # Check if enabled
    if not get_cfg(config, "web_dashboard.enabled", True):
        logger.info("Web dashboard is disabled in config")
        return 0
    
    # Get port
    port = args.port or get_cfg(config, "web_dashboard.port", 9137)
    
    # Create Flask app
    app = create_app(config)
    
    # Create routes
    create_routes(app)
    
    # Start data fetcher
    fetcher = DataFetcher(config)
    fetcher.start()
    
    # Handle shutdown gracefully
    def shutdown(signum, frame):
        logger.info("Shutting down...")
        fetcher.stop()
        sys.exit(0)
    
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    
    # Add some demo data if running in debug mode and store is empty
    if args.debug:
        import random
        from datetime import timedelta
        
        base_lat = get_cfg(config, "geofence.home_zone.lat", 45.5152)
        base_lon = get_cfg(config, "geofence.home_zone.lon", -122.6784)
        
        now = datetime.now(timezone.utc)
        for i in range(50):
            t = now - timedelta(minutes=i*5)
            lat = base_lat + random.uniform(-0.001, 0.001)
            lon = base_lon + random.uniform(-0.001, 0.001)
            data_store.update_gps(lat, lon, t)
            data_store.update_vitals({
                "heart_rate_bpm": 70 + random.randint(-10, 20),
                "temperature_c": 38.5 + random.uniform(-0.5, 0.5),
                "activity_level": random.uniform(0, 1),
            })
    
    # Run Flask
    logger.info(f"Starting web dashboard on {args.host}:{port}")
    app.run(host=args.host, port=port, debug=args.debug, threaded=True)
    
    return 0


if __name__ == "__main__":
    sys.exit(main())
