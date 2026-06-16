#!/usr/bin/env python3
"""
Adaptive GPS Module — Dog Agent
================================
Extends GPS functionality with intelligent rate control to save power.

Three GPS modes:
- HIGH_RATE: 10 Hz continuous (when away from home)
- LOW_RATE: 1 per 30 seconds (at home, active)
- SLEEP_MODE: 1 per 5 minutes (at home, resting/night)

Automatic transitions based on:
- Location: home vs away (uses geofence API on port 9114)
- Time: day vs night (configurable hours, default 10pm-6am)
- Activity level: from accelerometer (port 9112)
- Battery: switch to lower rate when <30%

Smart wake triggers:
- Accelerometer spike → immediately go HIGH_RATE
- Geofence exit event → immediately go HIGH_RATE
- Scheduled check-in time → briefly wake to HIGH_RATE

HTTP API on port 9124:
- GET /gps/adaptive/status — current mode, rate, next update time
- POST /gps/adaptive/mode — force mode {"mode": "high|low|sleep"}
- GET /gps/adaptive/stats — time in each mode, power saved estimate

Usage:
    python src/adaptive_gps.py               # Normal mode
    python src/adaptive_gps.py --test         # Test mode
    python src/adaptive_gps.py --config /path/to/config.yaml
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
from datetime import datetime, timedelta, timezone
from enum import Enum, auto
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from dataclasses import dataclass, field

import yaml

try:
    import requests
except ImportError:
    requests = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logger = logging.getLogger("adaptive_gps")
_handler = logging.StreamHandler(sys.stdout)
_handler.setFormatter(logging.Formatter(
    "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
))
logger.addHandler(_handler)
logger.setLevel(logging.INFO)


# ---------------------------------------------------------------------------
# GPS Mode Definitions
# ---------------------------------------------------------------------------
class GPSMode(Enum):
    """GPS operating modes with different power characteristics."""
    HIGH_RATE = "high"
    LOW_RATE = "low"
    SLEEP_MODE = "sleep"

    @property
    def display_name(self) -> str:
        return self.value.upper()


@dataclass
class ModeConfig:
    """Configuration for a GPS mode."""
    update_interval_sec: float
    label: str
    power_consumption_ma: float  # Approximate power consumption in mA
    
    @classmethod
    def default_high(cls) -> "ModeConfig":
        return cls(update_interval_sec=0.1, label="HIGH_RATE", power_consumption_ma=45.0)
    
    @classmethod
    def default_low(cls) -> "ModeConfig":
        return cls(update_interval_sec=30.0, label="LOW_RATE", power_consumption_ma=15.0)
    
    @classmethod
    def default_sleep(cls) -> "ModeConfig":
        return cls(update_interval_sec=300.0, label="SLEEP_MODE", power_consumption_ma=3.0)


@dataclass
class ModeStatistics:
    """Tracks time spent in each mode for power calculations."""
    high_rate_seconds: float = 0.0
    low_rate_seconds: float = 0.0
    sleep_seconds: float = 0.0
    mode_entry_times: Dict[GPSMode, Optional[datetime]] = field(default_factory=dict)
    total_fixes: int = 0
    total_wake_triggers: int = 0
    
    def __post_init__(self):
        if not self.mode_entry_times:
            self.mode_entry_times = {mode: None for mode in GPSMode}
    
    def record_mode_entry(self, mode: GPSMode) -> None:
        now = datetime.now(timezone.utc)
        # Update previous mode duration if there was one
        for m, entry_time in self.mode_entry_times.items():
            if entry_time is not None:
                duration = (now - entry_time).total_seconds()
                if m == GPSMode.HIGH_RATE:
                    self.high_rate_seconds += duration
                elif m == GPSMode.LOW_RATE:
                    self.low_rate_seconds += duration
                elif m == GPSMode.SLEEP_MODE:
                    self.sleep_seconds += duration
        self.mode_entry_times[mode] = now
    
    def compute_power_savings(self, baseline_ma: float = 45.0) -> Dict[str, Any]:
        """Calculate estimated power savings compared to always-on HIGH_RATE."""
        total_time = self.high_rate_seconds + self.low_rate_seconds + self.sleep_seconds
        if total_time == 0:
            return {"percent_saved": 0.0, "mah_saved": 0.0, "estimated_battery_life_hours": 0.0}
        
        # Weighted average power consumption
        avg_power = (
            (self.high_rate_seconds * 45.0) +
            (self.low_rate_seconds * 15.0) +
            (self.sleep_seconds * 3.0)
        ) / total_time
        
        percent_saved = ((baseline_ma - avg_power) / baseline_ma) * 100.0
        mah_saved = (baseline_ma - avg_power) * (total_time / 3600.0)  # Convert seconds to hours
        
        # Estimate battery life extension (assuming 10000 mAh battery)
        battery_capacity_mah = 10000.0
        estimated_hours = battery_capacity_mah / avg_power if avg_power > 0 else 0.0
        
        return {
            "percent_saved": round(percent_saved, 1),
            "mah_saved": round(mah_saved, 2),
            "estimated_battery_life_hours": round(estimated_hours, 1),
            "avg_consumption_ma": round(avg_power, 2),
        }
    
    def to_dict(self) -> Dict[str, Any]:
        power_stats = self.compute_power_savings()
        total_time = self.high_rate_seconds + self.low_rate_seconds + self.sleep_seconds
        return {
            "high_rate_seconds": round(self.high_rate_seconds, 1),
            "low_rate_seconds": round(self.low_rate_seconds, 1),
            "sleep_seconds": round(self.sleep_seconds, 1),
            "total_tracked_seconds": round(total_time, 1),
            "total_fixes": self.total_fixes,
            "total_wake_triggers": self.total_wake_triggers,
            "power_savings": power_stats,
        }


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
@dataclass
class AdaptiveGPSConfig:
    """Configuration for the adaptive GPS module."""
    enabled: bool = True
    high_rate_hz: float = 10.0
    low_rate_interval_sec: float = 30.0
    sleep_interval_sec: float = 300.0
    night_start_hour: int = 22
    night_end_hour: int = 6
    activity_threshold: float = 0.5
    battery_low_threshold: float = 30.0
    geofence_api_url: str = "http://127.0.0.1:9114/geofence"
    health_api_url: str = "http://127.0.0.1:9112/health"
    power_api_url: str = "http://127.0.0.1:9120/power"
    gps_daemon_url: str = "http://127.0.0.1:9111/gps"
    api_port: int = 9124
    check_interval_sec: float = 5.0
    accel_spike_threshold: float = 2.0  # G-force threshold for activity spike
    scheduled_check_times: List[str] = field(default_factory=lambda: ["08:00", "12:00", "18:00"])


# ---------------------------------------------------------------------------
# External Data Fetchers
# ---------------------------------------------------------------------------
class ExternalDataFetcher:
    """Fetches data from other dog-agent services."""
    
    def __init__(self, config: AdaptiveGPSConfig) -> None:
        self.config = config
        self._session = requests.Session() if requests else None
        self._session_timeout = 3.0
    
    def _get(self, url: str) -> Optional[Dict[str, Any]]:
        """Make a GET request and return JSON response."""
        if self._session is None:
            return None
        try:
            resp = self._session.get(url, timeout=self._session_timeout)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.debug("Failed to fetch from %s: %s", url, e)
            return None
    
    def get_geofence_status(self) -> Optional[Dict[str, Any]]:
        """Check if dog is at home or away."""
        data = self._get(self.config.geofence_api_url)
        if data is None:
            # Try alternative URL format
            data = self._get(self.config.geofence_api_url.replace("/geofence", "/geofence"))
        return data
    
    def get_health_vitals(self) -> Optional[Dict[str, Any]]:
        """Get health/activity data including accelerometer."""
        return self._get(self.config.health_api_url)
    
    def get_battery_status(self) -> Optional[Dict[str, Any]]:
        """Get battery percentage from power manager."""
        return self._get(self.config.power_api_url)
    
    def is_at_home(self) -> bool:
        """Check if dog is currently at home based on geofence."""
        status = self.get_geofence_status()
        if status is None:
            logger.debug("No geofence data available, assuming away for safety")
            return False
        
        # Check home zone status
        zones = status.get("zones", [])
        for zone in zones:
            if zone.get("name") == "home":
                return zone.get("inside", False)
        
        # Fallback: check home_distance_meters
        home_distance = status.get("home_distance_meters", float('inf'))
        escape_radius = status.get("escape_radius_meters", 200)
        return home_distance < (escape_radius / 2)  # Consider home if within half escape radius
    
    def get_activity_level(self) -> float:
        """Get current activity level from accelerometer (0.0 to 1.0+)."""
        vitals = self.get_health_vitals()
        if vitals is None:
            return 0.0
        
        vitals_data = vitals.get("vitals", {})
        accel_mag = vitals_data.get("accel_magnitude_g", 1.0)
        speed = vitals_data.get("speed_mps", 0.0)
        
        # Normalize activity: 1.0 = resting, >1.0 = moving
        # Typical resting accel is ~1.0G (gravity), movement adds variation
        activity = max(0.0, abs(accel_mag - 1.0) * 2.0)  # Deviation from 1G
        activity += min(speed / 2.0, 1.0)  # Speed contribution
        
        return min(activity, 2.0)  # Cap at 2.0
    
    def get_battery_percent(self) -> Optional[float]:
        """Get current battery percentage."""
        power = self.get_battery_status()
        if power is None:
            return None
        return power.get("battery_percent")
    
    def check_accel_spike(self) -> bool:
        """Check if there's an accelerometer spike indicating sudden movement."""
        vitals = self.get_health_vitals()
        if vitals is None:
            return False
        
        vitals_data = vitals.get("vitals", {})
        accel_mag = vitals_data.get("accel_magnitude_g", 1.0)
        
        # Spike if significantly above resting 1G
        return accel_mag > self.config.accel_spike_threshold


