#!/usr/bin/env python3
"""
Health Monitor — Dog Agent
===========================
Reads sensor vitals from the sensor daemon's HTTP API (localhost:9110/sensors),
reads GPS data from the GPS daemon's HTTP API (localhost:9110/gps), analyzes
vitals against configurable thresholds, detects anomalies (high/low heart rate,
fever, hypothermia, inactivity), maintains a rolling 5-minute window of vitals
for trend analysis, and exposes a /health summary endpoint on localhost:9110/health.

Usage:
    python src/health_monitor.py               # Normal mode (reads from daemon APIs)
    python src/health_monitor.py --simulate     # Simulate mode (generates realistic data)
    python src/health_monitor.py --config /path/to/config.yaml
    python src/health_monitor.py --port 9111    # Custom API listen port

Requires:
    - sensor_daemon running on localhost:9110 (or --simulate)
    - gps_daemon running on localhost:9110    (or --simulate)
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import os
import random
import signal
import sys
import threading
import time
from collections import deque
from contextlib import suppress
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from statistics import linear_regression
from typing import Any, Dict, List, Optional, Tuple

import yaml

try:
    import requests
except ImportError:
    requests = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logger = logging.getLogger("health_monitor")
_handler = logging.StreamHandler(sys.stdout)
_handler.setFormatter(logging.Formatter(
    "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
))
logger.addHandler(_handler)
logger.setLevel(logging.INFO)


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------
class VitalsReading:
    """A single point-in-time vitals snapshot."""

    __slots__ = (
        "timestamp", "heart_rate_bpm", "temperature_c",
        "accel_magnitude_g", "speed_mps", "lat", "lon",
        "sensors_valid", "gps_valid",
    )

    def __init__(
        self,
        timestamp: datetime,
        heart_rate_bpm: float,
        temperature_c: float,
        accel_magnitude_g: float,
        speed_mps: float = 0.0,
        lat: float = 0.0,
        lon: float = 0.0,
        sensors_valid: bool = False,
        gps_valid: bool = False,
    ) -> None:
        self.timestamp = timestamp
        self.heart_rate_bpm = heart_rate_bpm
        self.temperature_c = temperature_c
        self.accel_magnitude_g = accel_magnitude_g
        self.speed_mps = speed_mps
        self.lat = lat
        self.lon = lon
        self.sensors_valid = sensors_valid
        self.gps_valid = gps_valid

    def to_dict(self) -> Dict[str, Any]:
        return {
            "timestamp": self.timestamp.isoformat(),
            "heart_rate_bpm": self.heart_rate_bpm,
            "temperature_c": self.temperature_c,
            "temperature_f": round(self.temperature_c * 9.0 / 5.0 + 32.0, 1),
            "accel_magnitude_g": self.accel_magnitude_g,
            "speed_mps": self.speed_mps,
            "lat": self.lat,
            "lon": self.lon,
            "sensors_valid": self.sensors_valid,
            "gps_valid": self.gps_valid,
        }

    def to_csv_row(self) -> Dict[str, Any]:
        d = self.to_dict()
        d["timestamp"] = self.timestamp.isoformat()
        return d


class HealthAlert:
    """A single active or cleared alert."""

    __slots__ = ("alert_type", "severity", "message", "started_at", "cleared_at")

    SEVERITY_INFO = "info"
    SEVERITY_WARNING = "warning"
    SEVERITY_CRITICAL = "critical"

    def __init__(
        self,
        alert_type: str,
        severity: str,
        message: str,
        started_at: Optional[datetime] = None,
    ) -> None:
        self.alert_type = alert_type
        self.severity = severity
        self.message = message
        self.started_at = started_at or datetime.now(timezone.utc)
        self.cleared_at: Optional[datetime] = None

    def clear(self) -> None:
        self.cleared_at = datetime.now(timezone.utc)

    def is_active(self) -> bool:
        return self.cleared_at is None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "type": self.alert_type,
            "severity": self.severity,
            "message": self.message,
            "started_at": self.started_at.isoformat(),
            "cleared_at": self.cleared_at.isoformat() if self.cleared_at else None,
            "active": self.is_active(),
        }


# ---------------------------------------------------------------------------
# Simulated vitals generator
# ---------------------------------------------------------------------------
class SimulatedVitals:
    """Generates realistic fake vitals data for development without hardware
    or running daemons.

    Produces plausible values:
      - Heart rate: 60–120 BPM with occasional spikes (activity) or dips (rest)
      - Temperature: 37.5–39.5 °C (normal canine range)
      - Accelerometer magnitude: ~0.98–1.2 G at rest, up to 2.5 G when active
      - GPS speed: 0–2.5 m/s (walking pace)
    """

    def __init__(self, seed: int = 42) -> None:
        self._rng = random.Random(seed)
        self._t0 = time.monotonic()
        self._hr_baseline = 70.0
        self._temp_baseline = 38.5
        self._hr_target = 70.0
        self._temp_target = 38.5
        self._activity_level = 0.0  # 0.0 = rest, 1.0 = active

    def read(self) -> Dict[str, float]:
        """Return a dict of simulated vitals."""

        elapsed = time.monotonic() - self._t0

        # -- Activity state --
        # Simulate bursts of activity (walking, running) every ~30-90 seconds
        if self._rng.random() < 0.03:
            self._activity_level = self._rng.uniform(0.0, 1.0)
        self._activity_level += (0.0 - self._activity_level) * 0.02  # decay
        self._activity_level = max(0.0, min(1.0, self._activity_level))

        # -- Heart rate --
        base_hr = 65.0 + self._activity_level * 60.0  # 65-125 BPM based on activity
        if self._rng.random() < 0.01:
            # Brief spike from excitement
            base_hr += self._rng.uniform(15.0, 40.0)
        self._hr_baseline += (base_hr - self._hr_baseline) * 0.1
        hr_noise = self._rng.gauss(0, 2.0)
        heart_rate = max(35.0, min(200.0, self._hr_baseline + hr_noise))

        # -- Temperature --
        base_temp = 38.2 + self._activity_level * 0.8  # 38.2-39.0 °C
        temp_osc = 0.2 * math.sin(elapsed * 0.008)
        self._temp_baseline += (base_temp - self._temp_baseline) * 0.05
        temp_noise = self._rng.gauss(0, 0.04)
        temperature = max(36.5, min(40.5, self._temp_baseline + temp_osc + temp_noise))

        # -- Accelerometer magnitude --
        # ~1G at rest (gravity), up to ~2.5G when active
        accel_mag = 0.98 + self._activity_level * 1.2 + self._rng.gauss(0, 0.05)

        # -- Speed --
        # ~0 m/s at rest, up to 2.5 m/s walking
        speed = self._activity_level * self._rng.uniform(0.5, 2.5)

        return {
            "heart_rate_bpm": round(heart_rate, 1),
            "temperature_c": round(temperature, 2),
            "accel_magnitude_g": round(accel_mag, 3),
            "speed_mps": round(speed, 2),
            "lat": 45.5152 + self._rng.gauss(0, 0.0001),
            "lon": -122.6784 + self._rng.gauss(0, 0.0001),
            "sensors_valid": True,
            "gps_valid": True,
        }


# ---------------------------------------------------------------------------
# Vitals store — rolling window + alerts
# ---------------------------------------------------------------------------
class VitalsStore:
    """Thread-safe store for vitals readings and alerts.

    Maintains:
      - A rolling 5-minute window of ``VitalsReading`` objects (``deque``)
      - A list of active and recent ``HealthAlert`` objects
    """

    def __init__(self, max_window_seconds: int = 300) -> None:
        self._lock = threading.Lock()
        # Rolling window — maxlen is generous; we prune by time on read
        self._readings: deque = deque()
        self._max_window_seconds = max_window_seconds
        self._alerts: List[HealthAlert] = []
        # Cache current vitals snapshot for instant /health responses
        self._latest: Optional[VitalsReading] = None

    def add_reading(self, reading: VitalsReading) -> None:
        """Add a new reading and prune old entries."""
        with self._lock:
            self._readings.append(reading)
            self._latest = reading
            self._prune()

    def _prune(self) -> None:
        """Remove readings older than the rolling window."""
        cutoff = datetime.now(timezone.utc).timestamp() - self._max_window_seconds
        while self._readings and self._readings[0].timestamp.timestamp() < cutoff:
            self._readings.popleft()

    def get_window(self) -> List[VitalsReading]:
        """Return all readings within the rolling window."""
        with self._lock:
            self._prune()
            return list(self._readings)

    def get_latest(self) -> Optional[VitalsReading]:
        with self._lock:
            return self._latest

    def add_alert(self, alert: HealthAlert) -> None:
        with self._lock:
            self._alerts.append(alert)
            logger.warning(
                "ALERT [%s/%s] %s",
                alert.severity, alert.alert_type, alert.message,
            )

    def clear_alert(self, alert_type: str) -> None:
        """Mark all active alerts of *alert_type* as cleared."""
        with self._lock:
            for alert in self._alerts:
                if alert.alert_type == alert_type and alert.is_active():
                    alert.clear()
                    logger.info(
                        "ALERT CLEARED [%s] %s",
                        alert.alert_type, alert.message,
                    )

    def get_active_alerts(self) -> List[HealthAlert]:
        with self._lock:
            return [a for a in self._alerts if a.is_active()]

    def get_recent_alerts(self, max_age_sec: int = 3600) -> List[HealthAlert]:
        """Return alerts from the last *max_age_sec* seconds."""
        cutoff = datetime.now(timezone.utc).timestamp() - max_age_sec
        with self._lock:
            return [
                a for a in self._alerts
                if a.started_at.timestamp() >= cutoff
            ]

    def compute_trend(self) -> str:
        """Analyze heart rate trend over the rolling window.

        Returns one of: ``"stable"``, ``"rising"``, ``"falling"``.

        Uses linear regression on heart rate over time. If fewer than 3
        readings exist, returns ``"stable"``.
        """
        readings = self.get_window()
        if len(readings) < 3:
            return "stable"

        # Build data for linear regression: x = relative seconds, y = HR
        t0 = readings[0].timestamp.timestamp()
        x = [r.timestamp.timestamp() - t0 for r in readings]
        y = [r.heart_rate_bpm for r in readings]

        try:
            slope, intercept = linear_regression(x, y)
        except StatisticsError:
            return "stable"

        # Fuzzy thresholds: slope > 0.5 bpm/min → rising, < -0.5 → falling
        slope_bpm_per_min = slope * 60.0
        if slope_bpm_per_min > 0.5:
            return "rising"
        elif slope_bpm_per_min < -0.5:
            return "falling"
        else:
            return "stable"

    def get_inactivity_minutes(self, speed_threshold_mps: float = 0.3) -> float:
        """Estimate how many minutes the dog has been inactive (speed < threshold)."""
        readings = self.get_window()
        if not readings:
            return 0.0

        inactive_seconds = 0.0
        for r in reversed(readings):
            if r.speed_mps < speed_threshold_mps and r.accel_magnitude_g < 1.15:
                inactive_seconds += 1.0  # each reading approximates 1 second
            else:
                break  # stop at first movement

        # Refine: compute actual time span of trailing inactive readings
        if inactive_seconds > 0:
            inactive_start = readings[-1].timestamp
            for r in reversed(readings):
                if r.speed_mps < speed_threshold_mps and r.accel_magnitude_g < 1.15:
                    inactive_start = r.timestamp
                else:
                    break
            elapsed = (datetime.now(timezone.utc) - inactive_start).total_seconds()
            return elapsed / 60.0

        return 0.0


# ---------------------------------------------------------------------------
# CSV Logger
# ---------------------------------------------------------------------------
class HealthCsvLogger:
    """Logs vitals readings to a daily CSV file."""

    FIELD_NAMES = [
        "timestamp", "heart_rate_bpm", "temperature_c", "temperature_f",
        "accel_magnitude_g", "speed_mps", "lat", "lon",
        "sensors_valid", "gps_valid",
    ]

    def __init__(self, directory: str) -> None:
        self._directory = Path(directory)
        self._directory.mkdir(parents=True, exist_ok=True)
        self._file: Optional[Any] = None
        self._writer: Optional[csv.DictWriter] = None
        self._current_date: Optional[str] = None
        self._lock = threading.Lock()

    def write(self, reading: VitalsReading) -> None:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        with self._lock:
            if today != self._current_date:
                self._rotate(today)
            if self._writer:
                self._writer.writerow(reading.to_csv_row())
                self._file.flush()

    def _rotate(self, today: str) -> None:
        if self._file:
            self._file.close()
        filepath = self._directory / f"health_log_{today}.csv"
        self._file = open(filepath, "a", newline="")  # noqa: SIM115
        self._writer = csv.DictWriter(self._file, fieldnames=self.FIELD_NAMES)
        if filepath.stat().st_size == 0:
            self._writer.writeheader()
        self._current_date = today

    def close(self) -> None:
        with self._lock:
            if self._file:
                self._file.close()
                self._file = None
            self._writer = None


# ---------------------------------------------------------------------------
# Anomaly detector
# ---------------------------------------------------------------------------
class AnomalyDetector:
    """Checks a vitals reading against configurable thresholds and returns
    any triggered alerts."""

    def __init__(self, config: Dict[str, Any]) -> None:
        health_cfg = config.get("health", {})
        dog_cfg = config.get("dog", {})

        self.hr_alert_high: float = health_cfg.get("hr_alert_high", 160)
        self.hr_alert_low: float = health_cfg.get("hr_alert_low", 40)
        self.temp_alert_high: float = health_cfg.get("temp_alert_high", 40.0)
        self.temp_alert_low: float = health_cfg.get("temp_alert_low", 37.0)
        self.inactivity_alert_min: float = health_cfg.get("inactivity_alert_min", 180)

        # Dog-specific reference values (for context, not threshold enforcement)
        self.resting_hr: float = dog_cfg.get("resting_hr_bpm", 70)
        self.normal_temp: float = dog_cfg.get("normal_temp_c", 38.5)
        self.dog_name: str = dog_cfg.get("name", "Dog")

    def check(self, reading: VitalsReading, store: VitalsStore) -> List[HealthAlert]:
        """Evaluate *reading* against thresholds and return any new alerts.

        Compares against previously triggered alerts in *store* to avoid
        re-triggering the same alert on every check cycle.

        Returns a list of *newly* triggered ``HealthAlert`` objects.
        """
        new_alerts: List[HealthAlert] = []
        active = {a.alert_type for a in store.get_active_alerts()}

        # -- High heart rate --
        if reading.heart_rate_bpm > self.hr_alert_high and "hr_high" not in active:
            new_alerts.append(HealthAlert(
                alert_type="hr_high",
                severity=HealthAlert.SEVERITY_CRITICAL,
                message=(
                    f"{self.dog_name}'s heart rate is critically high: "
                    f"{reading.heart_rate_bpm:.0f} bpm (threshold: {self.hr_alert_high:.0f})"
                ),
            ))
        elif reading.heart_rate_bpm <= self.hr_alert_high - 10 and "hr_high" in active:
            store.clear_alert("hr_high")

        # -- Low heart rate --
        if reading.heart_rate_bpm < self.hr_alert_low and "hr_low" not in active:
            new_alerts.append(HealthAlert(
                alert_type="hr_low",
                severity=HealthAlert.SEVERITY_CRITICAL,
                message=(
                    f"{self.dog_name}'s heart rate is critically low: "
                    f"{reading.heart_rate_bpm:.0f} bpm (threshold: {self.hr_alert_low:.0f})"
                ),
            ))
        elif reading.heart_rate_bpm >= self.hr_alert_low + 10 and "hr_low" in active:
            store.clear_alert("hr_low")

        # -- Fever (high temperature) --
        if reading.temperature_c > self.temp_alert_high and "fever" not in active:
            new_alerts.append(HealthAlert(
                alert_type="fever",
                severity=HealthAlert.SEVERITY_WARNING,
                message=(
                    f"{self.dog_name} has a fever: "
                    f"{reading.temperature_c:.1f} °C (threshold: {self.temp_alert_high:.1f})"
                ),
            ))
        elif reading.temperature_c <= self.temp_alert_high - 0.3 and "fever" in active:
            store.clear_alert("fever")

        # -- Hypothermia (low temperature) --
        if reading.temperature_c < self.temp_alert_low and "hypothermia" not in active:
            new_alerts.append(HealthAlert(
                alert_type="hypothermia",
                severity=HealthAlert.SEVERITY_WARNING,
                message=(
                    f"{self.dog_name} has hypothermia: "
                    f"{reading.temperature_c:.1f} °C (threshold: {self.temp_alert_low:.1f})"
                ),
            ))
        elif reading.temperature_c >= self.temp_alert_low + 0.3 and "hypothermia" in active:
            store.clear_alert("hypothermia")

        # -- Inactivity --
        inactive_min = store.get_inactivity_minutes()
        inactive_key = "inactivity"
        if inactive_min >= self.inactivity_alert_min and inactive_key not in active:
            new_alerts.append(HealthAlert(
                alert_type=inactive_key,
                severity=HealthAlert.SEVERITY_WARNING,
                message=(
                    f"{self.dog_name} has been inactive for "
                    f"{inactive_min:.0f} minutes (threshold: {self.inactivity_alert_min:.0f})"
                ),
            ))
        elif inactive_min < self.inactivity_alert_min * 0.5 and inactive_key in active:
            store.clear_alert(inactive_key)

        return new_alerts


# ---------------------------------------------------------------------------
# Sensor/GPS API fetchers
# ---------------------------------------------------------------------------
def fetch_sensors(api_base: str) -> Optional[Dict[str, Any]]:
    """Fetch sensor readings from the sensor daemon HTTP API.

    Returns a dict with keys ``heart_rate_bpm``, ``temperature_c``,
    ``acceleration`` (nested dict with ``magnitude_g``), ``valid``, or
    ``None`` if the request fails.
    """
    if requests is None:
        logger.error("Cannot fetch sensors: 'requests' library not installed")
        return None

    url = f"{api_base}/sensors"
    try:
        resp = requests.get(url, timeout=3.0)
        resp.raise_for_status()
        data = resp.json()
        return data
    except requests.exceptions.RequestException as exc:
        logger.warning("Failed to fetch sensors from %s: %s", url, exc)
        return None
    except (json.JSONDecodeError, KeyError) as exc:
        logger.warning("Invalid sensor response from %s: %s", url, exc)
        return None


def fetch_gps(api_base: str) -> Optional[Dict[str, Any]]:
    """Fetch GPS position from the GPS daemon HTTP API.

    Returns a dict with keys ``lat``, ``lon``, ``speed_mps``, ``valid``, or
    ``None`` if the request fails.
    """
    if requests is None:
        logger.error("Cannot fetch GPS: 'requests' library not installed")
        return None

    url = f"{api_base}/gps"
    try:
        resp = requests.get(url, timeout=3.0)
        resp.raise_for_status()
        data = resp.json()
        return data
    except requests.exceptions.RequestException as exc:
        logger.warning("Failed to fetch GPS from %s: %s", url, exc)
        return None
    except (json.JSONDecodeError, KeyError) as exc:
        logger.warning("Invalid GPS response from %s: %s", url, exc)
        return None


# ---------------------------------------------------------------------------
# Configuration loader
# ---------------------------------------------------------------------------
def load_config(path: str) -> dict:
    """Load YAML config, returning defaults for missing health keys."""
    with open(path) as f:
        cfg = yaml.safe_load(f) or {}

    health_cfg = cfg.get("health", {})
    dog_cfg = cfg.get("dog", {})
    log_cfg = cfg.get("logging", {})
    hermes_cfg = cfg.get("hermes", {})

    return {
        "dog": dog_cfg,
        "health": health_cfg,
        "health_log_dir": log_cfg.get("health_log_dir", "data/health_logs"),
        "api_port": hermes_cfg.get("api_port", 9110),
        "sensor_api_base": f"http://127.0.0.1:{hermes_cfg.get('api_port', 9110)}",
        "gps_api_base": f"http://127.0.0.1:{hermes_cfg.get('api_port', 9110)}",
    }


# ---------------------------------------------------------------------------
# HTTP API Handler
# ---------------------------------------------------------------------------
class HealthAPIHandler(BaseHTTPRequestHandler):
    """Serves the current health summary as JSON.

    Class-level references set by the server:
      - ``store: VitalsStore``
      - ``detector: AnomalyDetector``
    """

    store: VitalsStore = None  # type: ignore[assignment]
    detector: AnomalyDetector = None  # type: ignore[assignment]

    def do_GET(self) -> None:
        if self.path == "/health":
            self._serve_health()
        elif self.path == "/health/raw":
            self._serve_raw()
        else:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b'{"error": "not found"}')

    def _serve_health(self) -> None:
        """Return the current health summary."""
        latest = self.store.get_latest()
        trend = self.store.compute_trend()
        active_alerts = self.store.get_active_alerts()

        summary = {
            "status": "ok" if not active_alerts else "alert",
            "service": "health_monitor",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "vitals": latest.to_dict() if latest else None,
            "trend": trend,
            "active_alerts_count": len(active_alerts),
            "active_alerts": [a.to_dict() for a in active_alerts],
            "window_readings": len(self.store.get_window()),
        }
        self._json_response(summary)

    def _serve_raw(self) -> None:
        """Return all readings in the current rolling window."""
        readings = [r.to_dict() for r in self.store.get_window()]
        self._json_response({
            "count": len(readings),
            "readings": readings,
        })

    def _json_response(self, data: dict) -> None:
        body = json.dumps(data, indent=2, default=str).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: Any) -> None:
        logger.debug(f"HTTP: {fmt % args}")


# ---------------------------------------------------------------------------
# Health monitor loop
# ---------------------------------------------------------------------------
def health_check_loop(
    store: VitalsStore,
    detector: AnomalyDetector,
    csv_logger: HealthCsvLogger,
    stop_event: threading.Event,
    config: Dict[str, Any],
    simulate: bool = False,
) -> None:
    """Periodically fetch vitals, analyze, log, and update state.

    Args:
        store: Thread-safe vitals store.
        detector: Anomaly detector with thresholds.
        csv_logger: CSV file logger.
        stop_event: Set to signal shutdown.
        config: Loaded configuration dict.
        simulate: If True, generate data locally instead of fetching from APIs.
    """
    interval = config.get("health", {}).get("check_interval_sec", 60)
    sensor_api = config["sensor_api_base"]
    gps_api = config["gps_api_base"]

    # Simulate mode generator
    sim_vitals: Optional[SimulatedVitals] = None
    if simulate:
        sim_vitals = SimulatedVitals()

    while not stop_event.is_set():
        try:
            # --- Fetch or simulate data ---
            if simulate and sim_vitals:
                data = sim_vitals.read()
            else:
                sensor_data = fetch_sensors(sensor_api)
                gps_data = fetch_gps(gps_api)

                if sensor_data is None and gps_data is None:
                    logger.warning(
                        "Cannot reach sensor or GPS daemon APIs. "
                        "Is the orchestrator running?"
                    )
                    time.sleep(interval)
                    continue

                # Merge sensor + GPS into a single vitals record
                now = datetime.now(timezone.utc)

                hr = sensor_data.get("heart_rate_bpm", 0.0) if sensor_data else 0.0
                temp = sensor_data.get("temperature_c", 0.0) if sensor_data else 0.0
                accel_mag = (
                    sensor_data.get("acceleration", {}).get("magnitude_g", 0.0)
                    if sensor_data else 0.0
                )
                sensors_valid = sensor_data.get("valid", False) if sensor_data else False

                speed = gps_data.get("speed_mps", 0.0) if gps_data else 0.0
                lat = gps_data.get("lat", 0.0) if gps_data else 0.0
                lon = gps_data.get("lon", 0.0) if gps_data else 0.0
                gps_valid = gps_data.get("valid", False) if gps_data else False

                data = {
                    "heart_rate_bpm": hr,
                    "temperature_c": temp,
                    "accel_magnitude_g": accel_mag,
                    "speed_mps": speed,
                    "lat": lat,
                    "lon": lon,
                    "sensors_valid": sensors_valid,
                    "gps_valid": gps_valid,
                }

            # --- Build reading ---
            reading = VitalsReading(
                timestamp=datetime.now(timezone.utc),
                heart_rate_bpm=data["heart_rate_bpm"],
                temperature_c=data["temperature_c"],
                accel_magnitude_g=data["accel_magnitude_g"],
                speed_mps=data["speed_mps"],
                lat=data["lat"],
                lon=data["lon"],
                sensors_valid=data["sensors_valid"],
                gps_valid=data["gps_valid"],
            )

            # --- Store ---
            store.add_reading(reading)

            # --- Log to CSV ---
            csv_logger.write(reading)

            # --- Anomaly detection ---
            if reading.sensors_valid:
                new_alerts = detector.check(reading, store)
                for alert in new_alerts:
                    store.add_alert(alert)

            # --- Log summary ---
            trend = store.compute_trend()
            active_count = len(store.get_active_alerts())
            logger.info(
                "HR=%.1f Temp=%.1f°C Accel=%.3fG Speed=%.2fm/s "
                "Trend=%s Alerts=%d Window=%d",
                reading.heart_rate_bpm,
                reading.temperature_c,
                reading.accel_magnitude_g,
                reading.speed_mps,
                trend,
                active_count,
                len(store.get_window()),
            )

        except Exception:
            logger.exception("Unexpected error in health check loop")

        # Wait for next interval (check stop_event periodically)
        for _ in range(max(1, int(interval * 10))):
            if stop_event.is_set():
                return
            time.sleep(0.1)


# ---------------------------------------------------------------------------
# Main daemon
# ---------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(description="Dog Agent Health Monitor")
    parser.add_argument(
        "--config",
        default=None,
        help="Path to config.yaml (default: ../config.yaml relative to this script)",
    )
    parser.add_argument(
        "--simulate",
        action="store_true",
        help="Simulate mode — generate fake vitals data (no sensor/GPS daemons needed)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="HTTP API port (default: from config, usually 9110)",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=None,
        help="Health check interval in seconds (default: from config, usually 60)",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Enable debug logging"
    )
    args = parser.parse_args()

    if args.verbose:
        logger.setLevel(logging.DEBUG)

    # --- Resolve config path ---
    if args.config:
        config_path = args.config
    else:
        script_dir = Path(__file__).resolve().parent
        config_path = str(script_dir.parent / "config.yaml")

    if os.path.exists(config_path):
        cfg = load_config(config_path)
        logger.info("Loaded config from %s", config_path)
    else:
        logger.warning(
            "No config.yaml found at %s; using defaults. "
            "Copy config.example.yaml to config.yaml and edit.",
            config_path,
        )
        cfg = {
            "dog": {"name": "Fido", "resting_hr_bpm": 70, "normal_temp_c": 38.5},
            "health": {
                "check_interval_sec": 60,
                "hr_alert_high": 160,
                "hr_alert_low": 40,
                "temp_alert_high": 40.0,
                "temp_alert_low": 37.0,
                "inactivity_alert_min": 180,
            },
            "health_log_dir": "data/health_logs",
            "api_port": 9110,
            "sensor_api_base": "http://127.0.0.1:9110",
            "gps_api_base": "http://127.0.0.1:9110",
        }

    # --- Override from CLI args ---
    if args.port is not None:
        cfg["api_port"] = args.port
        cfg["sensor_api_base"] = f"http://127.0.0.1:{args.port}"
        cfg["gps_api_base"] = f"http://127.0.0.1:{args.port}"
    if args.interval is not None:
        cfg["health"]["check_interval_sec"] = args.interval

    # --- Resolve health log directory ---
    health_log_dir = cfg["health_log_dir"]
    if not os.path.isabs(health_log_dir):
        script_dir = Path(__file__).resolve().parent
        health_log_dir = str(script_dir.parent / health_log_dir)

    # --- State ---
    store = VitalsStore(max_window_seconds=300)
    detector = AnomalyDetector(cfg)
    csv_logger = HealthCsvLogger(health_log_dir)
    stop_event = threading.Event()

    logger.info(
        "Health monitor starting (simulate=%s, interval=%ss, log=%s)",
        args.simulate,
        cfg["health"]["check_interval_sec"],
        health_log_dir,
    )
    logger.info(
        "Thresholds: HR high=%s low=%s | Temp high=%s low=%s | Inactivity=%s min",
        detector.hr_alert_high,
        detector.hr_alert_low,
        detector.temp_alert_high,
        detector.temp_alert_low,
        detector.inactivity_alert_min,
    )

    # --- Start health check thread ---
    check_thread = threading.Thread(
        target=health_check_loop,
        args=(store, detector, csv_logger, stop_event, cfg, args.simulate),
        name="health-check",
        daemon=True,
    )
    check_thread.start()
    logger.info("Health check thread started")

    # --- Start HTTP API server ---
    HealthAPIHandler.store = store
    HealthAPIHandler.detector = detector
    api_port = cfg["api_port"]
    server = HTTPServer(("127.0.0.1", api_port), HealthAPIHandler)

    try:
        server_thread = threading.Thread(
            target=server.serve_forever,
            name="health-api",
            daemon=True,
        )
        server_thread.start()
        logger.info(
            "Health API server listening on http://127.0.0.1:%d/health",
            api_port,
        )
    except OSError as e:
        logger.error("Failed to start HTTP server on port %d: %s", api_port, e)
        logger.error(
            "Port %d may be in use. Use --port to specify a different port.",
            api_port,
        )
        stop_event.set()
        csv_logger.close()
        sys.exit(1)

    # ------------------------------------------------------------------
    # Graceful shutdown
    # ------------------------------------------------------------------
    def shutdown(signum: int, frame: Any) -> None:
        logger.info("Received signal %d — shutting down...", signum)
        stop_event.set()
        server.shutdown()
        csv_logger.close()
        logger.info("Health monitor stopped.")
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    # Keep main thread alive
    try:
        while not stop_event.is_set():
            time.sleep(1)
    except KeyboardInterrupt:
        shutdown(signal.SIGINT, None)


if __name__ == "__main__":
    main()