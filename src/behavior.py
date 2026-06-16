#!/usr/bin/env python3
"""
Dog Agent — Behavioral Analysis Module
=======================================
Learns the dog's daily routine and detects deviations.

Features:
  - Builds a routine model over 14 days of GPS + health data
  - Detects typical walk times, rest periods, activity baselines
  - Flags missed walks, unusual inactivity, nighttime restlessness
  - HTTP API on localhost:9110/behavior
  - --simulate mode generates training data for development
"""

import json
import logging
import os
import signal
import sys
import threading
import time
from datetime import datetime, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

import yaml
import pandas as pd
import numpy as np

# ─── Paths ──────────────────────────────────────────────────────────────────

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(PROJECT_DIR, "config.yaml")
DATA_DIR = os.path.join(PROJECT_DIR, "data")
GPS_TRACK_DIR = os.path.join(DATA_DIR, "gps_tracks")
HEALTH_LOG_DIR = os.path.join(DATA_DIR, "health_logs")
BEHAVIOR_DIR = os.path.join(DATA_DIR, "behavior")
ZONES_PATH = os.path.join(DATA_DIR, "zones.json")

os.makedirs(BEHAVIOR_DIR, exist_ok=True)

# ─── Default Config ─────────────────────────────────────────────────────────

DEFAULT_CONFIG = {
    "behavior": {
        "learning_days": 14,
        "walk_duration_min": 30,
        "meal_times": ["07:00", "17:00"],
    },
    "dog": {
        "name": "Fido",
        "weight_kg": 30,
        "age_years": 4,
    },
    "geofence": {
        "home_zone": {"lat": 45.5152, "lon": -122.6784, "radius_meters": 50},
    },
}


def load_config():
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH) as f:
            return yaml.safe_load(f) or {}
    return {}


def get_cfg(path, default=None):
    cfg = load_config()
    keys = path.split(".")
    for k in keys:
        if isinstance(cfg, dict):
            cfg = cfg.get(k)
        else:
            return default
    return cfg if cfg is not None else default


# ─── Constants ──────────────────────────────────────────────────────────────

HOME_LAT = get_cfg("geofence.home_zone.lat", 45.5152)
HOME_LON = get_cfg("geofence.home_zone.lon", -122.6784)
HOME_RADIUS_M = get_cfg("geofence.home_zone.radius_meters", 50)
LEARNING_DAYS = get_cfg("behavior.learning_days", 14)
WALK_DURATION_MIN = get_cfg("behavior.walk_duration_min", 30)
MEAL_TIMES = get_cfg("behavior.meal_times", ["07:00", "17:00"])
DOG_NAME = get_cfg("dog.name", "Fido")

BEHAVIOR_API_PORT = 9110
API_BASE = f"http://127.0.0.1:{BEHAVIOR_API_PORT}"
GPS_API = f"http://127.0.0.1:{BEHAVIOR_API_PORT}/gps"
SENSOR_API = f"http://127.0.0.1:{BEHAVIOR_API_PORT}/sensors"

# ─── Helpers ────────────────────────────────────────────────────────────────


def haversine_m(lat1, lon1, lat2, lon2):
    """Haversine distance in meters."""
    R = 6371000
    dlat = np.radians(lat2 - lat1)
    dlon = np.radians(lon2 - lon1)
    a = np.sin(dlat / 2) ** 2 + np.cos(np.radians(lat1)) * np.cos(np.radians(lat2)) * np.sin(dlon / 2) ** 2
    return R * 2 * np.arctan2(np.sqrt(a), np.sqrt(1 - a))


def is_outside_home(lat, lon):
    """Check if a position is outside the home geofence."""
    return haversine_m(lat, lon, HOME_LAT, HOME_LON) > HOME_RADIUS_M


# ─── Routine Model ──────────────────────────────────────────────────────────