# ---------------------------------------------------------------------------
# GPS Mode Controller
# ---------------------------------------------------------------------------
class GPSModeController:
    """Manages GPS mode transitions with state machine logic."""
    
    def __init__(self, config: AdaptiveGPSConfig) -> None:
        self.config = config
        self._lock = threading.RLock()
        self._current_mode: GPSMode = GPSMode.HIGH_RATE
        self._forced_mode: Optional[GPSMode] = None
        self._statistics = ModeStatistics()
        self._last_update_time: Optional[datetime] = None
        self._next_scheduled_wake: Optional[datetime] = None
        self._last_accel_spike: Optional[datetime] = None
        self._last_geofence_exit: Optional[datetime] = None
        
        # Initialize mode entry tracking
        self._statistics.record_mode_entry(self._current_mode)
    
    @property
    def current_mode(self) -> GPSMode:
        with self._lock:
            return self._current_mode
    
    def force_mode(self, mode: Optional[GPSMode]) -> None:
        """Force a specific mode or reset to automatic."""
        with self._lock:
            self._forced_mode = mode
            if mode is not None and mode != self._current_mode:
                self._transition_to(mode)
    
    def get_forced_mode(self) -> Optional[GPSMode]:
        with self._lock:
            return self._forced_mode
    
    def _transition_to(self, new_mode: GPSMode) -> None:
        """Execute mode transition."""
        if new_mode == self._current_mode:
            return
        
        logger.info("GPS mode transition: %s → %s", 
                    self._current_mode.display_name, new_mode.display_name)
        
        self._current_mode = new_mode
        self._statistics.record_mode_entry(new_mode)
    
    def is_night_time(self) -> bool:
        """Check if current time is within night hours."""
        now = datetime.now(timezone.utc)
        hour = now.hour
        
        if self.config.night_start_hour > self.config.night_end_hour:
            # Night spans midnight (e.g., 22:00 to 06:00)
            return hour >= self.config.night_start_hour or hour < self.config.night_end_hour
        else:
            # Day spans midnight (rare case)
            return self.config.night_start_hour <= hour < self.config.night_end_hour
    
    def is_scheduled_check_time(self) -> bool:
        """Check if current time matches a scheduled check-in time."""
        now = datetime.now(timezone.utc)
        current_time = now.strftime("%H:%M")
        
        for check_time in self.config.scheduled_check_times:
            # Check if within 1 minute of scheduled time
            try:
                scheduled = datetime.strptime(check_time, "%H:%M").replace(
                    year=now.year, month=now.month, day=now.day,
                    tzinfo=timezone.utc
                )
                time_diff = abs((now - scheduled).total_seconds())
                if time_diff < 60:  # Within 1 minute
                    return True
            except ValueError:
                continue
        return False
    
    def should_wake_for_scheduled(self) -> bool:
        """Check if we should briefly wake for a scheduled check."""
        if self._next_scheduled_wake is None:
            return False
        now = datetime.now(timezone.utc)
        return now >= self._next_scheduled_wake
    
    def record_accel_spike(self) -> None:
        """Record an accelerometer spike event."""
        self._last_accel_spike = datetime.now(timezone.utc)
        self._statistics.total_wake_triggers += 1
    
    def record_geofence_exit(self) -> None:
        """Record a geofence exit event."""
        self._last_geofence_exit = datetime.now(timezone.utc)
        self._statistics.total_wake_triggers += 1
    
    def evaluate_mode_transition(self, fetcher: ExternalDataFetcher) -> None:
        """Evaluate and execute mode transitions based on all inputs."""
        with self._lock:
            # If mode is forced, don't auto-transition
            if self._forced_mode is not None:
                if self._current_mode != self._forced_mode:
                    self._transition_to(self._forced_mode)
                return
            
            # Get current state
            is_home = fetcher.is_at_home()
            activity = fetcher.get_activity_level()
            battery = fetcher.get_battery_percent()
            is_night = self.is_night_time()
            accel_spike = fetcher.check_accel_spike()
            scheduled_wake = self.is_scheduled_check_time()
            
            # Check wake triggers first (highest priority)
            now = datetime.now(timezone.utc)
            
            # Check recent accel spike (within last 5 minutes)
            if accel_spike:
                self.record_accel_spike()
                if self._current_mode != GPSMode.HIGH_RATE:
                    logger.info("Wake trigger: Accelerometer spike detected (%.2fG)", 
                                activity + 1.0)
                    self._transition_to(GPSMode.HIGH_RATE)
                return
            
            # Check recent geofence exit (within last 5 minutes)
            if self._last_geofence_exit:
                time_since_exit = (now - self._last_geofence_exit).total_seconds()
                if time_since_exit < 300 and self._current_mode != GPSMode.HIGH_RATE:
                    logger.info("Wake trigger: Recent geofence exit")
                    self._transition_to(GPSMode.HIGH_RATE)
                    return
            
            # Check scheduled wake
            if scheduled_wake and self._current_mode == GPSMode.SLEEP_MODE:
                logger.info("Wake trigger: Scheduled check-in time")
                self._transition_to(GPSMode.HIGH_RATE)
                return
            
            # Battery conservation mode
            if battery is not None and battery < self.config.battery_low_threshold:
                if self._current_mode == GPSMode.HIGH_RATE:
                    logger.warning("Battery low (%.1f%%), switching to LOW_RATE", battery)
                    self._transition_to(GPSMode.LOW_RATE)
                return
            
            # Normal mode logic
            if not is_home:
                # Away from home - always high rate
                if self._current_mode != GPSMode.HIGH_RATE:
                    logger.info("Away from home, switching to HIGH_RATE")
                    self._transition_to(GPSMode.HIGH_RATE)
            elif is_night:
                # At home, night time - sleep mode
                if self._current_mode != GPSMode.SLEEP_MODE:
                    logger.info("Night time at home, switching to SLEEP_MODE")
                    self._transition_to(GPSMode.SLEEP_MODE)
            elif activity < self.config.activity_threshold:
                # At home, low activity - low rate
                if self._current_mode != GPSMode.LOW_RATE:
                    logger.info("Low activity at home (%.2f), switching to LOW_RATE", activity)
                    self._transition_to(GPSMode.LOW_RATE)
            else:
                # At home, active - low rate (could upgrade to high if very active)
                if activity > 1.0 and self._current_mode != GPSMode.HIGH_RATE:
                    logger.info("High activity at home (%.2f), switching to HIGH_RATE", activity)
                    self._transition_to(GPSMode.HIGH_RATE)
                elif self._current_mode != GPSMode.LOW_RATE:
                    logger.info("Active at home, switching to LOW_RATE")
                    self._transition_to(GPSMode.LOW_RATE)
    
    def get_status(self) -> Dict[str, Any]:
        """Get current controller status."""
        with self._lock:
            now = datetime.now(timezone.utc)
            
            # Calculate next update time based on current mode
            if self._current_mode == GPSMode.HIGH_RATE:
                interval = 1.0 / self.config.high_rate_hz
            elif self._current_mode == GPSMode.LOW_RATE:
                interval = self.config.low_rate_interval_sec
            else:  # SLEEP_MODE
                interval = self.config.sleep_interval_sec
            
            last_update = self._statistics.mode_entry_times.get(self._current_mode)
            if last_update:
                next_update = last_update + timedelta(seconds=interval)
                seconds_until = max(0, (next_update - now).total_seconds())
            else:
                next_update = now
                seconds_until = 0
            
            return {
                "current_mode": self._current_mode.value,
                "mode_label": self._current_mode.display_name,
                "forced_mode": self._forced_mode.value if self._forced_mode else None,
                "update_interval_sec": interval,
                "next_update_time": next_update.isoformat() if next_update else None,
                "seconds_until_next_update": round(seconds_until, 1),
                "is_night": self.is_night_time(),
                "statistics": self._statistics.to_dict(),
            }


