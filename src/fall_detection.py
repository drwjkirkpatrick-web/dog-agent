#!/usr/bin/env python3
"""
Fall Detection Module — Dog Agent
=================================
Detects serious falls or collisions using the BNO055 9-DOF IMU.

Detection Algorithm:
  - Sudden high-G impact (>3G for >100ms)
  - Followed by inactivity (no movement for N seconds)
  - Or abnormal orientation (upside-down detection)

Severity Levels:
  - MINOR: Brief impact, quick recovery
  - MODERATE: Impact + 5-10 sec immobility
  - SEVERE: High impact + >10 sec no movement + abnormal orientation

Automatic Actions on Severe Fall:
  - Immediate alert to owner (via alert_manager)
  - Start high-rate GPS tracking
  - Activate voice module for calming
  - Log precise timestamp and location

False Positive Filtering:
  - Distinguish from normal play (rolling, wrestling)
  - Ignore repeated impacts within 30 seconds
  - Consider activity context from behavior module

HTTP API on port 9130:
  GET /fall/status      — detection enabled, last event
  GET /fall/history     — recent fall events
  POST /fall/test       — simulate fall for testing
  GET /fall/health      — module health

Usage:
    python src/fall_detection.py              # Normal mode (I2C hardware)
    python src/fall_detection.py --simulate   # Simulation mode
    python src/fall_detection.py --config /path/to/config.yaml
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
from collections import deque
from contextlib import suppress
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import yaml
import requests

try:
    import smbus2
except ImportError:
    smbus2 = None  # type: ignore

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logger = logging.getLogger("fall_detection")
_handler = logging.StreamHandler(sys.stdout)
_handler.setFormatter(logging.Formatter(
    "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
))
logger.addHandler(_handler)
logger.setLevel(logging.INFO)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
PROJECT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_DIR / "config.yaml"
DATA_DIR = PROJECT_DIR / "data"
FALL_LOG_DIR = DATA_DIR / "fall_events"
DEFAULT_PORT = 9130

# BNO055 Registers (same as environmental_sensors.py)
BNO055_REG_CHIP_ID = 0x00
BNO055_REG_OPR_MODE = 0x3D
BNO055_REG_PWR_MODE = 0x3E
BNO055_REG_SYS_TRIGGER = 0x3F
BNO055_REG_QUAT_DATA = 0x20
BNO055_REG_ACCEL_DATA = 0x08
BNO055_REG_GYRO_DATA = 0x14
BNO055_REG_CALIB_STAT = 0x35

BNO055_MODE_CONFIG = 0x00
BNO055_MODE_NDOF = 0x0C


class Severity(Enum):
    """Fall severity levels."""
    MINOR = "minor"
    MODERATE = "moderate"
    SEVERE = "severe"


# ---------------------------------------------------------------------------
# Data Classes
# ---------------------------------------------------------------------------
@dataclass
class FallEvent:
    """A single fall event with full telemetry."""
    timestamp: datetime
    severity: Severity
    
    # Impact data
    impact_accel_g: float = 0.0  # Peak acceleration magnitude in G
    impact_duration_ms: int = 0  # Duration above threshold
    
    # Post-fall state
    immobility_sec: float = 0.0  # Seconds of no movement
    orientation_before: Tuple[float, float, float] = (0.0, 0.0, 0.0)  # (pitch, roll, yaw)
    orientation_after: Tuple[float, float, float] = (0.0, 0.0, 0.0)
    is_upside_down: bool = False
    
    # Recovery
    recovered: bool = False
    recovery_time_sec: float = 0.0
    
    # Context
    location: Optional[Dict[str, float]] = None  # lat, lon, altitude
    activity_before: str = "unknown"  # resting, walking, running, playing
    
    # Actions taken
    alert_sent: bool = False
    gps_high_rate: bool = False
    voice_activated: bool = False
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "timestamp": self.timestamp.isoformat(),
            "severity": self.severity.value,
            "impact_accel_g": round(self.impact_accel_g, 2),
            "impact_duration_ms": self.impact_duration_ms,
            "immobility_sec": round(self.immobility_sec, 1),
            "orientation_before": [round(x, 2) for x in self.orientation_before],
            "orientation_after": [round(x, 2) for x in self.orientation_after],
            "is_upside_down": self.is_upside_down,
            "recovered": self.recovered,
            "recovery_time_sec": round(self.recovery_time_sec, 1),
            "location": self.location,
            "activity_before": self.activity_before,
            "alert_sent": self.alert_sent,
            "gps_high_rate": self.gps_high_rate,
            "voice_activated": self.voice_activated,
        }


@dataclass
class IMUReading:
    """Single IMU reading."""
    timestamp: float  # monotonic
    acceleration: Tuple[float, float, float]  # x, y, z in m/s^2
    linear_accel: Tuple[float, float, float]  # x, y, z in m/s^2 (no gravity)
    gravity: Tuple[float, float, float]  # x, y, z in m/s^2
    quaternion: Tuple[float, float, float, float]  # w, x, y, z
    gyroscope: Tuple[float, float, float]  # x, y, z in deg/s
    
    @property
    def accel_magnitude(self) -> float:
        """Total acceleration magnitude in G (9.8 m/s^2 = 1G)."""
        return math.sqrt(sum(a**2 for a in self.acceleration)) / 9.80665
    
    @property
    def linear_accel_magnitude(self) -> float:
        """Linear acceleration magnitude in G."""
        return math.sqrt(sum(a**2 for a in self.linear_accel)) / 9.80665
    
    @property
    def orientation(self) -> Tuple[float, float, float]:
        """Convert quaternion to pitch, roll, yaw in degrees."""
        w, x, y, z = self.quaternion
        
        # Roll (x-axis rotation)
        sinr_cosp = 2.0 * (w * x + y * z)
        cosr_cosp = 1.0 - 2.0 * (x * x + y * y)
        roll = math.atan2(sinr_cosp, cosr_cosp)
        
        # Pitch (y-axis rotation)
        sinp = 2.0 * (w * y - z * x)
        if abs(sinp) >= 1:
            pitch = math.copysign(math.pi / 2, sinp)
        else:
            pitch = math.asin(sinp)
        
        # Yaw (z-axis rotation)
        siny_cosp = 2.0 * (w * z + x * y)
        cosy_cosp = 1.0 - 2.0 * (y * y + z * z)
        yaw = math.atan2(siny_cosp, cosy_cosp)
        
        return (
            math.degrees(pitch),
            math.degrees(roll),
            math.degrees(yaw),
        )
    
    def is_upside_down(self, threshold_deg: float = 60.0) -> bool:
        """Check if device is upside-down based on gravity vector."""
        pitch, roll, _ = self.orientation
        return abs(pitch) > threshold_deg or abs(roll) > threshold_deg


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
@dataclass
class FallDetectionConfig:
    """Configuration for fall detection."""
    enabled: bool = True
    i2c_bus: int = 1
    bno055_addr: int = 0x28
    
    # Detection thresholds
    impact_threshold_g: float = 3.0
    impact_duration_ms: int = 100
    immobility_timeout_sec: float = 10.0
    orientation_timeout_sec: float = 5.0
    
    # Severity thresholds
    moderate_immobility_sec: float = 5.0
    severe_immobility_sec: float = 10.0
    
    # False positive filtering
    cooldown_sec: float = 30.0  # Ignore repeated impacts within 30 sec
    play_detection_enabled: bool = True
    play_accel_variance_threshold: float = 2.0  # High variance suggests play
    
    # Integration
    alert_manager_url: str = "http://127.0.0.1:9118"
    adaptive_gps_url: str = "http://127.0.0.1:9124"
    voice_url: str = "http://127.0.0.1:9110"
    behavior_url: str = "http://127.0.0.1:9110"
    
    @classmethod
    def from_dict(cls, config: Dict[str, Any]) -> "FallDetectionConfig":
        """Load from configuration dictionary."""
        fd_cfg = config.get("fall_detection", {})
        return cls(
            enabled=fd_cfg.get("enabled", True),
            i2c_bus=fd_cfg.get("i2c_bus", 1),
            bno055_addr=fd_cfg.get("bno055_addr", 0x28),
            impact_threshold_g=fd_cfg.get("impact_threshold_g", 3.0),
            impact_duration_ms=fd_cfg.get("impact_duration_ms", 100),
            immobility_timeout_sec=fd_cfg.get("immobility_timeout_sec", 10.0),
            orientation_timeout_sec=fd_cfg.get("orientation_timeout_sec", 5.0),
            moderate_immobility_sec=fd_cfg.get("moderate_immobility_sec", 5.0),
            severe_immobility_sec=fd_cfg.get("severe_immobility_sec", 10.0),
            cooldown_sec=fd_cfg.get("cooldown_sec", 30.0),
            play_detection_enabled=fd_cfg.get("play_detection_enabled", True),
            play_accel_variance_threshold=fd_cfg.get("play_accel_variance_threshold", 2.0),
            alert_manager_url=config.get("alerts", {}).get("api_url", "http://127.0.0.1:9118"),
            adaptive_gps_url=config.get("adaptive_gps", {}).get("api_url", "http://127.0.0.1:9124"),
            voice_url=config.get("voice", {}).get("api_url", "http://127.0.0.1:9110"),
            behavior_url=config.get("behavior", {}).get("api_url", "http://127.0.0.1:9110"),
        )


def load_config(path: str) -> Dict[str, Any]:
    """Load YAML configuration."""
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        return yaml.safe_load(f) or {}


# ---------------------------------------------------------------------------
# BNO055 IMU Driver
# ---------------------------------------------------------------------------
class BNO055Driver:
    """Driver for BNO055 9-DOF IMU."""
    
    def __init__(self, bus_num: int, address: int = 0x28):
        if smbus2 is None:
            raise ImportError("smbus2 is required. Install with: pip install smbus2")
        
        self._bus = smbus2.SMBus(bus_num)
        self._address = address
        self._initialized = False
        self._init_sensor()
    
    def _init_sensor(self) -> None:
        """Initialize BNO055 sensor."""
        # Check chip ID
        chip_id = self._bus.read_byte_data(self._address, BNO055_REG_CHIP_ID)
        if chip_id != 0xA0:
            logger.warning(f"Unexpected BNO055 chip ID: 0x{chip_id:02X}")
        
        # Switch to config mode
        self._bus.write_byte_data(self._address, BNO055_REG_OPR_MODE, BNO055_MODE_CONFIG)
        time.sleep(0.025)
        
        # Reset
        self._bus.write_byte_data(self._address, BNO055_REG_SYS_TRIGGER, 0x20)
        time.sleep(0.65)
        
        # Set power mode to normal
        self._bus.write_byte_data(self._address, BNO055_REG_PWR_MODE, 0x00)
        time.sleep(0.01)
        
        # Set to NDOF mode (fusion mode with all sensors)
        self._bus.write_byte_data(self._address, BNO055_REG_OPR_MODE, BNO055_MODE_NDOF)
        time.sleep(0.025)
        
        self._initialized = True
        logger.info("BNO055 initialized successfully")
    
    @staticmethod
    def _int16(msb: int, lsb: int) -> int:
        """Convert two bytes to signed 16-bit integer."""
        val = (msb << 8) | lsb
        return val - 65536 if val > 32767 else val
    
    def read(self) -> Optional[IMUReading]:
        """Read current IMU data."""
        if not self._initialized:
            return None
        
        try:
            # Read acceleration (x, y, z) - 16-bit signed / 100 m/s^2
            accel_data = self._bus.read_i2c_block_data(self._address, BNO055_REG_ACCEL_DATA, 6)
            accel = tuple(
                self._int16(accel_data[i], accel_data[i+1]) / 100.0
                for i in range(0, 6, 2)
            )
            
            # Read linear acceleration (no gravity)
            lin_accel_data = self._bus.read_i2c_block_data(self._address, 0x28, 6)
            lin_accel = tuple(
                self._int16(lin_accel_data[i], lin_accel_data[i+1]) / 100.0
                for i in range(0, 6, 2)
            )
            
            # Read gravity vector
            grav_data = self._bus.read_i2c_block_data(self._address, 0x2E, 6)
            gravity = tuple(
                self._int16(grav_data[i], grav_data[i+1]) / 100.0
                for i in range(0, 6, 2)
            )
            
            # Read quaternion (w, x, y, z)
            quat_data = self._bus.read_i2c_block_data(self._address, BNO055_REG_QUAT_DATA, 8)
            quaternion = tuple(
                self._int16(quat_data[i], quat_data[i+1]) / 16384.0
                for i in range(0, 8, 2)
            )
            
            # Read gyroscope
            gyro_data = self._bus.read_i2c_block_data(self._address, BNO055_REG_GYRO_DATA, 6)
            gyro = tuple(
                self._int16(gyro_data[i], gyro_data[i+1]) / 16.0
                for i in range(0, 6, 2)
            )
            
            return IMUReading(
                timestamp=time.monotonic(),
                acceleration=accel,
                linear_accel=lin_accel,
                gravity=gravity,
                quaternion=quaternion,
                gyroscope=gyro,
            )
        except Exception as e:
            logger.error(f"BNO055 read error: {e}")
            return None
    
    def close(self) -> None:
        """Close I2C bus."""
        with suppress(Exception):
            self._bus.close()


# ---------------------------------------------------------------------------
# Simulated IMU for testing
# ---------------------------------------------------------------------------
class SimulatedIMU:
    """Generates simulated IMU data with occasional fall events."""
    
    def __init__(self, seed: int = 42) -> None:
        import random
        self._rng = random.Random(seed)
        self._t0 = time.monotonic()
        self._fall_scheduled: Optional[float] = None
        self._in_fall: bool = False
        self._fall_start: float = 0
        self._normal_orientation = (0.0, 0.0, 1.0)  # upright
        
    def schedule_fall(self, delay_sec: float = 0.0) -> None:
        """Schedule a simulated fall."""
        self._fall_scheduled = time.monotonic() + delay_sec
        logger.info(f"Scheduled simulated fall in {delay_sec:.1f} seconds")
    
    def read(self) -> IMUReading:
        """Generate simulated IMU reading."""
        now = time.monotonic()
        t = now - self._t0
        
        # Check if fall is scheduled
        if self._fall_scheduled and now >= self._fall_scheduled:
            self._in_fall = True
            self._fall_start = now
            self._fall_scheduled = None
            logger.info("SIMULATION: Fall event triggered")
        
        if self._in_fall:
            # Simulate fall physics
            fall_elapsed = now - self._fall_start
            
            if fall_elapsed < 0.1:  # Impact phase
                # High acceleration spike
                accel = (
                    self._rng.gauss(0, 5),
                    self._rng.gauss(0, 5),
                    -self._rng.uniform(25, 35),  # 3-4G impact
                )
                # Tumbling orientation
                quat = (
                    self._rng.gauss(0, 0.5),
                    self._rng.gauss(0.3, 0.2),
                    self._rng.gauss(0, 0.2),
                    self._rng.gauss(0, 0.1),
                )
            elif fall_elapsed < 15.0:  # Post-fall immobility
                # Low acceleration, possibly upside down
                accel = (
                    self._rng.gauss(0, 0.1),
                    self._rng.gauss(0, 0.1),
                    self._rng.gauss(-5, 0.5),  # May be upside down
                )
                # Upside down orientation
                quat = (0.0, 1.0, 0.0, 0.0)  # 180 degree rotation
            else:  # Recovery
                self._in_fall = False
                accel = (
                    self._rng.gauss(0, 0.2),
                    self._rng.gauss(0, 0.2),
                    9.8 + self._rng.gauss(0, 0.2),
                )
                quat = (1.0, 0.0, 0.0, 0.0)  # Upright
        else:
            # Normal movement
            activity = self._rng.choice(["resting", "walking", "running", "playing"])
            
            if activity == "resting":
                noise = 0.05
                base_accel = (0.0, 0.0, 9.8)
            elif activity == "walking":
                noise = 0.3
                base_accel = (
                    math.sin(t * 2) * 0.5,
                    math.cos(t * 2) * 0.3,
                    9.8 + math.sin(t * 4) * 0.2,
                )
            elif activity == "running":
                noise = 0.8
                base_accel = (
                    math.sin(t * 5) * 1.5,
                    math.cos(t * 5) * 0.8,
                    9.8 + math.sin(t * 10) * 0.5,
                )
            else:  # playing
                noise = 1.5
                base_accel = (
                    self._rng.gauss(0, 1.0),
                    self._rng.gauss(0, 1.0),
                    9.8 + self._rng.gauss(0, 0.5),
                )
            
            accel = tuple(
                base_accel[i] + self._rng.gauss(0, noise)
                for i in range(3)
            )
            
            # Normal upright orientation with small variations
            quat = (
                1.0 + self._rng.gauss(0, 0.02),
                self._rng.gauss(0, 0.05),
                self._rng.gauss(0, 0.05),
                self._rng.gauss(0, 0.02),
            )
            # Normalize
            norm = math.sqrt(sum(x**2 for x in quat))
            quat = tuple(x / norm for x in quat)
        
        # Calculate linear acceleration (subtract gravity)
        gravity = (0.0, 0.0, 9.8)  # Simplified
        lin_accel = tuple(accel[i] - gravity[i] for i in range(3))
        
        return IMUReading(
            timestamp=now,
            acceleration=accel,
            linear_accel=lin_accel,
            gravity=gravity,
            quaternion=quat,
            gyroscope=(
                self._rng.gauss(0, 0.5),
                self._rng.gauss(0, 0.5),
                self._rng.gauss(0, 0.5),
            ),
        )


# ---------------------------------------------------------------------------
# Fall Detection Engine
# ---------------------------------------------------------------------------
class FallDetector:
    """
    Core fall detection engine.
    
    Monitors IMU data for:
    1. High-G impact events (>threshold for >duration)
    2. Post-impact inactivity
    3. Abnormal orientation
    
    Classifies severity based on impact magnitude and recovery time.
    """
    
    def __init__(self, config: FallDetectionConfig):
        self.config = config
        
        # State machine
        self._state = "monitoring"  # monitoring, impact_detected, analyzing
        self._state_lock = threading.Lock()
        
        # Impact detection
        self._impact_start: Optional[float] = None
        self._peak_accel: float = 0.0
        self._impact_orientation: Optional[Tuple[float, float, float]] = None
        
        # Post-fall analysis
        self._fall_detected_time: Optional[float] = None
        self._immobility_start: Optional[float] = None
        self._post_fall_readings: List[IMUReading] = []
        
        # History and cooldown
        self._fall_history: deque = deque(maxlen=100)
        self._last_fall_time: Optional[float] = None
        self._cooldown_active: bool = False
        
        # Activity context (from behavior module)
        self._current_activity = "unknown"
        self._last_activity_update = 0.0
        
        # Current reading
        self._current_reading: Optional[IMUReading] = None
        self._reading_lock = threading.Lock()
        
        # Callbacks for severe falls
        self._severe_fall_callbacks: List[Callable[[FallEvent], None]] = []
        
        # Running flag
        self._running = False
        self._thread: Optional[threading.Thread] = None
    
    def add_severe_fall_callback(self, callback: Callable[[FallEvent], None]) -> None:
        """Register a callback for severe fall events."""
        self._severe_fall_callbacks.append(callback)
    
    def get_activity_context(self) -> str:
        """Get current activity context from behavior module."""
        # Cache activity for 30 seconds
        if time.monotonic() - self._last_activity_update < 30:
            return self._current_activity
        
        try:
            resp = requests.get(
                f"{self.config.behavior_url}/behavior/current",
                timeout=2.0
            )
            if resp.status_code == 200:
                data = resp.json()
                self._current_activity = data.get("activity_state", "unknown")
                self._last_activity_update = time.monotonic()
        except Exception:
            pass
        
        return self._current_activity
    
    def is_play_activity(self, recent_readings: List[IMUReading]) -> bool:
        """Detect if current activity is play (high variance in acceleration)."""
        if not self.config.play_detection_enabled or len(recent_readings) < 10:
            return False
        
        # Calculate variance in acceleration magnitudes
        magnitudes = [r.accel_magnitude for r in recent_readings[-20:]]
        if len(magnitudes) < 10:
            return False
        
        mean_mag = sum(magnitudes) / len(magnitudes)
        variance = sum((m - mean_mag) ** 2 for m in magnitudes) / len(magnitudes)
        
        return variance > self.config.play_accel_variance_threshold
    
    def _check_cooldown(self) -> bool:
        """Check if we're within the cooldown period after a fall."""
        if self._last_fall_time is None:
            return False
        elapsed = time.monotonic() - self._last_fall_time
        return elapsed < self.config.cooldown_sec
    
    def _detect_impact(self, reading: IMUReading) -> bool:
        """Detect if current reading indicates an impact."""
        return reading.accel_magnitude >= self.config.impact_threshold_g
    
    def _classify_severity(self, 
                          impact_accel: float,
                          immobility_sec: float,
                          is_upside_down: bool) -> Severity:
        """Classify fall severity based on telemetry."""
        # SEVERE: High impact + long immobility + abnormal orientation
        if (impact_accel >= self.config.impact_threshold_g * 1.5 and
            immobility_sec >= self.config.severe_immobility_sec and
            is_upside_down):
            return Severity.SEVERE
        
        # MODERATE: Impact + moderate immobility
        if (impact_accel >= self.config.impact_threshold_g and
            immobility_sec >= self.config.moderate_immobility_sec):
            return Severity.MODERATE
        
        # MINOR: Brief impact, quick recovery
        return Severity.MINOR
    
    def _process_reading(self, reading: IMUReading) -> Optional[FallEvent]:
        """Process a single IMU reading and detect falls."""
        with self._state_lock:
            now = reading.timestamp
            
            # Update current reading
            with self._reading_lock:
                self._current_reading = reading
            
            # Check cooldown
            if self._check_cooldown():
                return None
            
            # State machine
            if self._state == "monitoring":
                # Look for impact
                if self._detect_impact(reading):
                    self._impact_start = now
                    self._peak_accel = reading.accel_magnitude
                    self._impact_orientation = reading.orientation
                    self._state = "impact_detected"
                    logger.debug(f"Impact detected: {reading.accel_magnitude:.2f}G")
                
            elif self._state == "impact_detected":
                # Track impact duration and peak
                impact_duration_ms = (now - self._impact_start) * 1000 if self._impact_start else 0
                
                if reading.accel_magnitude >= self.config.impact_threshold_g:
                    # Still in impact
                    self._peak_accel = max(self._peak_accel, reading.accel_magnitude)
                    
                    # Check if impact duration exceeded threshold
                    if impact_duration_ms >= self.config.impact_duration_ms:
                        # Impact confirmed, start post-fall analysis
                        self._fall_detected_time = now
                        self._immobility_start = now
                        self._post_fall_readings = [reading]
                        self._state = "analyzing"
                        logger.info(f"Impact confirmed: {self._peak_accel:.2f}G for {impact_duration_ms:.0f}ms")
                else:
                    # Impact ended too quickly - might be false positive
                    if impact_duration_ms < self.config.impact_duration_ms:
                        logger.debug(f"Impact too brief: {impact_duration_ms:.0f}ms")
                        self._state = "monitoring"
                    else:
                        # Valid but short impact
                        self._fall_detected_time = now
                        self._immobility_start = now
                        self._post_fall_readings = [reading]
                        self._state = "analyzing"
            
            elif self._state == "analyzing":
                # Collect post-fall readings
                self._post_fall_readings.append(reading)
                
                # Check for recovery (movement)
                linear_mag = reading.linear_accel_magnitude
                if linear_mag > 0.3:  # Movement detected
                    # Recovery detected
                    immobility_sec = now - self._immobility_start if self._immobility_start else 0
                    
                    # Classify severity
                    is_upside_down = any(r.is_upside_down() for r in self._post_fall_readings[-5:])
                    
                    severity = self._classify_severity(
                        self._peak_accel,
                        immobility_sec,
                        is_upside_down
                    )
                    
                    # Get activity context
                    activity_before = self.get_activity_context()
                    
                    # Create fall event
                    event = FallEvent(
                        timestamp=datetime.now(timezone.utc),
                        severity=severity,
                        impact_accel_g=self._peak_accel,
                        impact_duration_ms=int((now - self._impact_start) * 1000) if self._impact_start else 0,
                        immobility_sec=immobility_sec,
                        orientation_before=self._impact_orientation or (0.0, 0.0, 0.0),
                        orientation_after=reading.orientation,
                        is_upside_down=is_upside_down,
                        recovered=True,
                        recovery_time_sec=immobility_sec,
                        activity_before=activity_before,
                    )
                    
                    # Reset state
                    self._state = "monitoring"
                    self._last_fall_time = now
                    
                    # Store in history
                    self._fall_history.append(event)
                    
                    logger.info(f"Fall detected: {severity.value} severity, "
                               f"{self._peak_accel:.2f}G impact, "
                               f"{immobility_sec:.1f}s immobility")
                    
                    return event
                
                # Check for timeout (no recovery within timeout period)
                elapsed = now - self._fall_detected_time if self._fall_detected_time else 0
                if elapsed > self.config.immobility_timeout_sec:
                    # No recovery detected - severe fall
                    is_upside_down = any(r.is_upside_down() for r in self._post_fall_readings[-10:])
                    activity_before = self.get_activity_context()
                    
                    event = FallEvent(
                        timestamp=datetime.now(timezone.utc),
                        severity=Severity.SEVERE,
                        impact_accel_g=self._peak_accel,
                        impact_duration_ms=int((now - self._impact_start) * 1000) if self._impact_start else 0,
                        immobility_sec=elapsed,
                        orientation_before=self._impact_orientation or (0.0, 0.0, 0.0),
                        orientation_after=reading.orientation,
                        is_upside_down=is_upside_down,
                        recovered=False,
                        recovery_time_sec=0.0,
                        activity_before=activity_before,
                    )
                    
                    # Reset state
                    self._state = "monitoring"
                    self._last_fall_time = now
                    
                    # Store in history
                    self._fall_history.append(event)
                    
                    logger.warning(f"Severe fall detected: no recovery after {elapsed:.1f}s")
                    
                    return event
        
        return None
    
    def start(self) -> None:
        """Start the fall detection engine."""
        self._running = True
        self._thread = threading.Thread(target=self._run, name="fall-detector", daemon=True)
        self._thread.start()
        logger.info("Fall detection engine started")
    
    def stop(self) -> None:
        """Stop the fall detection engine."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=2.0)
        logger.info("Fall detection engine stopped")
    
    def _run(self) -> None:
        """Main detection loop - to be overridden by sensor reader."""
        pass
    
    def get_last_event(self) -> Optional[FallEvent]:
        """Get the most recent fall event."""
        if self._fall_history:
            return self._fall_history[-1]
        return None
    
    def get_history(self, count: int = 10) -> List[FallEvent]:
        """Get recent fall events."""
        return list(self._fall_history)[-count:]
    
    def get_status(self) -> Dict[str, Any]:
        """Get current detection status."""
        with self._reading_lock:
            reading = self._current_reading
        
        last_event = self.get_last_event()
        
        return {
            "enabled": self.config.enabled,
            "state": self._state,
            "cooldown_active": self._check_cooldown(),
            "current_accel_g": round(reading.accel_magnitude, 2) if reading else None,
            "last_fall": last_event.to_dict() if last_event else None,
            "total_falls_detected": len(self._fall_history),
            "config": {
                "impact_threshold_g": self.config.impact_threshold_g,
                "immobility_timeout_sec": self.config.immobility_timeout_sec,
                "cooldown_sec": self.config.cooldown_sec,
            }
        }


# ---------------------------------------------------------------------------
# Fall Detection Manager (with hardware/simulation)
# ---------------------------------------------------------------------------
class FallDetectionManager:
    """Manages fall detection with hardware or simulated IMU."""
    
    def __init__(self, config: FallDetectionConfig, simulate: bool = False):
        self.config = config
        self.simulate = simulate
        
        # Initialize IMU
        if simulate:
            self.imu = SimulatedIMU()
            logger.info("Using simulated IMU")
        else:
            try:
                self.imu = BNO055Driver(config.i2c_bus, config.bno055_addr)
                logger.info("BNO055 IMU initialized")
            except Exception as e:
                logger.error(f"Failed to initialize BNO055: {e}")
                logger.info("Falling back to simulation mode")
                self.imu = SimulatedIMU()
                self.simulate = True
        
        # Initialize detector
        self.detector = FallDetector(config)
        self.detector.add_severe_fall_callback(self._on_severe_fall)
        
        # Running state
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
    
    def _on_severe_fall(self, event: FallEvent) -> None:
        """Handle severe fall - trigger all automatic actions."""
        logger.warning(f"SEVERE FALL DETECTED - triggering emergency response")
        
        # Get current location
        try:
            resp = requests.get("http://127.0.0.1:9123/location", timeout=2.0)
            if resp.status_code == 200:
                loc_data = resp.json()
                event.location = {
                    "lat": loc_data.get("lat"),
                    "lon": loc_data.get("lon"),
                    "altitude": loc_data.get("altitude"),
                }
        except Exception as e:
            logger.warning(f"Could not get location: {e}")
        
        # 1. Send alert to owner
        try:
            alert_msg = (
                f"🚨 <b>SEVERE FALL DETECTED</b>\n\n"
                f"Impact: {event.impact_accel_g:.1f}G\n"
                f"Immobility: {event.immobility_sec:.1f}s\n"
                f"Upside-down: {'Yes' if event.is_upside_down else 'No'}\n"
            )
            if event.location:
                alert_msg += f"\n📍 Location: {event.location['lat']:.5f}, {event.location['lon']:.5f}"
            
            requests.post(
                f"{self.config.alert_manager_url}/alerts",
                json={
                    "alert_type": "severe_fall",
                    "severity": "critical",
                    "message": alert_msg,
                    "data": event.to_dict(),
                },
                timeout=5.0
            )
            event.alert_sent = True
            logger.info("Alert sent to owner")
        except Exception as e:
            logger.error(f"Failed to send alert: {e}")
        
        # 2. Start high-rate GPS tracking
        try:
            requests.post(
                f"{self.config.adaptive_gps_url}/gps/mode",
                json={"mode": "high_rate", "duration_min": 10},
                timeout=2.0
            )
            event.gps_high_rate = True
            logger.info("High-rate GPS tracking activated")
        except Exception as e:
            logger.warning(f"Could not activate high-rate GPS: {e}")
        
        # 3. Activate voice module for calming
        try:
            requests.post(
                f"{self.config.voice_url}/voice/say",
                json={"text": "It's okay, I'm here. Everything is alright.",
                      "priority": "high"},
                timeout=3.0
            )
            event.voice_activated = True
            logger.info("Voice calming activated")
        except Exception as e:
            logger.warning(f"Could not activate voice: {e}")
        
        # Log the event with full telemetry
        self._log_fall_event(event)
    
    def _log_fall_event(self, event: FallEvent) -> None:
        """Log fall event to file."""
        FALL_LOG_DIR.mkdir(parents=True, exist_ok=True)
        log_file = FALL_LOG_DIR / f"fall_events_{datetime.now():%Y-%m-%d}.jsonl"
        
        with open(log_file, "a") as f:
            f.write(json.dumps(event.to_dict(), default=str) + "\n")
        
        logger.info(f"Fall event logged to {log_file}")
    
    def _run_detection_loop(self) -> None:
        """Main IMU reading and detection loop."""
        poll_interval = 0.01  # 100 Hz for IMU
        recent_readings: deque = deque(maxlen=100)
        
        while not self._stop_event.is_set():
            try:
                # Read IMU
                reading = self.imu.read()
                if reading is None:
                    time.sleep(poll_interval)
                    continue
                
                recent_readings.append(reading)
                
                # Check for play activity (false positive filter)
                if self.detector.is_play_activity(list(recent_readings)):
                    # Skip detection during play
                    continue
                
                # Process reading
                event = self.detector._process_reading(reading)
                
                if event and event.severity == Severity.SEVERE:
                    # Trigger severe fall callback
                    for callback in self.detector._severe_fall_callbacks:
                        try:
                            callback(event)
                        except Exception as e:
                            logger.error(f"Severe fall callback error: {e}")
                
                time.sleep(poll_interval)
                
            except Exception as e:
                logger.error(f"Detection loop error: {e}")
                time.sleep(0.1)
    
    def start(self) -> None:
        """Start fall detection."""
        if not self.config.enabled:
            logger.info("Fall detection is disabled in config")
            return
        
        self._running = True
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_detection_loop, daemon=True)
        self._thread.start()
        logger.info("Fall detection manager started")
    
    def stop(self) -> None:
        """Stop fall detection."""
        self._running = False
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=2.0)
        if hasattr(self.imu, 'close'):
            self.imu.close()
        logger.info("Fall detection manager stopped")
    
    def trigger_test_fall(self, severity: Severity = Severity.SEVERE) -> FallEvent:
        """Trigger a simulated fall for testing."""
        if not isinstance(self.imu, SimulatedIMU):
            logger.warning("Test fall only available in simulation mode")
            # Create a manual fall event
            event = FallEvent(
                timestamp=datetime.now(timezone.utc),
                severity=severity,
                impact_accel_g=4.5,
                impact_duration_ms=150,
                immobility_sec=12.0,
                is_upside_down=True,
                recovered=False,
                activity_before="walking",
            )
            self.detector._fall_history.append(event)
            if severity == Severity.SEVERE:
                self._on_severe_fall(event)
            return event
        
        # Schedule simulated fall
        self.imu.schedule_fall(delay_sec=0.5)
        
        # Wait for fall detection
        timeout = time.monotonic() + 5.0
        while time.monotonic() < timeout:
            last = self.detector.get_last_event()
            if last and (datetime.now(timezone.utc) - last.timestamp).total_seconds() < 2:
                return last
            time.sleep(0.1)
        
        raise TimeoutError("Test fall was not detected within timeout")


# ---------------------------------------------------------------------------
# HTTP API Handler
# ---------------------------------------------------------------------------
class FallDetectionHandler(BaseHTTPRequestHandler):
    """HTTP request handler for fall detection API."""
    
    manager: Optional[FallDetectionManager] = None
    
    def log_message(self, format, *args):
        logger.debug(f"HTTP: {format % args}")
    
    def _send_json(self, data, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data, indent=2, default=str).encode())
    
    def _send_error(self, status, message):
        self._send_json({"error": message}, status)
    
    def do_GET(self):
        """Handle GET requests."""
        if self.manager is None:
            self._send_error(503, "Fall detection not initialized")
            return
        
        path = self.path
        
        if path == "/fall/status":
            status = self.manager.detector.get_status()
            self._send_json(status)
        
        elif path == "/fall/history":
            history = self.manager.detector.get_history(count=50)
            self._send_json({
                "events": [e.to_dict() for e in history],
                "count": len(history),
            })
        
        elif path == "/fall/health":
            self._send_json({
                "status": "healthy",
                "enabled": self.manager.config.enabled,
                "simulation_mode": self.manager.simulate,
                "detector_state": self.manager.detector._state,
            })
        
        else:
            self._send_error(404, "Not found")
    
    def do_POST(self):
        """Handle POST requests."""
        if self.manager is None:
            self._send_error(503, "Fall detection not initialized")
            return
        
        path = self.path
        
        if path == "/fall/test":
            # Parse optional severity from body
            content_length = int(self.headers.get('Content-Length', 0))
            severity = Severity.SEVERE
            if content_length > 0:
                try:
                    body = self.rfile.read(content_length).decode()
                    data = json.loads(body)
                    severity_str = data.get("severity", "severe")
                    severity = Severity(severity_str)
                except Exception:
                    pass
            
            try:
                event = self.manager.trigger_test_fall(severity)
                self._send_json({
                    "success": True,
                    "message": f"Test {severity.value} fall triggered",
                    "event": event.to_dict(),
                })
            except Exception as e:
                self._send_error(500, f"Test fall failed: {e}")
        
        else:
            self._send_error(404, "Not found")


# ---------------------------------------------------------------------------
# Main Server
# ---------------------------------------------------------------------------
class FallDetectionServer:
    """HTTP server for fall detection API."""
    
    def __init__(self, manager: FallDetectionManager, port: int = DEFAULT_PORT):
        self.manager = manager
        self.port = port
        self.server: Optional[HTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
    
    def start(self) -> None:
        """Start the HTTP server."""
        FallDetectionHandler.manager = self.manager
        self.server = HTTPServer(("0.0.0.0", self.port), FallDetectionHandler)
        
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()
        logger.info(f"Fall detection HTTP server started on port {self.port}")
    
    def _serve(self) -> None:
        """Server loop."""
        while not self._stop_event.is_set():
            try:
                self.server.handle_request()
            except Exception as e:
                if not self._stop_event.is_set():
                    logger.error(f"Server error: {e}")
    
    def stop(self) -> None:
        """Stop the HTTP server."""
        self._stop_event.set()
        if self.server:
            self.server.shutdown()
        if self._thread:
            self._thread.join(timeout=2.0)
        logger.info("HTTP server stopped")


# ---------------------------------------------------------------------------
# Main Entry Point
# ---------------------------------------------------------------------------
def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(description="Dog Agent Fall Detection Module")
    parser.add_argument("--config", type=str, default=str(DEFAULT_CONFIG_PATH),
                       help="Path to config.yaml")
    parser.add_argument("--simulate", action="store_true",
                       help="Run in simulation mode")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                       help="HTTP API port")
    parser.add_argument("--test-fall", action="store_true",
                       help="Trigger a test fall and exit")
    args = parser.parse_args()
    
    # Setup logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    
    # Load configuration
    config_dict = load_config(args.config)
    config = FallDetectionConfig.from_dict(config_dict)
    
    # Create manager
    manager = FallDetectionManager(config, simulate=args.simulate)
    
    # Test mode
    if args.test_fall:
        print("Triggering test fall...")
        event = manager.trigger_test_fall(Severity.SEVERE)
        print(json.dumps(event.to_dict(), indent=2))
        return
    
    # Start detection
    manager.start()
    
    # Start HTTP server
    server = FallDetectionServer(manager, port=args.port)
    server.start()
    
    # Setup signal handlers
    def shutdown(signum, frame):
        logger.info("Shutting down...")
        server.stop()
        manager.stop()
        sys.exit(0)
    
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    
    # Keep running
    logger.info("Fall detection running. Press Ctrl+C to stop.")
    while True:
        time.sleep(1)


if __name__ == "__main__":
    main()
