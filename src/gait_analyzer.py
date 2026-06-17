#!/usr/bin/env python3
"""
Gait Analyzer Module — Dog Agent v5.0
=====================================
Analyzes walking rhythm, step symmetry, and stride regularity using the
BNO055 9-DOF IMU. Detects limping / lameness and tracks gait trends over
multiple days.

Features
--------
* Step detection with timestamps
* Step interval regularity (coefficient of variation)
* Left/right step symmetry estimate (lateral acceleration asymmetry)
* Stride length estimation via IMU double integration
* Gait change detection vs per-dog baseline
* Limping detection: asymmetric timing, reduced stride length, irregular rhythm
* Health insight messages
* SQLite persistence for history and baselines

HTTP API on port 9149:
  GET /gait/status   — current gait state
  GET /gait/today    — today's summary
  GET /gait/history  — last N days summary (default 7)
  GET /gait/health   — module health

Usage:
    python src/gait_analyzer.py              # Normal mode (I2C hardware)
    python src/gait_analyzer.py --simulate   # Simulation mode
    python src/gait_analyzer.py --config /path/to/config.yaml
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import signal
import sqlite3
import sys
import threading
import time
from collections import deque
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, Tuple

import yaml

try:
    import smbus2
except ImportError:
    smbus2 = None  # type: ignore

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logger = logging.getLogger("gait_analyzer")
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
DATA_DIR = PROJECT_DIR / "data" / "gait"
DEFAULT_PORT = 9149

G = 9.80665

# BNO055 registers (aligned with environmental_sensors.py / fall_detection.py)
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


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
@dataclass
class GaitAnalyzerConfig:
    """Configuration for the gait analyzer."""
    enabled: bool = True
    sample_rate_hz: int = 50
    baseline_days: int = 7
    i2c_bus: int = 1
    bno055_addr: int = 0x28
    api_port: int = DEFAULT_PORT
    db_path: Path = field(default_factory=lambda: DATA_DIR / "gait.db")

    # Step detection
    step_threshold_g: float = 1.15          # min upward accel spike over gravity
    step_min_interval_ms: float = 250.0     # ignore quicker peaks (max ~4 steps/sec)
    step_window_ms: int = 150               # width of impact phase

    # Stride estimation
    stride_integration_window_ms: int = 600  # integration window per step
    drift_correction_hz: float = 0.5           # high-pass-ish drift zeroing

    # Symmetry / limping thresholds
    asymmetry_threshold: float = 0.20         # 20% side-to-side difference
    cv_threshold: float = 0.25                # irregular rhythm threshold
    stride_drop_threshold: float = 0.20       # 20% reduction vs baseline
    baseline_change_threshold: float = 0.15   # 15% change flags trend insight

    @classmethod
    def from_dict(cls, config: Dict[str, Any]) -> "GaitAnalyzerConfig":
        ga = config.get("gait_analyzer", {})
        db_path = ga.get("db_path")
        if db_path:
            db_path = Path(db_path)
        else:
            db_path = DATA_DIR / "gait.db"
        return cls(
            enabled=ga.get("enabled", True),
            sample_rate_hz=ga.get("sample_rate_hz", 50),
            baseline_days=ga.get("baseline_days", 7),
            i2c_bus=ga.get("i2c_bus", 1),
            bno055_addr=ga.get("bno055_addr", 0x28),
            api_port=ga.get("api_port", DEFAULT_PORT),
            db_path=db_path,
            step_threshold_g=ga.get("step_threshold_g", 1.15),
            step_min_interval_ms=ga.get("step_min_interval_ms", 250.0),
            step_window_ms=ga.get("step_window_ms", 150),
            stride_integration_window_ms=ga.get("stride_integration_window_ms", 600),
            drift_correction_hz=ga.get("drift_correction_hz", 0.5),
            asymmetry_threshold=ga.get("asymmetry_threshold", 0.20),
            cv_threshold=ga.get("cv_threshold", 0.25),
            stride_drop_threshold=ga.get("stride_drop_threshold", 0.20),
            baseline_change_threshold=ga.get("baseline_change_threshold", 0.15),
        )


def load_config(path: str) -> Dict[str, Any]:
    """Load YAML configuration."""
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        return yaml.safe_load(f) or {}


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------
@dataclass
class IMUSample:
    """Single normalized IMU sample."""
    timestamp: float  # monotonic seconds
    accel: Tuple[float, float, float]  # m/s^2
    gyro: Tuple[float, float, float]  # deg/s
    quat: Tuple[float, float, float, float]

    @property
    def accel_magnitude(self) -> float:
        return math.sqrt(sum(a ** 2 for a in self.accel))

    @property
    def vertical_accel(self) -> float:
        """Up component in sensor frame; approximate with Z."""
        return self.accel[2]

    @property
    def lateral_accel(self) -> float:
        """Left-right component; approximate with X."""
        return self.accel[0]


@dataclass
class Step:
    """Detected step with derived metrics."""
    timestamp: float
    timestamp_utc: datetime
    stride_length_m: float
    lateral_asymmetry: float  # signed left(-) / right(+) dominance, normalized
    impact_accel_g: float


@dataclass
class GaitState:
    """Current real-time gait state."""
    moving: bool = False
    step_count_session: int = 0
    last_step_time: Optional[float] = None
    cadence_steps_per_min: float = 0.0
    avg_stride_m: float = 0.0
    current_symmetry_score: float = 1.0  # 1.0 = perfectly symmetric
    current_cv: float = 0.0
    limping_detected: bool = False
    limping_reasons: List[str] = field(default_factory=list)
    insights: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# BNO055 driver (compact, self-contained)
# ---------------------------------------------------------------------------
class BNO055Driver:
    """Minimal BNO055 driver returning calibrated IMU samples."""

    def __init__(self, bus_num: int, address: int = 0x28):
        if smbus2 is None:
            raise ImportError("smbus2 is required. Install with: pip install smbus2")
        self._bus = smbus2.SMBus(bus_num)
        self._address = address
        self._initialized = False
        self._init_sensor()

    def _init_sensor(self) -> None:
        chip_id = self._bus.read_byte_data(self._address, BNO055_REG_CHIP_ID)
        if chip_id != 0xA0:
            logger.warning("Unexpected BNO055 chip ID: 0x%02X", chip_id)

        self._bus.write_byte_data(self._address, BNO055_REG_OPR_MODE, BNO055_MODE_CONFIG)
        time.sleep(0.025)
        self._bus.write_byte_data(self._address, BNO055_REG_SYS_TRIGGER, 0x20)
        time.sleep(0.65)
        self._bus.write_byte_data(self._address, BNO055_REG_PWR_MODE, 0x00)
        time.sleep(0.01)
        self._bus.write_byte_data(self._address, BNO055_REG_OPR_MODE, BNO055_MODE_NDOF)
        time.sleep(0.025)
        self._initialized = True
        logger.info("BNO055 initialized for gait analysis")

    @staticmethod
    def _int16(msb: int, lsb: int) -> int:
        val = (msb << 8) | lsb
        return val - 65536 if val > 32767 else val

    def read(self) -> Optional[IMUSample]:
        if not self._initialized:
            return None
        try:
            accel_data = self._bus.read_i2c_block_data(self._address, BNO055_REG_ACCEL_DATA, 6)
            accel = tuple(self._int16(accel_data[i], accel_data[i + 1]) / 100.0 for i in range(0, 6, 2))

            gyro_data = self._bus.read_i2c_block_data(self._address, BNO055_REG_GYRO_DATA, 6)
            gyro = tuple(self._int16(gyro_data[i], gyro_data[i + 1]) / 16.0 for i in range(0, 6, 2))

            quat_data = self._bus.read_i2c_block_data(self._address, BNO055_REG_QUAT_DATA, 8)
            quat = tuple(self._int16(quat_data[i], quat_data[i + 1]) / 16384.0 for i in range(0, 8, 2))

            return IMUSample(timestamp=time.monotonic(), accel=accel, gyro=gyro, quat=quat)
        except Exception as e:
            logger.error("BNO055 read error: %s", e)
            return None

    def close(self) -> None:
        with suppress(Exception):
            self._bus.close()


# ---------------------------------------------------------------------------
# Simulated IMU for testing / development
# ---------------------------------------------------------------------------
class SimulatedIMU:
    """Generates realistic dog walking IMU data with optional limping."""

    def __init__(self, sample_rate_hz: int = 50, seed: int = 42) -> None:
        import random
        self._rng = random.Random(seed)
        self._sample_rate_hz = sample_rate_hz
        self._dt = 1.0 / sample_rate_hz
        self._t0 = time.monotonic()
        self._limping = False
        self._limp_side: str = "left"
        self._limp_factor = 1.0

    def set_limping(self, enabled: bool, side: str = "left", factor: float = 1.0) -> None:
        self._limping = enabled
        self._limp_side = side
        self._limp_factor = factor

    def read(self) -> IMUSample:
        now = time.monotonic()
        t = now - self._t0

        # Base walking cadence ~1.5 Hz (two steps per cycle, left/right)
        cadence_hz = 1.5
        phase = 2 * math.pi * cadence_hz * t

        # Even/odd steps: left at phase ~0, right at phase ~pi
        left_step = math.exp(-((phase % (2 * math.pi)) ** 2) / 0.3)
        right_step = math.exp(-(((phase + math.pi) % (2 * math.pi)) ** 2) / 0.3)

        # Vertical acceleration (Z) spikes on each step.  When limping the
        # affected side generates a smaller vertical impulse, mimicking
        # reduced weight bearing.
        if self._limping:
            if self._limp_side == "left":
                left_step *= (1.0 - 0.45 * self._limp_factor)
                right_step *= (1.0 + 0.10 * self._limp_factor)
            else:
                right_step *= (1.0 - 0.45 * self._limp_factor)
                left_step *= (1.0 + 0.10 * self._limp_factor)

        vertical = G + 2.5 * (left_step + right_step) + self._rng.gauss(0, 0.3)

        # Lateral acceleration (X): push-off creates side-to-side sway.
        # When limping, exaggerate the imbalance so analysis can detect it.
        lateral_scale = 1.2
        if self._limping:
            lateral_scale = 2.5 if self._limp_side == "left" else -2.5
        lateral = lateral_scale * (right_step - left_step) + self._rng.gauss(0, 0.2)

        # Forward acceleration (Y): periodic propulsion, shorter on affected side
        forward_scale = 0.6
        if self._limping:
            forward_scale = 0.35 if self._limp_side == "left" else 0.75
        forward = forward_scale * math.sin(phase) + self._rng.gauss(0, 0.15)

        accel = (lateral, forward, vertical)
        gyro = (
            self._rng.gauss(0, 2.0),
            self._rng.gauss(0, 2.0),
            5.0 * math.sin(phase) + self._rng.gauss(0, 1.0),
        )
        quat = (1.0, 0.0, 0.0, 0.0)

        return IMUSample(timestamp=now, accel=accel, gyro=gyro, quat=quat)


# ---------------------------------------------------------------------------
# SQLite persistence
# ---------------------------------------------------------------------------
class GaitDatabase:
    """SQLite-backed storage for gait samples, steps, summaries, and baselines."""

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._init_schema()

    def _conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn") or self._local.conn is None:
            self._local.conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
            self._local.conn.row_factory = sqlite3.Row
        return self._local.conn

    def _init_schema(self) -> None:
        conn = self._conn()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS steps (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                timestamp_utc TEXT NOT NULL,
                stride_length_m REAL NOT NULL,
                lateral_asymmetry REAL NOT NULL,
                impact_accel_g REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_steps_date ON steps(timestamp_utc);

            CREATE TABLE IF NOT EXISTS daily_summary (
                date TEXT PRIMARY KEY,
                step_count INTEGER NOT NULL,
                avg_stride_m REAL NOT NULL,
                std_stride_m REAL NOT NULL,
                cadence REAL NOT NULL,
                cv_interval REAL NOT NULL,
                symmetry_score REAL NOT NULL,
                left_dominance REAL NOT NULL,
                right_dominance REAL NOT NULL,
                limping_flags TEXT NOT NULL,
                insights TEXT NOT NULL,
                updated_utc TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS baseline (
                metric TEXT PRIMARY KEY,
                value REAL NOT NULL,
                samples INTEGER NOT NULL,
                updated_utc TEXT NOT NULL
            );
        """)
        conn.commit()

    def insert_step(self, step: Step) -> None:
        conn = self._conn()
        conn.execute(
            """INSERT INTO steps (timestamp, timestamp_utc, stride_length_m,
                                  lateral_asymmetry, impact_accel_g)
               VALUES (?, ?, ?, ?, ?)""",
            (step.timestamp, step.timestamp_utc.isoformat(), step.stride_length_m,
             step.lateral_asymmetry, step.impact_accel_g),
        )
        conn.commit()

    def get_steps_for_date(self, date_str: str) -> List[Dict[str, Any]]:
        conn = self._conn()
        # timestamp_utc stored as ISO 8601 with timezone offset.  Strip the
        # timezone suffix and use SQLite's date() on the normalized string.
        rows = conn.execute(
            "SELECT * FROM steps WHERE DATE(SUBSTR(timestamp_utc, 1, 10)) = ? ORDER BY timestamp",
            (date_str,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_steps_between(self, start: str, end: str) -> List[Dict[str, Any]]:
        conn = self._conn()
        rows = conn.execute(
            "SELECT * FROM steps WHERE DATE(SUBSTR(timestamp_utc, 1, 10)) BETWEEN ? AND ? ORDER BY timestamp",
            (start, end),
        ).fetchall()
        return [dict(r) for r in rows]

    def save_daily_summary(self, summary: Dict[str, Any]) -> None:
        conn = self._conn()
        conn.execute(
            """INSERT INTO daily_summary (date, step_count, avg_stride_m, std_stride_m,
                                          cadence, cv_interval, symmetry_score,
                                          left_dominance, right_dominance, limping_flags,
                                          insights, updated_utc)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(date) DO UPDATE SET
                 step_count=excluded.step_count,
                 avg_stride_m=excluded.avg_stride_m,
                 std_stride_m=excluded.std_stride_m,
                 cadence=excluded.cadence,
                 cv_interval=excluded.cv_interval,
                 symmetry_score=excluded.symmetry_score,
                 left_dominance=excluded.left_dominance,
                 right_dominance=excluded.right_dominance,
                 limping_flags=excluded.limping_flags,
                 insights=excluded.insights,
                 updated_utc=excluded.updated_utc""",
            (summary["date"], summary["step_count"], summary["avg_stride_m"],
             summary["std_stride_m"], summary["cadence"], summary["cv_interval"],
             summary["symmetry_score"], summary["left_dominance"], summary["right_dominance"],
             json.dumps(summary["limping_flags"]), json.dumps(summary["insights"]),
             summary["updated_utc"]),
        )
        conn.commit()

    def get_daily_summary(self, date_str: str) -> Optional[Dict[str, Any]]:
        conn = self._conn()
        row = conn.execute("SELECT * FROM daily_summary WHERE date = ?", (date_str,)).fetchone()
        if not row:
            return None
        data = dict(row)
        data["limping_flags"] = json.loads(data["limping_flags"])
        data["insights"] = json.loads(data["insights"])
        return data

    def get_history(self, days: int = 7) -> List[Dict[str, Any]]:
        conn = self._conn()
        since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
        rows = conn.execute(
            "SELECT * FROM daily_summary WHERE date >= ? ORDER BY date DESC",
            (since,),
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["limping_flags"] = json.loads(d["limping_flags"])
            d["insights"] = json.loads(d["insights"])
            out.append(d)
        return out

    def get_baseline(self, metric: str) -> Optional[float]:
        conn = self._conn()
        row = conn.execute("SELECT value FROM baseline WHERE metric = ?", (metric,)).fetchone()
        return row["value"] if row else None

    def update_baseline(self, metric: str, value: float, samples: int) -> None:
        conn = self._conn()
        conn.execute(
            """INSERT INTO baseline (metric, value, samples, updated_utc)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(metric) DO UPDATE SET
                 value=excluded.value,
                 samples=excluded.samples,
                 updated_utc=excluded.updated_utc""",
            (metric, value, samples, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()

    def close(self) -> None:
        with suppress(Exception):
            if hasattr(self._local, "conn") and self._local.conn:
                self._local.conn.close()
                self._local.conn = None


# ---------------------------------------------------------------------------
# Gait analysis engine
# ---------------------------------------------------------------------------
class GaitAnalyzer:
    """Core step detection, symmetry, and limping analysis."""

    def __init__(self, config: GaitAnalyzerConfig, db: GaitDatabase):
        self.config = config
        self.db = db
        self.state = GaitState()
        self._state_lock = threading.Lock()

        self._buffer: Deque[IMUSample] = deque(maxlen=config.sample_rate_hz * 10)
        self._last_step_time: Optional[float] = None
        self._step_candidates: List[Tuple[float, float]] = []  # (time, vertical_accel)

        self._steps_today: List[Step] = []
        self._current_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

    def _zero_drift(self, values: List[float]) -> List[float]:
        """Remove low-frequency drift via a simple high-pass filter."""
        if not values:
            return values
        alpha = 2 * math.pi * self.config.drift_correction_hz / self.config.sample_rate_hz
        out = []
        baseline = sum(values[:min(10, len(values))]) / min(10, len(values))
        for v in values:
            baseline += alpha * (v - baseline)
            out.append(v - baseline)
        return out

    def _estimate_stride(self, step_time: float) -> float:
        """Estimate stride length by double-integrating forward acceleration."""
        window = []
        times = []
        for s in reversed(self._buffer):
            if s.timestamp < step_time - self.config.stride_integration_window_ms / 1000.0:
                break
            window.append(s)
            times.append(s.timestamp)
        if len(window) < 10:
            return 0.5  # fallback default

        window.reverse()
        forward = [s.accel[1] for s in window]
        forward = self._zero_drift(forward)

        # First integration -> velocity
        velocity = [0.0]
        dt = 1.0 / self.config.sample_rate_hz
        for a in forward:
            velocity.append(velocity[-1] + a * dt)
        velocity = self._zero_drift(velocity[1:])

        # Second integration -> displacement
        displacement = 0.0
        for v in velocity:
            displacement += v * dt

        # Stride length is twice displacement for a single step (one foot to same foot)
        stride = max(0.15, abs(displacement) * 2.0)
        # Clamp to plausible dog stride range
        return min(stride, 2.5)

    def _detect_step(self, sample: IMUSample) -> Optional[Step]:
        """Peak-detection based step detection from vertical acceleration."""
        now = sample.timestamp
        vertical_over_gravity = sample.accel_magnitude / G

        # Track local maxima above threshold
        if vertical_over_gravity >= self.config.step_threshold_g:
            self._step_candidates.append((now, vertical_over_gravity))
        else:
            if self._step_candidates:
                peak_time, peak_accel = max(self._step_candidates, key=lambda x: x[1])

                # Enforce minimum step interval
                if self._last_step_time is None or \
                   (peak_time - self._last_step_time) * 1000.0 >= self.config.step_min_interval_ms:

                    stride = self._estimate_stride(peak_time)

                    # Asymmetry: lateral acceleration integral since last step.
                    # This isolates the side-to-side loading for the current step.
                    start_t = self._last_step_time if self._last_step_time else peak_time - 0.5
                    step_samples = [s for s in self._buffer if start_t <= s.timestamp <= peak_time]
                    lateral_sum = sum(s.accel[0] for s in step_samples)
                    lateral_asymmetry = lateral_sum / (len(step_samples) * G) if step_samples else 0.0

                    step = Step(
                        timestamp=peak_time,
                        timestamp_utc=datetime.now(timezone.utc),
                        stride_length_m=stride,
                        lateral_asymmetry=lateral_asymmetry,
                        impact_accel_g=peak_accel,
                    )
                    self._last_step_time = peak_time
                    self._step_candidates = []
                    return step

                self._step_candidates = []

        # Prune old candidates
        cutoff = now - self.config.step_window_ms / 1000.0
        self._step_candidates = [(t, a) for t, a in self._step_candidates if t > cutoff]
        return None

    def _roll_date_if_needed(self) -> None:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if today != self._current_date:
            self._summarize_and_persist(self._current_date)
            self._steps_today = []
            self._current_date = today

    def _summarize_and_persist(self, date_str: str) -> Dict[str, Any]:
        """Compute and store daily summary for date_str."""
        steps = self.db.get_steps_for_date(date_str)
        if not steps:
            steps = [s.__dict__ for s in self._steps_today if s.timestamp_utc.strftime("%Y-%m-%d") == date_str]

        count = len(steps)
        if count == 0:
            summary = {
                "date": date_str,
                "step_count": 0,
                "avg_stride_m": 0.0,
                "std_stride_m": 0.0,
                "cadence": 0.0,
                "cv_interval": 0.0,
                "symmetry_score": 1.0,
                "left_dominance": 0.0,
                "right_dominance": 0.0,
                "limping_flags": [],
                "insights": ["No walking data recorded today"],
                "updated_utc": datetime.now(timezone.utc).isoformat(),
            }
            self.db.save_daily_summary(summary)
            return summary

        strides = [s["stride_length_m"] for s in steps]
        avg_stride = sum(strides) / count
        std_stride = math.sqrt(sum((x - avg_stride) ** 2 for x in strides) / count) if count > 1 else 0.0

        # Step interval CV
        times = [s["timestamp"] for s in steps]
        intervals = [(times[i] - times[i - 1]) for i in range(1, count)]
        mean_interval = sum(intervals) / len(intervals) if intervals else 0.0
        cv = (math.sqrt(sum((x - mean_interval) ** 2 for x in intervals) / len(intervals)) / mean_interval
              if intervals and mean_interval > 0 else 0.0)

        # Cadence: steps per minute over active walking periods
        total_walking_sec = sum(intervals) if intervals else 0.0
        cadence = (count / (total_walking_sec / 60.0)) if total_walking_sec > 0 else 0.0

        # Symmetry score based on lateral asymmetry distribution
        asym = [s["lateral_asymmetry"] for s in steps]
        left_dom = sum(a for a in asym if a < 0) / (sum(1 for a in asym if a < 0) or 1)
        right_dom = sum(a for a in asym if a > 0) / (sum(1 for a in asym if a > 0) or 1)
        total_dom = abs(left_dom) + right_dom
        symmetry_score = max(0.0, 1.0 - total_dom) if total_dom < 1.0 else 0.0

        limping_flags = []
        if cv > self.config.cv_threshold:
            limping_flags.append("irregular_rhythm")
        if total_dom > self.config.asymmetry_threshold:
            side = "left" if abs(left_dom) > right_dom else "right"
            limping_flags.append(f"asymmetric_loading_{side}")

        # Compare to baseline
        baseline_stride = self.db.get_baseline("stride_length_m")
        if baseline_stride and baseline_stride > 0:
            if avg_stride < baseline_stride * (1.0 - self.config.stride_drop_threshold):
                limping_flags.append("reduced_stride_length")

        insights = self._generate_insights(
            date_str, avg_stride, baseline_stride, symmetry_score, cv, limping_flags
        )

        summary = {
            "date": date_str,
            "step_count": count,
            "avg_stride_m": round(avg_stride, 3),
            "std_stride_m": round(std_stride, 3),
            "cadence": round(cadence, 1),
            "cv_interval": round(cv, 3),
            "symmetry_score": round(symmetry_score, 3),
            "left_dominance": round(left_dom, 4),
            "right_dominance": round(right_dom, 4),
            "limping_flags": limping_flags,
            "insights": insights,
            "updated_utc": datetime.now(timezone.utc).isoformat(),
        }
        self.db.save_daily_summary(summary)
        return summary

    def _generate_insights(self, date_str: str, avg_stride: float,
                           baseline_stride: Optional[float],
                           symmetry_score: float, cv: float,
                           limping_flags: List[str]) -> List[str]:
        insights: List[str] = []

        if limping_flags:
            if "asymmetric_loading_left" in limping_flags:
                insights.append("Possible lameness detected in left hind")
            elif "asymmetric_loading_right" in limping_flags:
                insights.append("Possible lameness detected in right hind")
            if "irregular_rhythm" in limping_flags:
                insights.append("Irregular foot placement rhythm detected")
            if "reduced_stride_length" in limping_flags:
                insights.append("Reduced stride length compared to baseline")

        # Trend vs prior days
        history = self.db.get_history(days=self.config.baseline_days)
        history = [h for h in history if h["date"] != date_str and h["step_count"] > 0]
        if history:
            prev_sym = sum(h["symmetry_score"] for h in history) / len(history)
            prev_stride = sum(h["avg_stride_m"] for h in history) / len(history)
            if prev_sym > 0 and symmetry_score > 0:
                sym_change = (prev_sym - symmetry_score) / prev_sym
                if sym_change > self.config.baseline_change_threshold:
                    insights.append(
                        f"Gait symmetry declined {int(sym_change * 100)}% over {len(history)} days"
                    )
                elif sym_change < -self.config.baseline_change_threshold:
                    insights.append("Walking pattern returning to baseline")
            if prev_stride > 0 and baseline_stride is None:
                stride_change = (prev_stride - avg_stride) / prev_stride
                if stride_change > self.config.baseline_change_threshold:
                    insights.append(f"Average stride length dropped {int(stride_change * 100)}% recently")

        if not insights:
            if cv < self.config.cv_threshold and symmetry_score > 0.85:
                insights.append("Gait looks normal and regular today")
            else:
                insights.append("Gait within acceptable variation")

        return insights

    def _update_baseline(self) -> None:
        """Recompute multi-day baselines from history."""
        history = self.db.get_history(days=self.config.baseline_days)
        history = [h for h in history if h["step_count"] > 0]
        if len(history) >= 2:
            avg_stride = sum(h["avg_stride_m"] for h in history) / len(history)
            avg_sym = sum(h["symmetry_score"] for h in history) / len(history)
            avg_cv = sum(h["cv_interval"] for h in history) / len(history)
            self.db.update_baseline("stride_length_m", avg_stride, len(history))
            self.db.update_baseline("symmetry_score", avg_sym, len(history))
            self.db.update_baseline("cv_interval", avg_cv, len(history))

    def _process_sample(self, sample: IMUSample) -> None:
        self._buffer.append(sample)

        step = self._detect_step(sample)
        if step:
            self._roll_date_if_needed()
            self._steps_today.append(step)
            self.db.insert_step(step)

            with self._state_lock:
                self.state.step_count_session += 1
                self.state.last_step_time = step.timestamp
                self._recompute_state()

    def _recompute_state(self) -> None:
        """Recompute real-time gait state from recent steps."""
        recent = list(self._buffer)
        if not recent:
            return

        # Activity classification
        accel_mag_mean = sum(s.accel_magnitude for s in recent) / len(recent)
        self.state.moving = accel_mag_mean > G * 1.05

        if len(self._steps_today) >= 2:
            times = [s.timestamp for s in self._steps_today[-20:]]
            intervals = [(times[i] - times[i - 1]) for i in range(1, len(times))]
            mean_interval = sum(intervals) / len(intervals) if intervals else 1.0
            self.state.cadence_steps_per_min = 60.0 / mean_interval if mean_interval > 0 else 0.0

            strides = [s.stride_length_m for s in self._steps_today[-20:]]
            self.state.avg_stride_m = sum(strides) / len(strides)

            asym = [s.lateral_asymmetry for s in self._steps_today[-20:]]
            left = sum(a for a in asym if a < 0)
            right = sum(a for a in asym if a > 0)
            total = abs(left) + right
            self.state.current_symmetry_score = max(0.0, 1.0 - total) if total < 1.0 else 0.0

            self.state.current_cv = (
                math.sqrt(sum((x - mean_interval) ** 2 for x in intervals) / len(intervals)) / mean_interval
                if intervals and mean_interval > 0 else 0.0
            )

        # Limping detection in real-time
        reasons = []
        if self.state.current_cv > self.config.cv_threshold:
            reasons.append("irregular_rhythm")
        if self.state.current_symmetry_score < (1.0 - self.config.asymmetry_threshold):
            reasons.append("asymmetric_loading")
        baseline_stride = self.db.get_baseline("stride_length_m")
        if baseline_stride and self.state.avg_stride_m > 0 and \
           self.state.avg_stride_m < baseline_stride * (1.0 - self.config.stride_drop_threshold):
            reasons.append("reduced_stride_length")

        self.state.limping_detected = len(reasons) > 0
        self.state.limping_reasons = reasons

    def get_status(self) -> Dict[str, Any]:
        with self._state_lock:
            state = self.state
            return {
                "enabled": self.config.enabled,
                "moving": state.moving,
                "step_count_session": state.step_count_session,
                "last_step_time": state.last_step_time,
                "cadence_steps_per_min": round(state.cadence_steps_per_min, 1),
                "avg_stride_m": round(state.avg_stride_m, 3),
                "symmetry_score": round(state.current_symmetry_score, 3),
                "cv_interval": round(state.current_cv, 3),
                "limping_detected": state.limping_detected,
                "limping_reasons": state.limping_reasons,
                "insights": state.insights,
                "current_date": self._current_date,
            }

    def get_today_summary(self) -> Dict[str, Any]:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        summary = self.db.get_daily_summary(today)
        if summary is None:
            # Build ephemeral summary from in-memory steps
            summary = self._summarize_and_persist(today)
        return summary

    def get_history(self, days: int = 7) -> List[Dict[str, Any]]:
        return self.db.get_history(days=days)

    def start(self) -> None:
        if not self.config.enabled:
            logger.info("Gait analyzer disabled in config")
            return
        self._running = True
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._analysis_loop, daemon=True)
        self._thread.start()
        logger.info("Gait analyzer started")

    def stop(self) -> None:
        self._running = False
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=2.0)
        self._summarize_and_persist(self._current_date)
        logger.info("Gait analyzer stopped")

    def _analysis_loop(self) -> None:
        """Background loop: recompute baselines hourly and roll date."""
        last_baseline_update = 0.0
        while not self._stop_event.is_set():
            self._roll_date_if_needed()
            now = time.monotonic()
            if now - last_baseline_update > 3600:
                self._update_baseline()
                last_baseline_update = now
                # Refresh insights for today
                today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                self._summarize_and_persist(today)
            time.sleep(5.0)


# ---------------------------------------------------------------------------
# Sensor manager (hardware or simulated)
# ---------------------------------------------------------------------------
class GaitSensorManager:
    """Feeds IMU samples into the analyzer from hardware or simulation."""

    def __init__(self, analyzer: GaitAnalyzer, config: GaitAnalyzerConfig, simulate: bool = False):
        self.analyzer = analyzer
        self.config = config
        self.simulate = simulate
        self.imu: Any = None
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

    def start(self) -> None:
        if self.simulate:
            self.imu = SimulatedIMU(self.config.sample_rate_hz)
            logger.info("Gait sensor manager using simulated IMU")
        else:
            try:
                self.imu = BNO055Driver(self.config.i2c_bus, self.config.bno055_addr)
                logger.info("Gait sensor manager using BNO055 hardware")
            except Exception as e:
                logger.error("Failed to initialize BNO055: %s", e)
                logger.info("Falling back to simulated IMU")
                self.imu = SimulatedIMU(self.config.sample_rate_hz)
                self.simulate = True

        self._running = True
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._read_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=2.0)
        if hasattr(self.imu, "close"):
            self.imu.close()

    def _read_loop(self) -> None:
        interval = 1.0 / self.config.sample_rate_hz
        while not self._stop_event.is_set():
            try:
                sample = self.imu.read()
                if sample:
                    self.analyzer._process_sample(sample)
                time.sleep(interval)
            except Exception as e:
                logger.error("Sensor read error: %s", e)
                time.sleep(0.1)


# ---------------------------------------------------------------------------
# HTTP API handler
# ---------------------------------------------------------------------------
class GaitHandler(BaseHTTPRequestHandler):
    analyzer: Optional[GaitAnalyzer] = None

    def log_message(self, format, *args):
        logger.debug("HTTP: %s", format % args)

    def _send_json(self, data, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data, indent=2, default=str).encode())

    def _send_error(self, status, message):
        self._send_json({"error": message}, status)

    def do_GET(self):
        if self.analyzer is None:
            self._send_error(503, "Gait analyzer not initialized")
            return

        path = self.path.split("?", 1)[0]

        if path == "/gait/status":
            self._send_json(self.analyzer.get_status())
        elif path == "/gait/today":
            self._send_json(self.analyzer.get_today_summary())
        elif path == "/gait/history":
            days = 7
            raw_path = self.path
            if "?" in raw_path:
                query = raw_path.split("?", 1)[1]
                for param in query.split("&"):
                    if "=" in param:
                        k, v = param.split("=", 1)
                        if k == "days":
                            try:
                                days = int(v)
                            except ValueError:
                                pass
            self._send_json({
                "days": days,
                "history": self.analyzer.get_history(days=days),
            })
        elif path == "/gait/health":
            self._send_json({
                "status": "healthy",
                "enabled": self.analyzer.config.enabled,
                "sample_rate_hz": self.analyzer.config.sample_rate_hz,
                "baseline_days": self.analyzer.config.baseline_days,
                "db_path": str(self.analyzer.config.db_path),
                "simulate": getattr(self.analyzer, "_simulate", False),
            })
        else:
            self._send_error(404, "Not found")


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------
class GaitAnalyzerServer:
    def __init__(self, analyzer: GaitAnalyzer, port: int = DEFAULT_PORT):
        self.analyzer = analyzer
        self.port = port
        self.server: Optional[HTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

    def start(self) -> None:
        GaitHandler.analyzer = self.analyzer
        self.server = HTTPServer(("0.0.0.0", self.port), GaitHandler)
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()
        logger.info("Gait analyzer HTTP server started on port %d", self.port)

    def _serve(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.server.handle_request()
            except Exception as e:
                if not self._stop_event.is_set():
                    logger.error("Server error: %s", e)

    def stop(self) -> None:
        self._stop_event.set()
        if self.server:
            self.server.shutdown()
        if self._thread:
            self._thread.join(timeout=2.0)
        logger.info("Gait analyzer HTTP server stopped")


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Dog Agent Gait Analyzer Module")
    parser.add_argument("--config", type=str, default=str(DEFAULT_CONFIG_PATH),
                        help="Path to config.yaml")
    parser.add_argument("--simulate", action="store_true",
                        help="Run in simulation mode")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help="HTTP API port")
    parser.add_argument("--limp", action="store_true",
                        help="Inject simulated limping in simulation mode")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )

    config_dict = load_config(args.config)
    config = GaitAnalyzerConfig.from_dict(config_dict)
    if args.port != DEFAULT_PORT:
        config.api_port = args.port

    db = GaitDatabase(config.db_path)
    analyzer = GaitAnalyzer(config, db)
    analyzer._simulate = args.simulate  # type: ignore

    sensor_manager = GaitSensorManager(analyzer, config, simulate=args.simulate)
    if args.simulate and args.limp:
        if isinstance(sensor_manager.imu, SimulatedIMU):
            sensor_manager.imu.set_limping(True, side="left", factor=1.0)
            logger.info("Simulated limping enabled")

    analyzer.start()
    sensor_manager.start()

    server = GaitAnalyzerServer(analyzer, port=config.api_port)
    server.start()

    def shutdown(signum, frame):
        logger.info("Shutting down gait analyzer...")
        server.stop()
        sensor_manager.stop()
        analyzer.stop()
        db.close()
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    logger.info("Gait analyzer running. Press Ctrl+C to stop.")
    while True:
        time.sleep(1)


if __name__ == "__main__":
    main()