# ---------------------------------------------------------------------------
# GPS Update Controller
# ---------------------------------------------------------------------------
class GPSUpdateController:
    """Controls when GPS updates are fetched based on mode."""
    
    def __init__(self, mode_controller: GPSModeController, fetcher: ExternalDataFetcher) -> None:
        self.mode_controller = mode_controller
        self.fetcher = fetcher
        self._last_gps_reading: Optional[Dict[str, Any]] = None
        self._last_update_time: Optional[datetime] = None
        self._lock = threading.Lock()
    
    def should_update(self) -> bool:
        """Check if it's time to fetch a GPS update."""
        with self._lock:
            mode = self.mode_controller.current_mode
            config = self.mode_controller.config
            
            if mode == GPSMode.HIGH_RATE:
                interval = 1.0 / config.high_rate_hz
            elif mode == GPSMode.LOW_RATE:
                interval = config.low_rate_interval_sec
            else:
                interval = config.sleep_interval_sec
            
            if self._last_update_time is None:
                return True
            
            elapsed = (datetime.now(timezone.utc) - self._last_update_time).total_seconds()
            return elapsed >= interval
    
    def fetch_update(self) -> Optional[Dict[str, Any]]:
        """Fetch GPS update from daemon."""
        data = self.fetcher._get(self.mode_controller.config.gps_daemon_url)
        if data is not None:
            with self._lock:
                self._last_gps_reading = data
                self._last_update_time = datetime.now(timezone.utc)
                self.mode_controller._statistics.total_fixes += 1
        return data
    
    def get_last_reading(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            return self._last_gps_reading


# ---------------------------------------------------------------------------
# Geofence Event Monitor
# ---------------------------------------------------------------------------
class GeofenceEventMonitor:
    """Monitors for geofence exit events to trigger wake."""
    
    def __init__(self, controller: GPSModeController, fetcher: ExternalDataFetcher) -> None:
        self.controller = controller
        self.fetcher = fetcher
        self._was_home: Optional[bool] = None
        self._lock = threading.Lock()
    
    def check(self) -> None:
        """Check for geofence state changes."""
        with self._lock:
            is_home = self.fetcher.is_at_home()
            
            if self._was_home is not None:
                if self._was_home and not is_home:
                    # Transition: home → away
                    logger.info("Geofence event: Exited home zone")
                    self.controller.record_geofence_exit()
                elif not self._was_home and is_home:
                    # Transition: away → home
                    logger.info("Geofence event: Entered home zone")
            
            self._was_home = is_home


# ---------------------------------------------------------------------------
# HTTP API Handler
# ---------------------------------------------------------------------------
class AdaptiveGPSAPIHandler(BaseHTTPRequestHandler):
    """HTTP API for adaptive GPS control."""
    
    # Class-level references
    controller: GPSModeController = None  # type: ignore[assignment]
    
    def do_GET(self) -> None:
        parsed = self._parse_path()
        path = parsed["path"]
        
        if path == "/gps/adaptive/status":
            self._handle_status()
        elif path == "/gps/adaptive/stats":
            self._handle_stats()
        elif path == "/gps/adaptive/health":
            self._json_response({"status": "ok", "service": "adaptive_gps"})
        else:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b'{"error": "not found"}')
    
    def do_POST(self) -> None:
        parsed = self._parse_path()
        path = parsed["path"]
        
        if path == "/gps/adaptive/mode":
            self._handle_set_mode()
        else:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b'{"error": "not found"}')
    
    def _handle_status(self) -> None:
        """Return current mode and status."""
        status = self.controller.get_status()
        status["timestamp"] = datetime.now(timezone.utc).isoformat()
        self._json_response(status)
    
    def _handle_stats(self) -> None:
        """Return statistics and power savings."""
        stats = self.controller._statistics.to_dict()
        stats["timestamp"] = datetime.now(timezone.utc).isoformat()
        self._json_response(stats)
    
    def _handle_set_mode(self) -> None:
        """Force a specific mode."""
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
        
        mode_str = data.get("mode", "").lower()
        
        if mode_str == "high":
            self.controller.force_mode(GPSMode.HIGH_RATE)
            self._json_response({"success": True, "mode": "high", "forced": True})
        elif mode_str == "low":
            self.controller.force_mode(GPSMode.LOW_RATE)
            self._json_response({"success": True, "mode": "low", "forced": True})
        elif mode_str == "sleep":
            self.controller.force_mode(GPSMode.SLEEP_MODE)
            self._json_response({"success": True, "mode": "sleep", "forced": True})
        elif mode_str == "auto" or mode_str == "":
            self.controller.force_mode(None)
            self._json_response({"success": True, "mode": "auto", "forced": False})
        else:
            self._json_response(
                {"error": f"invalid mode: {mode_str}. Use 'high', 'low', 'sleep', or 'auto'"},
                status=400
            )
    
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
        logger.debug(f"HTTP: {fmt % args}")