class RoutineModel:
    """
    Builds and stores a model of the dog's daily routine.

    The model tracks:
      - Walk times (when dog leaves/returns from home zone)
      - Rest periods (sustained inactivity)
      - Activity baselines (avg accel magnitude by hour of day)
      - Meal times
    """

    def __init__(self):
        self.learning_days = LEARNING_DAYS
        self.walk_log = []  # list of {"start": datetime, "end": datetime, "duration_min": float}
        self.rest_periods = []  # list of {"start": datetime, "end": datetime}
        self.hourly_activity = {}  # hour -> list of mean accel magnitudes
        self.meal_times = self._parse_meal_times()
        self.last_build_time = None
        self.ready = False  # True when sufficient data has been gathered

    def _parse_meal_times(self):
        """Parse configured meal times into hour:int list."""
        hours = []
        for t in MEAL_TIMES:
            try:
                h = int(t.split(":")[0])
                hours.append(h)
            except (ValueError, IndexError):
                pass
        return hours or [7, 17]

    def load_history(self):
        """Load historical GPS tracks and health logs to build the routine."""
        self.walk_log = []
        self.rest_periods = []
        self.hourly_activity = {}
        day_count = 0

        # Scan GPS track files
        gps_files = sorted([
            f for f in os.listdir(GPS_TRACK_DIR)
            if f.startswith("gps_track_") and f.endswith(".csv")
        ])

        for gps_file in gps_files[-self.learning_days:]:
            try:
                df = pd.read_csv(os.path.join(GPS_TRACK_DIR, gps_file))
                if df.empty:
                    continue
                day_count += 1
                self._analyze_day(df)
            except Exception as e:
                logging.warning(f"Could not process {gps_file}: {e}")

        # Load health logs for activity baselines
        health_files = sorted([
            f for f in os.listdir(HEALTH_LOG_DIR)
            if f.startswith("health_log_") and f.endswith(".csv")
        ])
        for hf in health_files[-self.learning_days:]:
            try:
                df = pd.read_csv(os.path.join(HEALTH_LOG_DIR, hf))
                if df.empty:
                    continue
                self._analyze_activity(df)
            except Exception:
                pass

        self.ready = day_count >= 3  # minimum 3 days for useful model
        if self.ready:
            self._save_model()

        logging.info(
            f"Routine model built from {day_count} days: "
            f"{len(self.walk_log)} walks, {len(self.rest_periods)} rest periods"
        )
        return self.ready

    def _analyze_day(self, df):
        """Extract walk periods from a day's GPS track."""
        if "lat" not in df.columns or "lon" not in df.columns:
            return

        # Determine when dog was outside the home zone
        outside_mask = df.apply(
            lambda row: is_outside_home(row["lat"], row["lon"]), axis=1
        )

        if outside_mask.sum() < 2:
            return  # Never left home

        # Find contiguous outside periods (walks)
        outside_changes = outside_mask.astype(int).diff().fillna(0)
        walk_starts = df.index[outside_changes == 1].tolist()
        walk_ends = df.index[outside_changes == -1].tolist()

        # Pair starts and ends
        for start_idx in walk_starts:
            future_ends = [e for e in walk_ends if e > start_idx]
            if future_ends:
                end_idx = future_ends[0]
                start_time = self._parse_df_timestamp(df.iloc[start_idx])
                end_time = self._parse_df_timestamp(df.iloc[end_idx])
                if start_time and end_time:
                    duration = (end_time - start_time).total_seconds() / 60
                    if duration > 5:  # Ignore < 5 min trips
                        self.walk_log.append({
                            "start": start_time,
                            "end": end_time,
                            "duration_min": round(duration, 1),
                        })

    def _analyze_activity(self, df):
        """Extract activity baselines by hour from health log."""
        if "accel_magnitude" not in df.columns or "timestamp" not in df.columns:
            return

        for _, row in df.iterrows():
            ts = self._parse_df_timestamp(row)
            if ts is None or pd.isna(row.get("accel_magnitude")):
                continue
            hour = ts.hour
            if hour not in self.hourly_activity:
                self.hourly_activity[hour] = []
            self.hourly_activity[hour].append(float(row["accel_magnitude"]))

    def _parse_df_timestamp(self, row):
        """Try multiple timestamp column formats."""
        for col in ["timestamp", "Timestamp", "time"]:
            if col in row.index and row[col]:
                try:
                    return pd.to_datetime(row[col])
                except (ValueError, TypeError):
                    pass
        return None

    def _save_model(self):
        """Serialize the routine model to JSON."""
        model_path = os.path.join(BEHAVIOR_DIR, "routine_model.json")
        serializable = {
            "walks": [
                {
                    "start": w["start"].isoformat() if isinstance(w["start"], datetime) else str(w["start"]),
                    "end": w["end"].isoformat() if isinstance(w["end"], datetime) else str(w["end"]),
                    "duration_min": w["duration_min"],
                }
                for w in self.walk_log
            ],
            "hourly_activity": {
                str(h): {
                    "mean": float(np.mean(v)),
                    "std": float(np.std(v)) if len(v) > 1 else 0,
                    "count": len(v),
                }
                for h, v in self.hourly_activity.items()
            },
            "meal_times": self.meal_times,
            "built_at": datetime.now().isoformat(),
        }
        with open(model_path, "w") as f:
            json.dump(serializable, f, indent=2, default=str)

    def get_expected_walk_times(self):
        """Return expected walk windows based on learned pattern."""
        if not self.walk_log:
            return []

        walk_hours = [w["start"].hour for w in self.walk_log]
        if not walk_hours:
            return []

        # Cluster walk start hours
        hour_counts = {}
        for h in walk_hours:
            hour_counts[h] = hour_counts.get(h, 0) + 1

        # Expect walks at hours that occurred most often
        threshold = max(1, len(self.walk_log) // (len(hour_counts) or 1))
        expected = [h for h, c in hour_counts.items() if c >= threshold]

        return sorted(expected)

    def get_expected_walk_duration(self):
        """Return median walk duration in minutes."""
        durations = [w["duration_min"] for w in self.walk_log if w["duration_min"] > 5]
        return float(np.median(durations)) if durations else WALK_DURATION_MIN

    def get_activity_baseline(self, hour):
        """Return (mean, std) activity for a given hour."""
        data = self.hourly_activity.get(hour, [])
        if data:
            return float(np.mean(data)), float(np.std(data))
        return 1.0, 0.5  # fallback defaults

    def summary(self):
        """Return human-readable summary of the routine."""
        expected_walks = self.get_expected_walk_times()
        avg_duration = self.get_expected_walk_duration()
        active_hours = sorted(self.hourly_activity.keys()) if self.hourly_activity else [6, 7, 8, 17, 18, 19]

        return {
            "ready": self.ready,
            "days_analyzed": min(LEARNING_DAYS, len(self.walk_log) // 2 + 1),
            "walks_learned": len(self.walk_log),
            "expected_walk_times": expected_walks,
            "avg_walk_duration_min": round(avg_duration, 1),
            "meal_times": self.meal_times,
            "active_hours": active_hours,
            "restful_hours": [h for h in range(24) if h not in active_hours],
        }


# ─── Deviation Detector ─────────────────────────────────────────────────────


class DeviationDetector:
    """
    Detects deviations from the learned routine using real-time data.
    """

    def __init__(self, routine_model):
        self.model = routine_model
        self.deviations = []
        self._seen_today = set()

    def check(self, current_pos, current_hr, current_accel_mag):
        """Run all deviation checks and return any new deviations."""
        now = datetime.now()
        hour = now.hour
        new_deviations = []

        # Only check if model is ready
        if not self.model.ready:
            return []

        # 1. Missed walk check (do once per hour)
        check_key = f"walk_{now.strftime('%Y-%m-%d_%H')}"
        if check_key not in self._seen_today:
            expected_hours = self.model.get_expected_walk_times()
            if hour in expected_hours and current_pos:
                lat, lon = current_pos.get("lat"), current_pos.get("lon")
                if lat and lon and not is_outside_home(lat, lon):
                    # It's an expected walk hour but dog is home
                    dev = {
                        "type": "missed_walk",
                        "severity": "medium",
                        "description": f"{DOG_NAME} is at home during expected walk time ({hour}:00)",
                        "timestamp": now.isoformat(),
                    }
                    new_deviations.append(dev)
                    self.deviations.append(dev)
            self._seen_today.add(check_key)

        # 2. Unusual inactivity check (every 30 min)
        if current_accel_mag is not None:
            baseline_mean, baseline_std = self.model.get_activity_baseline(hour)
            threshold = baseline_mean * 0.3  # 30% of baseline
            if current_accel_mag < threshold and 8 <= hour <= 21:
                check_key = f"inactive_{now.strftime('%Y-%m-%d_%H')}"
                if check_key not in self._seen_today:
                    dev = {
                        "type": "unusual_inactivity",
                        "severity": "low",
                        "description": f"Unusually low activity at {hour}:00 (accel={current_accel_mag:.2f}, baseline={baseline_mean:.2f})",
                        "timestamp": now.isoformat(),
                    }
                    new_deviations.append(dev)
                    self.deviations.append(dev)
                    self._seen_today.add(check_key)

        # 3. Nighttime restlessness (11pm-5am)
        if hour >= 23 or hour <= 5:
            if current_accel_mag and current_accel_mag > 1.5:
                check_key = f"restless_{now.strftime('%Y-%m-%d_%H')}"
                if check_key not in self._seen_today:
                    dev = {
                        "type": "nighttime_restlessness",
                        "severity": "medium",
                        "description": f"{DOG_NAME} is restless at {hour}:00 (accel={current_accel_mag:.2f})",
                        "timestamp": now.isoformat(),
                    }
                    new_deviations.append(dev)
                    self.deviations.append(dev)
                    self._seen_today.add(check_key)

        # 4. High heart rate during rest
        if current_hr:
            if int(current_hr) > 120 and current_accel_mag and current_accel_mag < 0.3:
                dev = {
                    "type": "elevated_resting_hr",
                    "severity": "high",
                    "description": f"Elevated heart rate during rest: {current_hr} bpm",
                    "timestamp": now.isoformat(),
                }
                new_deviations.append(dev)
                self.deviations.append(dev)

        # Clean up old seen keys (older than 24h)
        today = now.strftime("%Y-%m-%d")
        self._seen_today = {k for k in self._seen_today if today in k}

        return new_deviations

    def get_today_deviations(self):
        """Return deviations from today only."""
        today = datetime.now().strftime("%Y-%m-%d")
        return [d for d in self.deviations if d["timestamp"].startswith(today)]


# ─── Daily Summary Generator ────────────────────────────────────────────────


class DailySummary:
    """Generates natural-language daily summaries."""

    @staticmethod
    def generate(routine_model, sensors_data=None, gps_data=None):
        now = datetime.now()
        parts = []

        # Total walks today
        num_walks = len(routine_model.walk_log)
        if num_walks > 0:
            today_walks = [
                w for w in routine_model.walk_log
                if w["start"].strftime("%Y-%m-%d") == now.strftime("%Y-%m-%d")
            ]
            if today_walks:
                total_duration = sum(w["duration_min"] for w in today_walks)
                parts.append(f"{DOG_NAME} had {len(today_walks)} walk(s) totaling {total_duration:.0f} minutes.")
            else:
                parts.append(f"No walks recorded today yet.")

        # Activity level
        if sensors_data and sensors_data.get("accel_magnitude"):
            mag = sensors_data["accel_magnitude"]
            if mag < 0.5:
                parts.append(f"Current activity level is low ({mag:.2f} G) — resting quietly.")
            elif mag < 1.5:
                parts.append(f"Current activity level is moderate ({mag:.2f} G) — moving around.")
            else:
                parts.append(f"Current activity level is high ({mag:.2f} G) — very active!")

        # Heart rate
        if sensors_data and sensors_data.get("heart_rate"):
            hr = sensors_data["heart_rate"]
            parts.append(f"Heart rate: {hr:.0f} bpm.")

        # Temperature
        if sensors_data and sensors_data.get("temperature_c"):
            temp = sensors_data["temperature_c"]
            parts.append(f"Temperature: {temp:.1f}°C.")

        # GPS
        if gps_data and gps_data.get("lat"):
            lat, lon = gps_data["lat"], gps_data["lon"]
            at_home = "at home" if not is_outside_home(float(lat), float(lon)) else "away from home"
            parts.append(f"Currently {at_home}.")

        if not parts:
            parts.append(f"No data available for {DOG_NAME} yet.")

        return " ".join(parts)


# ─── HTTP API Handler ───────────────────────────────────────────────────────


class BehaviorHTTPHandler(BaseHTTPRequestHandler):
    """HTTP request handler for the behavior API."""

    # Shared state set by the server
    routine_model = None
    deviation_detector = None

    def log_message(self, format, *args):
        logging.debug(f"HTTP: {format % args}")

    def _send_json(self, data, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data, indent=2, default=str).encode())

    def _send_error(self, status, message):
        self._send_json({"error": message}, status)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")

        if path == "/behavior" or path == "/behavior/routine":
            self._send_json(self.routine_model.summary() if self.routine_model else {"ready": False})

        elif path == "/behavior/deviations":
            devs = self.deviation_detector.get_today_deviations() if self.deviation_detector else []
            self._send_json({"deviations": devs, "count": len(devs)})

        elif path == "/behavior/summary":
            summary = DailySummary.generate(self.routine_model)
            self._send_json({
                "summary": summary,
                "routine": self.routine_model.summary() if self.routine_model else {"ready": False},
            })

        elif path == "/behavior/health":
            self._send_json({
                "status": "ok",
                "routine_ready": self.routine_model.ready if self.routine_model else False,
                "walks_learned": len(self.routine_model.walk_log) if self.routine_model else 0,
            })

        else:
            self._send_error(404, f"Unknown endpoint: {path}")

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()


# ─── Simulator ──────────────────────────────────────────────────────────────


class BehaviorSimulator:
    """Generates simulated GPS tracks and health logs for testing."""

    def __init__(self):
        self.home_lat = HOME_LAT
        self.home_lon = HOME_LON

    def generate_training_data(self, days=14):
        """Generate N days of realistic GPS + health data."""
        logging.info(f"Generating {days} days of simulated training data...")
        for day_offset in range(days):
            date = (datetime.now() - timedelta(days=days - day_offset)).strftime("%Y-%m-%d")
            self._generate_gps_day(date)
            self._generate_health_day(date)
        logging.info(f"Generated {days} days of simulated data.")

    def _generate_gps_day(self, date):
        """Generate a day's GPS track with walks at expected times."""
        filepath = os.path.join(GPS_TRACK_DIR, f"gps_track_{date}.csv")
        rows = []
        np.random.seed(abs(hash(date)) % 10000)

        for hour in range(6, 23):
            for minute in range(0, 60, 5):
                ts = f"{date}T{hour:02d}:{minute:02d}:00"

                # Dog is home most of the time
                if hour in [8, 17]:  # Walk times
                    walk_progress = minute / 30.0
                    if walk_progress <= 1.0:
                        offset_m = 20 + (walk_progress * 300)  # Walk out 0-300m
                        angle = np.random.uniform(0, 2 * np.pi)
                        dlat = offset_m * np.cos(angle) / 111320
                        dlon = offset_m * np.sin(angle) / (111320 * np.cos(np.radians(self.home_lat)))
                        lat = self.home_lat + dlat
                        lon = self.home_lon + dlon
                        speed = 0.5 + np.random.random() * 1.5
                    else:
                        lat, lon = self.home_lat, self.home_lon
                        speed = 0
                else:
                    lat, lon = self.home_lat, self.home_lon
                    speed = 0

                rows.append({
                    "timestamp": ts,
                    "lat": round(lat + np.random.normal(0, 0.00001), 6),
                    "lon": round(lon + np.random.normal(0, 0.00001), 6),
                    "altitude": round(50 + np.random.normal(0, 2), 1),
                    "speed": round(speed, 2),
                    "heading": round(np.random.uniform(0, 360), 1) if speed > 0 else 0,
                    "fix_quality": 1,
                    "satellites": int(8 + np.random.randint(0, 5)),
                })

        df = pd.DataFrame(rows)
        df.to_csv(filepath, index=False)
        logging.debug(f"  GPS track: {filepath} ({len(rows)} rows)")

    def _generate_health_day(self, date):
        """Generate a day's health log with realistic vitals."""
        filepath = os.path.join(HEALTH_LOG_DIR, f"health_log_{date}.csv")
        rows = []
        np.random.seed(abs(hash(date)) % 10000)

        for hour in range(6, 23):
            for minute in range(0, 60, 5):
                ts = f"{date}T{hour:02d}:{minute:02d}:00"

                # Activity-dependent vitals
                if hour in [8, 17]:  # Walk time — higher HR/movement
                    is_at_home = minute >= 30
                else:
                    is_at_home = True

                if is_at_home:
                    hr = int(65 + np.random.normal(0, 5))
                    temp = round(38.3 + np.random.normal(0, 0.1), 2)
                    accel_mag = round(0.2 + np.random.random() * 0.3, 3)
                else:
                    hr = int(90 + np.random.normal(0, 15))
                    temp = round(38.6 + np.random.normal(0, 0.15), 2)
                    accel_mag = round(1.0 + np.random.random() * 1.5, 3)

                rows.append({
                    "timestamp": ts,
                    "heart_rate": max(40, min(200, hr)),
                    "temperature_c": round(max(37, min(41, temp)), 2),
                    "accel_magnitude": accel_mag,
                    "speed_ms": 0 if is_at_home else round(0.5 + np.random.random() * 1.5, 2),
                })

        df = pd.DataFrame(rows)
        df.to_csv(filepath, index=False)
        logging.debug(f"  Health log: {filepath} ({len(rows)} rows)")


# ─── Main Daemon ────────────────────────────────────────────────────────────


class BehaviorDaemon:
    """Main daemon that runs the behavior analysis loop."""

    def __init__(self, simulate=False):
        self.simulate = simulate
        self.running = True
        self.model = RoutineModel()
        self.detector = DeviationDetector(self.model)
        self.server = None
        self.server_thread = None

        # Simulation data
        self._fake_pos = {"lat": HOME_LAT, "lon": HOME_LON, "altitude": 50, "speed": 0}
        self._fake_hr = 70
        self._fake_accel = 0.3
        self._sim_start = time.time()

        if simulate:
            self._setup_simulation()

    def _setup_simulation(self):
        """Generate training data and start fake position walk cycle."""
        self.simulator = BehaviorSimulator()
        self.simulator.generate_training_data(LEARNING_DAYS)

    def start(self):
        """Start the daemon: load model, start HTTP server, run check loop."""
        # Load or build routine model
        model_path = os.path.join(BEHAVIOR_DIR, "routine_model.json")
        if os.path.exists(model_path):
            # Model already built, try loading from saved data
            logging.info("Loading existing routine model...")
            self.model.load_history()
        else:
            logging.info("Building routine model from historical data...")
            self.model.load_history()

        # Start HTTP server
        self._start_http()

        # Run check loop
        check_interval = 300  # every 5 minutes
        logging.info(f"Starting behavior check loop (every {check_interval}s)...")
        while self.running:
            try:
                current_pos = None
                current_hr = None
                current_accel = None

                if self.simulate:
                    # Update fake position — cycle where dog is
                    elapsed = time.time() - self._sim_start
                    cycle_pos = (elapsed % 7200) / 7200  # 2-hour cycle
                    if 0.15 < cycle_pos < 0.45:  # Walking period
                        progress = (cycle_pos - 0.15) / 0.3
                        self._fake_pos["lat"] = HOME_LAT + 0.002 * np.sin(progress * 2 * np.pi)
                        self._fake_pos["lon"] = HOME_LON + 0.003 * np.cos(progress * 2 * np.pi)
                        self._fake_pos["speed"] = 1.5
                        self._fake_hr = 85 + 20 * np.sin(progress * 2 * np.pi)
                        self._fake_accel = 1.0 + 1.0 * np.sin(progress * 2 * np.pi)
                    else:
                        self._fake_pos = {"lat": HOME_LAT, "lon": HOME_LON, "altitude": 50, "speed": 0}
                        self._fake_hr = 65 + 5 * np.sin(elapsed / 1800)
                        self._fake_accel = 0.2 + 0.2 * np.sin(elapsed / 600)

                    current_pos = self._fake_pos
                    current_hr = self._fake_hr
                    current_accel = self._fake_accel

                # Run deviation checks
                new_devs = self.detector.check(current_pos, current_hr, current_accel)
                for dev in new_devs:
                    logging.info(f"Deviation: [{dev['severity']}] {dev['description']}")

                # Sleep with interrupt check
                for _ in range(check_interval):
                    if not self.running:
                        break
                    time.sleep(1)

            except Exception as e:
                logging.error(f"Check loop error: {e}")
                time.sleep(30)

    def _start_http(self):
        """Start the HTTP API server in a daemon thread."""
        BehaviorHTTPHandler.routine_model = self.model
        BehaviorHTTPHandler.deviation_detector = self.detector

        self.server = HTTPServer(("127.0.0.1", BEHAVIOR_API_PORT), BehaviorHTTPHandler)
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()
        logging.info(f"Behavior API running on http://127.0.0.1:{BEHAVIOR_API_PORT}")

    def stop(self):
        """Graceful shutdown."""
        logging.info("Shutting down behavior daemon...")
        self.running = False
        if self.server:
            self.server.shutdown()
        logging.info("Behavior daemon stopped.")


# ─── Entry Point ────────────────────────────────────────────────────────────


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Dog Agent — Behavioral Analysis Module")
    parser.add_argument("--simulate", action="store_true", help="Run with simulated data")
    parser.add_argument("--generate-data", type=int, metavar="DAYS", help="Generate N days of training data and exit")
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose logging")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    if args.generate_data:
        sim = BehaviorSimulator()
        sim.generate_training_data(args.generate_data)
        logging.info(f"Generated {args.generate_data} days of training data.")
        return

    daemon = BehaviorDaemon(simulate=args.simulate)

    def signal_handler(sig, frame):
        logging.info(f"Received signal {sig}")
        daemon.stop()

    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    try:
        daemon.start()
    except KeyboardInterrupt:
        daemon.stop()

    logging.info("Exiting.")


if __name__ == "__main__":
    main()