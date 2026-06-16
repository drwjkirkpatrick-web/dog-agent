#!/usr/bin/env python3
"""
Power Manager Module — Dog Agent
=================================

Manages power for the entire dog-agent system using an external microcontroller
approach (simulated via software, with hardware interface defined for future
Pi Pico integration).

This module implements "sleep cycles" where the Pi simulates deep sleep by:
  - Stopping non-critical modules (voice, behavior analysis)
  - Reducing GPS update rate to minimum
  - Pausing health monitoring checks
  - Maintaining only geofence (for escape detection)
  - Logging accumulated data before sleep

Power Management Strategy
-------------------------
The dog-agent operates in three power modes:

  ACTIVE:     All modules running at full capacity
              - Use when: walking, high activity, away from home
              - Power consumption: ~2.5W (Pi 4 with all modules)
              - Battery life: ~10-12 hours with 10Ah battery

  IDLE:       Reduced modules (at home, normal activity)
              - Use when: at home, moderate activity during day
              - GPS throttled, voice disabled, behavior minimal
              - Power consumption: ~1.5W
              - Battery life: ~16-18 hours

  DEEP_SLEEP: Critical modules only (at home, night/rest)
              - Use when: at home, nighttime, dog resting
              - Only geofence (escape detection) + GPS (minimal)
              - Pi can power down USB, HDMI, CPU underclocked
              - Power consumption: ~0.8W
              - Battery life: ~30+ hours

External Microcontroller Integration
------------------------------------
For production deployment, this module is designed to interface with a
Pi Pico or similar microcontroller:
  - Pico manages real power rails (cuts 5V/3.3V to Pi)
  - Pi signals sleep intent via GPIO or I2C
  - Pico cuts power, waits for wake condition
  - Wake conditions: RTC alarm, GPIO trigger (bark detector), or
    external interrupt (geofence breach signal from GPS module)
  - Pico restores power, Pi boots, resumes from last state

Simulation Mode
---------------
  --simulate: Creates fake battery drain/gain curves for testing

HTTP API (Port 9120)
--------------------
  GET  /power/status    — current mode, battery, next wake time
  POST /power/mode      — set mode manually {"mode": "active|idle|deep_sleep"}
  GET  /power/battery   — voltage, percentage, estimated hours
  POST /power/sleep     — trigger sleep now {"duration_min": 30}

Configuration (config.yaml)
---------------------------
  power:
    deep_sleep:
      enabled: true
      interval_min: 5              # Wake every N minutes
      critical_modules: ["gps", "geofence", "alerts"]
    battery:
      capacity_mah: 10000          # Battery capacity
      warning_percent: 20          # Low battery warning
      critical_percent: 10         # Force deep sleep
    schedule:
      night_start: "23:00"         # Auto deep sleep start
      night_end: "06:00"           # Auto wake time
      away_timeout_min: 30           # Auto sleep after inactivity

Usage:
    python src/power_manager.py               # Normal mode
    python src/power_manager.py --simulate    # Simulation mode
    python src/power_manager.py --config /path/to/config.yaml
    python src/power_manager.py --port 9120
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
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from enum import Enum, auto
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple
from contextlib import suppress

import yaml

try:
    import requests
except ImportError:
    requests = None  # type: ignore

# ---------------------------------------------------------------------------
# Logging Setup
# ---------------------------------------------------------------------------
logger = logging.getLogger("power_manager")
_handler = logging.StreamHandler(sys.stdout)
_handler.setFormatter(logging.Formatter(
    "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
))
_handler.flush = sys.stdout.flush  # type: ignore
logger.addHandler(_handler)
logger.setLevel(logging.INFO)


# ---------------------------------------------------------------------------
# Power Mode Enum
# ---------------------------------------------------------------------------
class PowerMode(Enum):
    """Power modes for the dog-agent system."""
    ACTIVE = "active"           # All modules running
    IDLE = "idle"               # Reduced modules
    DEEP_SLEEP = "deep_sleep"   # Critical only

    @classmethod
    def from_string(cls, s: str) -> "PowerMode":
        """Parse mode from string."""
        mode_map = {
            "active": cls.ACTIVE,
            "idle": cls.IDLE,
            "deep_sleep": cls.DEEP_SLEEP,
            "deep sleep": cls.DEEP_SLEEP,
        }
        return mode_map.get(s.lower().strip(), cls.ACTIVE)


# ---------------------------------------------------------------------------
# Module Status
# ---------------------------------------------------------------------------
@dataclass
class ModuleStatus:
    """Status of a managed module."""
    name: str
    enabled: bool = True
    required: bool = False  # Critical module (can't be disabled)
    last_active: Optional[datetime] = None
    power_draw_mw: float = 100.0  # Estimated power consumption


# ---------------------------------------------------------------------------
# Battery Model
# ---------------------------------------------------------------------------
@dataclass
class BatteryState:
    """Battery state tracking."""
    voltage: float = 4.2              # Current voltage (LiPo 1S)
    percentage: float = 100.0         # State of charge
    capacity_mah: float = 10000.0     # Total capacity
    current_ma: float = 0.0           # Discharge current (positive = discharging)
    cycle_count: int = 0              # Charge cycles
    last_full_charge: Optional[datetime] = None

    # LiPo discharge curve (voltage -> percentage)
    # Simplified curve for 1S LiPo (3.0V min, 4.2V max)
    VOLTAGE_CURVE: List[Tuple[float, float]] = field(default_factory=lambda: [
        (4.20, 100.0), (4.15, 95.0), (4.10, 90.0), (4.05, 85.0),
        (4.00, 80.0), (3.95, 70.0), (3.90, 60.0), (3.85, 50.0),
        (3.80, 40.0), (3.75, 30.0), (3.70, 20.0), (3.65, 15.0),
        (3.60, 10.0), (3.50, 5.0), (3.40, 2.0), (3.30, 1.0), (3.00, 0.0),
    ])

    def update_percentage_from_voltage(self) -> None:
        """Calculate percentage from voltage using discharge curve."""
        for v_high, p_high in self.VOLTAGE_CURVE:
            if self.voltage >= v_high:
                self.percentage = p_high
                return
        self.percentage = 0.0

    def estimate_hours_remaining(self) -> float:
        """Estimate hours remaining at current draw."""
        if self.current_ma <= 0:
            return 999.0  # Charging or no draw
        remaining_mah = self.capacity_mah * (self.percentage / 100.0)
        return remaining_mah / self.current_ma


# ---------------------------------------------------------------------------
# Power Configuration
# ---------------------------------------------------------------------------
@dataclass
class PowerConfig:
    """Power management configuration."""
    # Deep sleep settings
    deep_sleep_enabled: bool = True
    sleep_interval_min: int = 5
    critical_modules: List[str] = field(default_factory=lambda: [
        "gps", "geofence", "alerts"
    ])

    # Battery thresholds
    warning_percent: int = 20
    critical_percent: int = 10
    capacity_mah: int = 10000

    # Schedule
    night_start: str = "23:00"
    night_end: str = "06:00"
    away_timeout_min: int = 30
    inactivity_timeout_min: int = 60

    # Mode transition thresholds
    activity_threshold: float = 0.3  # m/s^2 acceleration variance
    home_geofence_m: float = 50.0


# ---------------------------------------------------------------------------
# Simulation Model
# ---------------------------------------------------------------------------
class BatterySimulator:
    """
    Simulates battery behavior for testing.

    Models:
      - Self-discharge (0.5% per day for LiPo)
      - Temperature effects (simplified)
      - Charge/discharge curves
      - Load-based voltage sag
    """

    def __init__(self, capacity_mah: float = 10000.0, initial_pct: float = 75.0):
        self.capacity_mah = capacity_mah
        self.percentage = initial_pct
        self.voltage = 3.8  # Start at nominal voltage
        self.last_update = time.monotonic()
        self.charging = False

        # Power draw by mode (mA)
        self.mode_current: Dict[PowerMode, float] = {
            PowerMode.ACTIVE: 500.0,      # ~2.5W at 5V
            PowerMode.IDLE: 300.0,        # ~1.5W at 5V
            PowerMode.DEEP_SLEEP: 160.0,  # ~0.8W at 5V
        }

        # Activity modifiers
        self.activity_multiplier: Dict[str, float] = {
            "resting": 0.8,
            "walking": 1.2,
            "running": 1.8,
            "playing": 1.5,
        }

    def update(self, mode: PowerMode, activity: str = "resting", dt: Optional[float] = None) -> None:
        """Update battery state based on time passed."""
        now = time.monotonic()
        if dt is None:
            dt = now - self.last_update
        self.last_update = now

        dt_hours = dt / 3600.0

        # Calculate discharge
        base_current = self.mode_current.get(mode, 300.0)
        activity_mult = self.activity_multiplier.get(activity, 1.0)
        total_current = base_current * activity_mult

        if self.charging:
            # Charging: add capacity (simplified linear model)
            charge_rate = 2000.0  # 2A charging
            self.percentage += (charge_rate * dt_hours / self.capacity_mah) * 100
            self.percentage = min(100.0, self.percentage)
        else:
            # Discharging
            discharged_pct = (total_current * dt_hours / self.capacity_mah) * 100
            self.percentage -= discharged_pct
            self.percentage = max(0.0, self.percentage)

        # Self-discharge (small)
        self.percentage -= 0.005 * dt_hours  # 0.5% per day

        # Update voltage based on percentage
        self.voltage = self._percentage_to_voltage(self.percentage)

    def _percentage_to_voltage(self, pct: float) -> float:
        """Convert percentage to voltage (inverse of discharge curve)."""
        # Simplified: linear between 3.0V (0%) and 4.2V (100%)
        return 3.0 + (pct / 100.0) * 1.2

    def start_charging(self) -> None:
        """Start charging."""
        self.charging = True

    def stop_charging(self) -> None:
        """Stop charging."""
        self.charging = False


# ---------------------------------------------------------------------------
# Module Controller Interface
# ---------------------------------------------------------------------------
class ModuleController:
    """
    Controls other dog-agent modules via HTTP API.

    In a real implementation with external microcontroller:
      - Would use GPIO signals or I2C to enable/disable power rails
      - MCU would physically cut power to module circuits
      - Sleep state persisted in RTC memory or external flash

    For now, we use HTTP API calls to other modules.
    """

    # Module port mapping (matches main.py)
    MODULE_PORTS: Dict[str, int] = {
        "gps": 9111,
        "sensors": 9112,
        "health": 9113,
        "geofence": 9114,
        "behavior": 9115,
        "voice": 9116,
        "logger": 9117,
    }

    # Module power consumption estimates (mW)
    MODULE_POWER: Dict[str, float] = {
        "gps": 300.0,
        "sensors": 100.0,
        "health": 200.0,
        "geofence": 150.0,
        "behavior": 400.0,
        "voice": 500.0,
        "logger": 100.0,
    }

    def __init__(self, base_url: str = "http://127.0.0.1"):
        self.base_url = base_url
        self.session = requests.Session() if requests else None
        self.module_states: Dict[str, Dict[str, Any]] = {}

    def get_module_health(self, name: str) -> Optional[Dict[str, Any]]:
        """Get health status from a module."""
        port = self.MODULE_PORTS.get(name)
        if not port:
            return None

        try:
            if self.session:
                resp = self.session.get(
                    f"{self.base_url}:{port}/health",
                    timeout=2.0
                )
                if resp.status_code == 200:
                    return resp.json()
        except Exception as e:
            logger.debug("Health check failed for %s: %s", name, e)

        return None

    def is_module_running(self, name: str) -> bool:
        """Check if a module is responding."""
        health = self.get_module_health(name)
        return health is not None

    def disable_module(self, name: str) -> bool:
        """
        Disable a non-critical module.

        In MCU implementation: would signal MCU to cut power rail.
        """
        logger.info("Disabling module: %s", name)
        # Currently modules don't have disable API, just track intent
        self.module_states[name] = {"enabled": False, "disabled_at": datetime.now(timezone.utc).isoformat()}
        return True

    def enable_module(self, name: str) -> bool:
        """
        Enable a module.

        In MCU implementation: would signal MCU to enable power rail.
        """
        logger.info("Enabling module: %s", name)
        self.module_states[name] = {"enabled": True, "enabled_at": datetime.now(timezone.utc).isoformat()}
        return True

    def set_module_throttle(self, name: str, throttle: float) -> bool:
        """Set module update rate throttle (0.0 to 1.0)."""
        # Would send throttle command via HTTP API
        logger.debug("Throttling %s to %.1f%%", name, throttle * 100)
        return True

    def get_total_power_draw(self) -> float:
        """Calculate total power draw from active modules."""
        total = 0.0
        for name, state in self.module_states.items():
            if state.get("enabled", True):
                total += self.MODULE_POWER.get(name, 100.0)
        return total


# ---------------------------------------------------------------------------
# Location Provider
# ---------------------------------------------------------------------------
class LocationProvider:
    """Provides location and home/away status from geofence module."""

    def __init__(self, geofence_port: int = 9114):
        self.geofence_port = geofence_port
        self.session = requests.Session() if requests else None

    def get_location(self) -> Optional[Dict[str, Any]]:
        """Get current GPS location."""
        try:
            if self.session:
                resp = self.session.get(
                    f"http://127.0.0.1:{self.geofence_port}/geofence/status",
                    timeout=2.0
                )
                if resp.status_code == 200:
                    return resp.json()
        except Exception as e:
            logger.debug("Location fetch failed: %s", e)
        return None

    def is_at_home(self) -> bool:
        """Check if dog is at home based on geofence."""
        location = self.get_location()
        if location:
            # Check if in any "home" zone
            zones = location.get("zones", [])
            for zone in zones:
                if zone.get("name", "").lower() == "home" and zone.get("inside"):
                    return True
        return False


# ---------------------------------------------------------------------------
# Activity Monitor
# ---------------------------------------------------------------------------
class ActivityMonitor:
    """Monitors dog activity level from health/behavior modules."""

    def __init__(self, health_port: int = 9113, behavior_port: int = 9115):
        self.health_port = health_port
        self.behavior_port = behavior_port
        self.session = requests.Session() if requests else None
        self.last_activity_time = datetime.now(timezone.utc)

    def get_activity_level(self) -> Dict[str, Any]:
        """Get current activity level."""
        try:
            if self.session:
                # Try behavior module first
                resp = self.session.get(
                    f"http://127.0.0.1:{self.behavior_port}/behavior/status",
                    timeout=2.0
                )
                if resp.status_code == 200:
                    data = resp.json()
                    return {
                        "level": data.get("activity_level", "unknown"),
                        "moving": data.get("is_moving", False),
                        "last_movement": data.get("last_movement"),
                    }
        except Exception:
            pass

        # Fallback: check health module accelerometer
        try:
            if self.session:
                resp = self.session.get(
                    f"http://127.0.0.1:{self.health_port}/health/status",
                    timeout=2.0
                )
                if resp.status_code == 200:
                    data = resp.json()
                    return {
                        "level": "moving" if data.get("moving", False) else "resting",
                        "moving": data.get("moving", False),
                        "last_movement": None,
                    }
        except Exception:
            pass

        return {"level": "unknown", "moving": False, "last_movement": None}

    def is_active(self, threshold_minutes: int = 30) -> bool:
        """Check if dog has been active recently."""
        activity = self.get_activity_level()
        if activity.get("moving"):
            self.last_activity_time = datetime.now(timezone.utc)
            return True

        # Check time since last activity
        elapsed = datetime.now(timezone.utc) - self.last_activity_time
        return elapsed.total_seconds() < (threshold_minutes * 60)


# ---------------------------------------------------------------------------
# Wake Scheduler
# ---------------------------------------------------------------------------
class WakeScheduler:
    """Manages wake-up scheduling for deep sleep cycles."""

    def __init__(self):
        self.scheduled_wake: Optional[datetime] = None
        self.wake_callbacks: List[Callable[[], None]] = []
        self._lock = threading.Lock()
        self._running = False
        self._thread: Optional[threading.Thread] = None

    def schedule_wake(self, at: datetime) -> None:
        """Schedule a wake-up time."""
        with self._lock:
            self.scheduled_wake = at
            logger.info("Scheduled wake for %s", at.isoformat())

    def schedule_wake_in(self, minutes: int) -> None:
        """Schedule wake-up in N minutes from now."""
        wake_time = datetime.now(timezone.utc) + timedelta(minutes=minutes)
        self.schedule_wake(wake_time)

    def cancel_wake(self) -> None:
        """Cancel scheduled wake."""
        with self._lock:
            self.scheduled_wake = None

    def get_time_to_wake(self) -> Optional[timedelta]:
        """Get time remaining until scheduled wake."""
        with self._lock:
            if self.scheduled_wake:
                return self.scheduled_wake - datetime.now(timezone.utc)
            return None

    def register_callback(self, callback: Callable[[], None]) -> None:
        """Register a callback to be called on wake."""
        self.wake_callbacks.append(callback)

    def start(self) -> None:
        """Start the wake scheduler thread."""
        self._running = True
        self._thread = threading.Thread(target=self._scheduler_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop the wake scheduler."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=2.0)

    def _scheduler_loop(self) -> None:
        """Main scheduler loop."""
        while self._running:
            with self._lock:
                if self.scheduled_wake:
                    now = datetime.now(timezone.utc)
                    if now >= self.scheduled_wake:
                        logger.info("Wake time reached: %s", now.isoformat())
                        self.scheduled_wake = None
                        # Trigger callbacks
                        for cb in self.wake_callbacks:
                            try:
                                cb()
                            except Exception as e:
                                logger.error("Wake callback error: %s", e)
            time.sleep(1.0)


# ---------------------------------------------------------------------------
# Sleep Data Manager
# ---------------------------------------------------------------------------
class SleepDataManager:
    """
    Manages data persistence across sleep cycles.

    Before entering deep sleep, accumulated data is flushed to disk.
    On wake, state is restored.

    In MCU implementation, this would use external RTC memory or flash.
    """

    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.state_file = data_dir / "power_state.json"
        self.sleep_log = data_dir / "sleep_cycles.jsonl"
        self.data_dir.mkdir(parents=True, exist_ok=True)

    def save_state(self, state: Dict[str, Any]) -> None:
        """Save current power state before sleep."""
        state["saved_at"] = datetime.now(timezone.utc).isoformat()
        with open(self.state_file, "w") as f:
            json.dump(state, f, indent=2)
        logger.info("Saved power state to %s", self.state_file)

    def load_state(self) -> Optional[Dict[str, Any]]:
        """Load power state after wake."""
        try:
            with open(self.state_file, "r") as f:
                state = json.load(f)
                logger.info("Restored power state from %s", self.state_file)
                return state
        except FileNotFoundError:
            return None

    def log_sleep_cycle(self, entry_mode: PowerMode, duration_min: float,
                        data_accumulated: Dict[str, Any]) -> None:
        """Log a completed sleep cycle."""
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "mode": entry_mode.value,
            "duration_min": duration_min,
            "data": data_accumulated,
        }
        with open(self.sleep_log, "a") as f:
            f.write(json.dumps(entry) + "\n")


# ---------------------------------------------------------------------------
# Main Power Manager
# ---------------------------------------------------------------------------
class PowerManager:
    """
    Central power management for dog-agent.

    Responsibilities:
      - Track and manage power modes
      - Coordinate module enable/disable
      - Monitor battery state
      - Schedule sleep/wake cycles
      - Automatic mode transitions
    """

    def __init__(self, config: PowerConfig, data_dir: Path,
                 simulate: bool = False):
        self.config = config
        self.data_dir = data_dir
        self.simulate = simulate

        # State
        self.current_mode = PowerMode.ACTIVE
        self.lock = threading.RLock()
        self.running = False
        self.shutdown_event = threading.Event()

        # Sub-components
        self.battery = BatteryState(capacity_mah=config.capacity_mah)
        self.module_controller = ModuleController()
        self.location_provider = LocationProvider()
        self.activity_monitor = ActivityMonitor()
        self.wake_scheduler = WakeScheduler()
        self.data_manager = SleepDataManager(data_dir)
        self.simulator: Optional[BatterySimulator] = None

        if simulate:
            self.simulator = BatterySimulator(
                capacity_mah=config.capacity_mah,
                initial_pct=75.0
            )

        # Statistics
        self.stats = {
            "mode_switches": 0,
            "sleep_cycles": 0,
            "time_in_mode": {mode.value: 0.0 for mode in PowerMode},
            "mode_start_time": datetime.now(timezone.utc),
        }

        # Restore previous state if available
        self._restore_state()

    def _restore_state(self) -> None:
        """Restore state from previous session."""
        state = self.data_manager.load_state()
        if state:
            saved_mode = state.get("mode", "active")
            self.current_mode = PowerMode.from_string(saved_mode)
            logger.info("Restored mode: %s", self.current_mode.value)

    # -----------------------------------------------------------------------
    # Mode Management
    # -----------------------------------------------------------------------
    def set_mode(self, mode: PowerMode, reason: str = "manual") -> bool:
        """Set power mode with transition logic."""
        with self.lock:
            if mode == self.current_mode:
                return True

            old_mode = self.current_mode
            logger.info("Mode transition: %s -> %s (reason: %s)",
                        old_mode.value, mode.value, reason)

            # Pre-transition: save data
            self._prepare_for_transition(old_mode, mode)

            # Perform transition
            self._enter_mode(mode)

            # Update stats
            now = datetime.now(timezone.utc)
            elapsed = (now - self.stats["mode_start_time"]).total_seconds()
            self.stats["time_in_mode"][old_mode.value] += elapsed
            self.stats["mode_switches"] += 1
            self.stats["mode_start_time"] = now
            self.current_mode = mode

            return True

    def _prepare_for_transition(self, old_mode: PowerMode,
                                new_mode: PowerMode) -> None:
        """Prepare for mode transition."""
        if new_mode == PowerMode.DEEP_SLEEP:
            # Flush accumulated data
            self._flush_data()
            # Save state for wake
            self.data_manager.save_state({
                "mode": old_mode.value,
                "battery_pct": self.battery.percentage,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })

    def _enter_mode(self, mode: PowerMode) -> None:
        """Configure system for new mode."""
        if mode == PowerMode.ACTIVE:
            self._enter_active()
        elif mode == PowerMode.IDLE:
            self._enter_idle()
        elif mode == PowerMode.DEEP_SLEEP:
            self._enter_deep_sleep()

    def _enter_active(self) -> None:
        """Enter ACTIVE mode - all systems go."""
        logger.info("Entering ACTIVE mode")

        # Enable all modules
        for name in self.module_controller.MODULE_PORTS:
            self.module_controller.enable_module(name)

        # GPS full rate
        self.module_controller.set_module_throttle("gps", 1.0)

        # Cancel any scheduled sleep
        self.wake_scheduler.cancel_wake()

    def _enter_idle(self) -> None:
        """Enter IDLE mode - reduced power."""
        logger.info("Entering IDLE mode")

        # Disable non-critical modules
        for name in self.module_controller.MODULE_PORTS:
            if name not in self.config.critical_modules:
                if name == "voice":  # Voice not needed at home
                    self.module_controller.disable_module(name)
                elif name == "behavior":  # Reduce behavior processing
                    self.module_controller.set_module_throttle("behavior", 0.3)

        # Throttle GPS
        self.module_controller.set_module_throttle("gps", 0.3)

    def _enter_deep_sleep(self) -> None:
        """
        Enter DEEP_SLEEP mode - critical only.

        In software simulation: we reduce module activity but keep running.
        In hardware: would signal MCU to cut power and set RTC alarm.
        """
        logger.info("Entering DEEP_SLEEP mode")

        # Disable all non-critical modules
        for name in self.module_controller.MODULE_PORTS:
            if name not in self.config.critical_modules:
                self.module_controller.disable_module(name)

        # Minimal GPS updates
        self.module_controller.set_module_throttle("gps", 0.1)

        # Schedule wake
        self.wake_scheduler.schedule_wake_in(self.config.sleep_interval_min)

        self.stats["sleep_cycles"] += 1

        if not self.simulate:
            # In real hardware, would trigger actual sleep
            logger.info("Deep sleep would cut power now (simulation mode active)")

    def _flush_data(self) -> None:
        """Flush accumulated data before sleep."""
        logger.info("Flushing accumulated data")
        # Trigger logger module flush
        try:
            if requests:
                requests.post(
                    "http://127.0.0.1:9117/logger/flush",
                    timeout=5.0
                )
        except Exception as e:
            logger.warning("Data flush failed: %s", e)

    def get_mode(self) -> PowerMode:
        """Get current power mode."""
        with self.lock:
            return self.current_mode

    # -----------------------------------------------------------------------
    # Automatic Transitions
    # -----------------------------------------------------------------------
    def _check_automatic_transition(self) -> None:
        """Check if automatic mode transition is needed."""
        with self.lock:
            current = self.current_mode

            # Don't auto-transition if manual override
            # (could add manual lock flag)

            # Get current conditions
            at_home = self.location_provider.is_at_home()
            is_night = self._is_night_time()
            battery_low = self.battery.percentage < self.config.warning_percent
            battery_critical = self.battery.percentage < self.config.critical_percent
            is_active = self.activity_monitor.is_active(self.config.away_timeout_min)

            # Decision tree
            new_mode = None

            if battery_critical:
                new_mode = PowerMode.DEEP_SLEEP
                reason = "critical battery"
            elif at_home and is_night and not is_active:
                new_mode = PowerMode.DEEP_SLEEP
                reason = "nighttime at home"
            elif at_home and not is_active and current == PowerMode.ACTIVE:
                # Been active but now at rest
                new_mode = PowerMode.IDLE
                reason = "at rest at home"
            elif not at_home and current != PowerMode.ACTIVE:
                # Away from home - need full tracking
                new_mode = PowerMode.ACTIVE
                reason = "away from home"
            elif battery_low and current == PowerMode.ACTIVE:
                # Low battery - conserve
                new_mode = PowerMode.IDLE
                reason = "low battery"

            if new_mode and new_mode != current:
                self.set_mode(new_mode, reason=reason)

    def _is_night_time(self) -> bool:
        """Check if current time is in night schedule."""
        now = datetime.now(timezone.utc)
        current_time = now.strftime("%H:%M")

        # Simple string comparison works for 24h format
        if self.config.night_start <= self.config.night_end:
            # Same day (e.g., 22:00 to 06:00 - wait, that's not same day)
            return (self.config.night_start <= current_time <=
                    self.config.night_end)
        else:
            # Overnight (e.g., 23:00 to 06:00)
            return (current_time >= self.config.night_start or
                    current_time <= self.config.night_end)

    # -----------------------------------------------------------------------
    # Battery Management
    # -----------------------------------------------------------------------
    def update_battery(self) -> None:
        """Update battery state."""
        if self.simulate and self.simulator:
            activity = self.activity_monitor.get_activity_level()
            self.simulator.update(
                self.current_mode,
                activity.get("level", "resting")
            )
            self.battery.voltage = self.simulator.voltage
            self.battery.percentage = self.simulator.percentage
            self.battery.current_ma = self.simulator.mode_current.get(
                self.current_mode, 300.0
            )
        else:
            # In real implementation, read from ADC via I2C
            # or from battery management IC
            pass

        # Check critical levels
        if self.battery.percentage < self.config.critical_percent:
            logger.warning("CRITICAL: Battery at %.1f%%", self.battery.percentage)
        elif self.battery.percentage < self.config.warning_percent:
            logger.warning("LOW BATTERY: %.1f%%", self.battery.percentage)

    # -----------------------------------------------------------------------
    # Main Loop
    # -----------------------------------------------------------------------
    def start(self) -> None:
        """Start the power manager."""
        logger.info("Starting Power Manager")
        self.running = True

        # Start wake scheduler
        self.wake_scheduler.register_callback(self._on_wake)
        self.wake_scheduler.start()

        # Start management thread
        self._management_thread = threading.Thread(
            target=self._management_loop, daemon=True
        )
        self._management_thread.start()

    def stop(self) -> None:
        """Stop the power manager."""
        logger.info("Stopping Power Manager")
        self.running = False
        self.shutdown_event.set()
        self.wake_scheduler.stop()

        # Save final state
        self.data_manager.save_state({
            "mode": self.current_mode.value,
            "battery_pct": self.battery.percentage,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

    def _management_loop(self) -> None:
        """Main management loop."""
        while self.running and not self.shutdown_event.is_set():
            try:
                # Update battery
                self.update_battery()

                # Check for automatic transitions
                self._check_automatic_transition()

                # Log status periodically
                logger.debug("Mode: %s, Battery: %.1f%%",
                            self.current_mode.value, self.battery.percentage)

            except Exception as e:
                logger.error("Management loop error: %s", e)

            # Wait with interrupt capability
            self.shutdown_event.wait(timeout=5.0)

    def _on_wake(self) -> None:
        """Handle wake event."""
        logger.info("Wake event triggered")
        with self.lock:
            # Transition to IDLE to assess situation
            if self.current_mode == PowerMode.DEEP_SLEEP:
                self.set_mode(PowerMode.IDLE, reason="scheduled wake")

    # -----------------------------------------------------------------------
    # API Helpers
    # -----------------------------------------------------------------------
    def get_status(self) -> Dict[str, Any]:
        """Get current power status."""
        with self.lock:
            time_to_wake = self.wake_scheduler.get_time_to_wake()

            return {
                "mode": self.current_mode.value,
                "battery": {
                    "voltage": round(self.battery.voltage, 2),
                    "percentage": round(self.battery.percentage, 1),
                    "hours_remaining": round(
                        self.battery.estimate_hours_remaining(), 1
                    ),
                    "current_ma": round(self.battery.current_ma, 1),
                },
                "next_wake": (self.wake_scheduler.scheduled_wake.isoformat()
                             if self.wake_scheduler.scheduled_wake else None),
                "time_to_wake_sec": (time_to_wake.total_seconds()
                                     if time_to_wake else None),
                "is_night": self._is_night_time(),
                "stats": {
                    "mode_switches": self.stats["mode_switches"],
                    "sleep_cycles": self.stats["sleep_cycles"],
                    "time_in_mode_sec": self.stats["time_in_mode"],
                },
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }

    def trigger_sleep(self, duration_min: int) -> Dict[str, Any]:
        """Manually trigger sleep."""
        self.set_mode(PowerMode.DEEP_SLEEP,
                     reason=f"manual trigger ({duration_min}min)")
        self.wake_scheduler.schedule_wake_in(duration_min)

        return {
            "success": True,
            "mode": PowerMode.DEEP_SLEEP.value,
            "wake_time": self.wake_scheduler.scheduled_wake.isoformat()
                         if self.wake_scheduler.scheduled_wake else None,
        }


# ---------------------------------------------------------------------------
# HTTP API Handler
# ---------------------------------------------------------------------------
class PowerAPIHandler(BaseHTTPRequestHandler):
    """HTTP API handler for power management."""

    power_manager: Optional[PowerManager] = None

    def log_message(self, format: str, *args: Any) -> None:
        """Override to use our logger."""
        logger.debug(format % args)

    def _send_json(self, data: Any, status: int = 200) -> None:
        """Send JSON response."""
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def _send_error(self, message: str, status: int = 400) -> None:
        """Send error response."""
        self._send_json({"error": message}, status=status)

    def _read_body(self) -> Optional[Dict[str, Any]]:
        """Read JSON body from request."""
        try:
            content_length = int(self.headers.get("Content-Length", 0))
            if content_length > 0:
                body = self.rfile.read(content_length).decode()
                return json.loads(body)
        except (json.JSONDecodeError, ValueError) as e:
            logger.warning("Failed to parse body: %s", e)
        return None

    def do_GET(self) -> None:  # noqa: N802
        """Handle GET requests."""
        path = self.path.split("?")[0]

        if path == "/power/status":
            if self.power_manager:
                self._send_json(self.power_manager.get_status())
            else:
                self._send_error("Power manager not initialized", 503)

        elif path == "/power/battery":
            if self.power_manager:
                status = self.power_manager.get_status()
                self._send_json(status.get("battery", {}))
            else:
                self._send_error("Power manager not initialized", 503)

        elif path == "/health":
            # Standard health endpoint
            self._send_json({
                "status": "healthy",
                "service": "power_manager",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })

        else:
            self._send_error("Not found", 404)

    def do_POST(self) -> None:  # noqa: N802
        """Handle POST requests."""
        path = self.path
        body = self._read_body()

        if path == "/power/mode":
            if not body or "mode" not in body:
                self._send_error("Missing 'mode' field", 400)
                return

            mode_str = body["mode"]
            try:
                mode = PowerMode.from_string(mode_str)
                if self.power_manager:
                    self.power_manager.set_mode(mode, reason="api_request")
                    self._send_json({
                        "success": True,
                        "mode": mode.value,
                        "previous_mode": self.power_manager.get_mode().value,
                    })
                else:
                    self._send_error("Power manager not initialized", 503)
            except ValueError as e:
                self._send_error(f"Invalid mode: {e}", 400)

        elif path == "/power/sleep":
            if not self.power_manager:
                self._send_error("Power manager not initialized", 503)
                return

            duration = body.get("duration_min", 30) if body else 30
            result = self.power_manager.trigger_sleep(duration)
            self._send_json(result)

        else:
            self._send_error("Not found", 404)

    def do_OPTIONS(self) -> None:  # noqa: N802
        """Handle CORS preflight."""
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()


# ---------------------------------------------------------------------------
# Configuration Loading
# ---------------------------------------------------------------------------
def load_config(config_path: Path) -> Tuple[PowerConfig, Path]:
    """Load configuration from YAML."""
    config = PowerConfig()
    data_dir = Path("data")

    if config_path.exists():
        try:
            with open(config_path, "r") as f:
                yaml_config = yaml.safe_load(f)

            if yaml_config:
                power_cfg = yaml_config.get("power", {})

                # Deep sleep settings
                deep_sleep = power_cfg.get("deep_sleep", {})
                config.deep_sleep_enabled = deep_sleep.get("enabled", True)
                config.sleep_interval_min = deep_sleep.get("interval_min", 5)
                config.critical_modules = deep_sleep.get(
                    "critical_modules",
                    ["gps", "geofence", "alerts"]
                )

                # Battery settings
                battery = power_cfg.get("battery", {})
                config.capacity_mah = battery.get("capacity_mah", 10000)
                config.warning_percent = battery.get("warning_percent", 20)
                config.critical_percent = battery.get("critical_percent", 10)

                # Schedule
                schedule = power_cfg.get("schedule", {})
                config.night_start = schedule.get("night_start", "23:00")
                config.night_end = schedule.get("night_end", "06:00")
                config.away_timeout_min = schedule.get("away_timeout_min", 30)
                config.inactivity_timeout_min = schedule.get(
                    "inactivity_timeout_min", 60
                )

                # Data directory
                logging_cfg = yaml_config.get("logging", {})
                data_dir = Path(logging_cfg.get("gps_track_dir", "data")).parent

            logger.info("Loaded configuration from %s", config_path)

        except Exception as e:
            logger.warning("Failed to load config: %s, using defaults", e)
    else:
        logger.warning("Config not found at %s, using defaults", config_path)

    return config, data_dir


# ---------------------------------------------------------------------------
# Main Entry Point
# ---------------------------------------------------------------------------
def main() -> int:
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Power Manager for dog-agent"
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("config.yaml"),
        help="Path to config.yaml"
    )
    parser.add_argument(
        "--port",
        type=int,
        default=9120,
        help="HTTP API port"
    )
    parser.add_argument(
        "--simulate",
        action="store_true",
        help="Run in simulation mode with fake battery"
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path("data"),
        help="Data directory for sleep state"
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable verbose logging"
    )

    args = parser.parse_args()

    if args.verbose:
        logger.setLevel(logging.DEBUG)
        for handler in logger.handlers:
            handler.setLevel(logging.DEBUG)

    # Load configuration
    config, config_data_dir = load_config(args.config)
    data_dir = args.data_dir if args.data_dir else config_data_dir

    logger.info("=" * 60)
    logger.info("Dog Agent Power Manager")
    logger.info("=" * 60)
    logger.info("Mode: %s", "SIMULATION" if args.simulate else "HARDWARE")
    logger.info("API Port: %d", args.port)
    logger.info("Data Directory: %s", data_dir)
    logger.info("Deep Sleep: %s", "enabled" if config.deep_sleep_enabled else "disabled")
    logger.info("Battery Capacity: %d mAh", config.capacity_mah)
    logger.info("Night Schedule: %s - %s", config.night_start, config.night_end)
    logger.info("=" * 60)

    # Create power manager
    power_manager = PowerManager(
        config=config,
        data_dir=data_dir,
        simulate=args.simulate
    )

    # Set handler's power manager
    PowerAPIHandler.power_manager = power_manager

    # Start power manager
    power_manager.start()

    # Start HTTP server
    server = HTTPServer(("", args.port), PowerAPIHandler)
    logger.info("HTTP API listening on port %d", args.port)

    # Setup signal handlers
    def signal_handler(signum: int, frame: Any) -> None:
        logger.info("Received signal %d, shutting down...", signum)
        power_manager.stop()
        server.shutdown()

    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    # Run server (blocks)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Keyboard interrupt received")
    finally:
        power_manager.stop()
        server.server_close()
        logger.info("Power Manager stopped")

    return 0


if __name__ == "__main__":
    sys.exit(main())