# ---------------------------------------------------------------------------
# Configuration Loader
# ---------------------------------------------------------------------------
def load_config(path: str) -> AdaptiveGPSConfig:
    """Load YAML config, returning defaults for missing keys."""
    with open(path) as f:
        cfg = yaml.safe_load(f) or {}
    
    agps_cfg = cfg.get("adaptive_gps", {})
    hermes_cfg = cfg.get("hermes", {})
    
    config = AdaptiveGPSConfig()
    
    if not agps_cfg.get("enabled", True):
        config.enabled = False
    
    config.high_rate_hz = agps_cfg.get("high_rate_hz", 10.0)
    config.low_rate_interval_sec = agps_cfg.get("low_rate_interval_sec", 30.0)
    config.sleep_interval_sec = agps_cfg.get("sleep_interval_sec", 300.0)
    config.activity_threshold = agps_cfg.get("activity_threshold", 0.5)
    
    night_hours = agps_cfg.get("night_hours", [22, 6])
    if len(night_hours) >= 2:
        config.night_start_hour = night_hours[0]
        config.night_end_hour = night_hours[1]
    
    config.battery_low_threshold = cfg.get("power", {}).get("battery", {}).get("warning_percent", 30.0)
    
    # Configure API endpoints
    hermes_port = hermes_cfg.get("api_port", 9110)
    config.geofence_api_url = f"http://127.0.0.1:{hermes_cfg.get('geofence_api_port', 9114)}/geofence"
    config.health_api_url = f"http://127.0.0.1:{hermes_cfg.get('health_api_port', 9112)}/health"
    config.power_api_url = f"http://127.0.0.1:{cfg.get('power', {}).get('api_port', 9120)}/power"
    config.gps_daemon_url = f"http://127.0.0.1:{hermes_cfg.get('gps_api_port', 9111)}/gps"
    config.api_port = agps_cfg.get("api_port", 9124)
    
    return config


# ---------------------------------------------------------------------------
# Main Adaptive GPS Loop
# ---------------------------------------------------------------------------
def adaptive_gps_loop(
    controller: GPSModeController,
    fetcher: ExternalDataFetcher,
    gps_controller: GPSUpdateController,
    geofence_monitor: GeofenceEventMonitor,
    stop_event: threading.Event,
) -> None:
    """Main loop for adaptive GPS management."""
    logger.info("Adaptive GPS loop started")
    
    check_interval = controller.config.check_interval_sec
    
    while not stop_event.is_set():
        try:
            # Check geofence events
            geofence_monitor.check()
            
            # Evaluate mode transitions
            controller.evaluate_mode_transition(fetcher)
            
            # Fetch GPS update if needed
            if gps_controller.should_update():
                gps_data = gps_controller.fetch_update()
                if gps_data:
                    logger.debug("GPS update: lat=%.6f, lon=%.6f, mode=%s",
                                gps_data.get("lat", 0),
                                gps_data.get("lon", 0),
                                controller.current_mode.value)
            
        except Exception:
            logger.exception("Error in adaptive GPS loop")
        
        # Wait for next check cycle
        for _ in range(int(check_interval * 10)):
            if stop_event.is_set():
                return
            time.sleep(0.1)


# ---------------------------------------------------------------------------
# Main Daemon
# ---------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(description="Dog Agent Adaptive GPS Module")
    parser.add_argument(
        "--config",
        default=None,
        help="Path to config.yaml (default: ../config.yaml relative to this script)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="HTTP API listen port (overrides config)",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Enable debug logging"
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
        cfg = AdaptiveGPSConfig()
    
    if not cfg.enabled:
        logger.info("Adaptive GPS module is disabled in config. Exiting.")
        return
    
    api_port = args.port if args.port is not None else cfg.api_port
    
    # Initialize components
    controller = GPSModeController(cfg)
    fetcher = ExternalDataFetcher(cfg)
    gps_controller = GPSUpdateController(controller, fetcher)
    geofence_monitor = GeofenceEventMonitor(controller, fetcher)
    
    # Log initial status
    logger.info("Adaptive GPS initialized")
    logger.info("  High rate: %.1f Hz (%.1fs interval)", cfg.high_rate_hz, 1.0/cfg.high_rate_hz)
    logger.info("  Low rate: %.1f sec interval", cfg.low_rate_interval_sec)
    logger.info("  Sleep mode: %.1f sec interval", cfg.sleep_interval_sec)
    logger.info("  Night hours: %02d:00 - %02d:00", cfg.night_start_hour, cfg.night_end_hour)
    logger.info("  Activity threshold: %.2f", cfg.activity_threshold)
    logger.info("  API port: %d", api_port)
    
    # Start adaptive GPS loop
    stop_event = threading.Event()
    
    adaptive_thread = threading.Thread(
        target=adaptive_gps_loop,
        args=(controller, fetcher, gps_controller, geofence_monitor, stop_event),
        name="adaptive-gps",
        daemon=True,
    )
    adaptive_thread.start()
    logger.info("Adaptive GPS thread started")
    
    # Start HTTP API server
    AdaptiveGPSAPIHandler.controller = controller
    server = HTTPServer(("127.0.0.1", api_port), AdaptiveGPSAPIHandler)
    server_thread = threading.Thread(
        target=server.serve_forever,
        name="adaptive-gps-api",
        daemon=True,
    )
    server_thread.start()
    logger.info("Adaptive GPS API server listening on http://127.0.0.1:%d/gps/adaptive", api_port)
    
    # Graceful shutdown
    def shutdown(signum: int, frame: Any) -> None:
        logger.info("Received signal %d — shutting down...", signum)
        stop_event.set()
        server.shutdown()
        logger.info("Adaptive GPS module stopped.")
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
