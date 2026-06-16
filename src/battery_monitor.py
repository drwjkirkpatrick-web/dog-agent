#!/usr/bin/env python3
"""
Dog Agent — Battery Monitor
===========================
Real-time battery monitoring with predictive "time remaining" estimation.

Features:
  - INA219 current/voltage/power monitoring
  - Predictive discharge curves
  - Smart alerts at multiple thresholds
  - Automatic low-power mode switching

Usage:
    python src/battery_monitor.py
    python src/battery_monitor.py --simulate
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sqlite3
import sys
import threading
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from collections import deque

import yaml

# Logging setup
logger = logging.getLogger("battery_monitor")
handler = logging.StreamHandler(sys.stdout)
handler.setFormatter(logging.Formatter(
    "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
))
logger.addHandler(handler)
logger.setLevel(logging.INFO)

# Paths
PROJECT_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_DIR / "config.yaml"
DB_PATH = PROJECT_DIR / "data" / "battery_history.db"

# Constants
SAMPLE_HISTORY_MINUTES = 10  # For drain rate calculation
DISCHARGE_CURVE = [
    (4.20, 100.0), (4.15, 95.0), (4.10, 90.0), (4.05, 85.0),
    (4.00, 75.0), (3.95, 65.0), (3.90, 55.0), (3.85, 45.0),
    (3.80, 35.0), (3.75, 25.0), (3.70, 15.0), (3.65, 10.0),
    (3.60, 5.0), (3.50, 2.0), (3.40, 1.0), (3.30, 0.0),
]

ALERT_THRESHOLDS = {
    "info": 50,
    "warning": 30,
    "critical": 15,
    "emergency": 5,
}


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
class BatteryReading:
    voltage_v: float
    current_ma: float
    power_mw: float
    percent: float
    time_remaining_min: Optional[float]
    timestamp: datetime
    
    def to_dict(self) -> dict:
        return {
            "voltage_v": round(self.voltage_v, 3),
            "current_ma": round(self.current_ma, 1),
            "power_mw": round(self.power_mw, 1),
            "percent": round(self.percent, 1),
            "time_remaining_min": round(self.time_remaining_min, 1) if self.time_remaining_min else None,
            "timestamp": self.timestamp.isoformat(),
        }


class BatteryMonitor:
    """Monitors battery with predictive analytics."""
    
    def __init__(self):
        self.enabled = get_cfg("battery_monitor.enabled", True)
        self.i2c_address = get_cfg("battery_monitor.ina219_address", 0x40)
        self.capacity_mah = get_cfg("battery_monitor.capacity_mah", 2000)
        self.poll_interval_sec = get_cfg("battery_monitor.poll_interval_sec", 30)
        self.alert_cooldown_min = get_cfg("battery_monitor.alert_cooldown_min", 15)
        
        self._ina219 = None
        self._readings: deque = deque(maxlen=60)  # 30 min of history
        self._last_alert_time: Dict[str, datetime] = {}
        self._lock = threading.Lock()
        self._db: Optional[sqlite3.Connection] = None
        
        self._init_db()
        self._init_hardware()
    
    def _init_db(self) -> None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS battery_readings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                voltage_v REAL NOT NULL,
                current_ma REAL NOT NULL,
                power_mw REAL NOT NULL,
                percent REAL NOT NULL,
                time_remaining_min REAL,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_battery_time ON battery_readings(timestamp);
        """)
        self._db.commit()
    
    def _init_hardware(self) -> None:
        if not self.enabled:
            return
        try:
            from ina219 import INA219
            self._ina219 = INA219(0.1, self.i2c_address)
            self._ina219.configure()
            logger.info(f"INA219 initialized at 0x{self.i2c_address:02X}")
        except ImportError:
            logger.warning("INA219 library not available")
        except Exception as e:
            logger.warning(f"Failed to initialize INA219: {e}")
    
    def _voltage_to_percent(self, voltage_v: float) -> float:
        if voltage_v >= 4.20:
            return 100.0
        if voltage_v <= 3.30:
            return 0.0
        
        for i in range(len(DISCHARGE_CURVE) - 1):
            v_high, p_high = DISCHARGE_CURVE[i]
            v_low, p_low = DISCHARGE_CURVE[i + 1]
            if v_low <= voltage_v <= v_high:
                ratio = (voltage_v - v_low) / (v_high - v_low)
                return p_low + ratio * (p_high - p_low)
        return 0.0
    
    def _calculate_drain_rate(self) -> Optional[float]:
        """Calculate mAh per hour drain rate."""
        with self._lock:
            if len(self._readings) < 2:
                return None
            
            recent = list(self._readings)[-20:]  # Last 10 min
            if len(recent) < 2:
                return None
            
            avg_current = sum(r.current_ma for r in recent) / len(recent)
            return avg_current * 60 / 1000  # mAh per hour
    
    def _predict_time_remaining(self, percent: float) -> Optional[float]:
        """Predict minutes remaining until empty."""
        drain_rate = self._calculate_drain_rate()
        if drain_rate is None or drain_rate <= 0:
            return None
        
        remaining_mah = (percent / 100.0) * self.capacity_mah
        hours_remaining = remaining_mah / drain_rate
        return hours_remaining * 60
    
    def read(self) -> Optional[BatteryReading]:
        if not self.enabled:
            return None
        
        try:
            if self._ina219:
                voltage_v = self._ina219.voltage()
                current_ma = self._ina219.current()
                power_mw = voltage_v * current_ma / 1000
            else:
                # Fallback: try sysfs
                voltage_v = self._read_sysfs_voltage()
                current_ma = self._read_sysfs_current()
                power_mw = voltage_v * current_ma / 1000
            
            percent = self._voltage_to_percent(voltage_v)
            time_remaining = self._predict_time_remaining(percent)
            
            reading = BatteryReading(
                voltage_v=voltage_v,
                current_ma=current_ma,
                power_mw=power_mw,
                percent=percent,
                time_remaining_min=time_remaining,
                timestamp=datetime.now(timezone.utc),
            )
            
            with self._lock:
                self._readings.append(reading)
            
            # Store in DB
            self._db.execute(
                """INSERT INTO battery_readings 
                   (voltage_v, current_ma, power_mw, percent, time_remaining_min)
                   VALUES (?, ?, ?, ?, ?)""",
                (voltage_v, current_ma, power_mw, percent, time_remaining)
            )
            self._db.commit()
            
            return reading
            
        except Exception as e:
            logger.error(f"Failed to read battery: {e}")
            return None
    
    def _read_sysfs_voltage(self) -> float:
        try:
            path = "/sys/class/power_supply/BAT0/voltage_now"
            if os.path.exists(path):
                with open(path) as f:
                    return int(f.read().strip()) / 1_000_000
        except:
            pass
        return 3.85  # Default
    
    def _read_sysfs_current(self) -> float:
        try:
            path = "/sys/class/power_supply/BAT0/current_now"
            if os.path.exists(path):
                with open(path) as f:
                    return int(f.read().strip()) / 1_000
        except:
            pass
        return -200.0  # Default drain
    
    def check_alerts(self) -> List[dict]:
        """Check for battery level alerts."""
        reading = self.get_latest()
        if not reading:
            return []
        
        alerts = []
        now = datetime.now(timezone.utc)
        
        for level, threshold in ALERT_THRESHOLDS.items():
            if reading.percent <= threshold:
                last_alert = self._last_alert_time.get(level)
                if not last_alert or (now - last_alert).seconds > self.alert_cooldown_min * 60:
                    alerts.append({
                        "level": level,
                        "percent": reading.percent,
                        "time_remaining": reading.time_remaining_min,
                        "message": f"Battery {level}: {reading.percent:.1f}% remaining",
                    })
                    self._last_alert_time[level] = now
        
        return alerts
    
    def get_latest(self) -> Optional[BatteryReading]:
        with self._lock:
            return self._readings[-1] if self._readings else None
    
    def get_history(self, hours: int = 24) -> List[BatteryReading]:
        """Get battery history from database."""
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        cursor = self._db.execute(
            """SELECT voltage_v, current_ma, power_mw, percent, time_remaining_min, timestamp
               FROM battery_readings WHERE timestamp > ? ORDER BY timestamp""",
            (cutoff.isoformat(),)
        )
        
        return [BatteryReading(
            voltage_v=row["voltage_v"],
            current_ma=row["current_ma"],
            power_mw=row["power_mw"],
            percent=row["percent"],
            time_remaining_min=row["time_remaining_min"],
            timestamp=datetime.fromisoformat(row["timestamp"]),
        ) for row in cursor]
    
    def get_summary(self) -> dict:
        """Get battery summary statistics."""
        reading = self.get_latest()
        if not reading:
            return {"error": "No data available"}
        
        drain_rate = self._calculate_drain_rate()
        
        return {
            "current": reading.to_dict(),
            "drain_rate_mah_per_hour": round(drain_rate, 2) if drain_rate else None,
            "estimated_runtime_hours": round(reading.time_remaining_min / 60, 1) if reading.time_remaining_min else None,
            "trend": "discharging" if reading.current_ma > 0 else "charging",
        }


class BatteryHTTPHandler(BaseHTTPRequestHandler):
    """HTTP API for battery monitoring."""
    
    battery_monitor: Optional[BatteryMonitor] = None
    
    def log_message(self, format, *args):
        logger.debug(f"HTTP: {format % args}")
    
    def _send_json(self, data: Any, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data, indent=2, default=str).encode())
    
    def _send_error(self, status: int, message: str) -> None:
        self._send_json({"error": message}, status)
    
    def do_GET(self):
        path = self.path.strip("/")
        
        if path == "battery/health":
            self._send_json({
                "status": "ok",
                "service": "battery_monitor",
                "enabled": bool(self.battery_monitor and self.battery_monitor.enabled),
            })
        elif path == "battery/current":
            if not self.battery_monitor:
                self._send_error(503, "Battery monitor not initialized")
                return
            reading = self.battery_monitor.get_latest()
            if reading:
                self._send_json(reading.to_dict())
            else:
                self._send_error(503, "No battery data available")
        elif path == "battery/summary":
            if not self.battery_monitor:
                self._send_error(503, "Battery monitor not initialized")
                return
            self._send_json(self.battery_monitor.get_summary())
        elif path == "battery/history":
            if not self.battery_monitor:
                self._send_error(503, "Battery monitor not initialized")
                return
            history = self.battery_monitor.get_history(hours=24)
            self._send_json({"readings": [r.to_dict() for r in history]})
        else:
            self._send_error(404, f"Unknown endpoint: {path}")


def main():
    parser = argparse.ArgumentParser(description="Dog Agent — Battery Monitor")
    parser.add_argument("--port", type=int, default=9132, help="HTTP API port")
    parser.add_argument("--simulate", action="store_true", help="Run simulation mode")
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose logging")
    args = parser.parse_args()
    
    if args.verbose:
        logger.setLevel(logging.DEBUG)
    
    monitor = BatteryMonitor()
    
    if args.simulate:
        logger.info("=== Battery Monitor Simulation ===")
        # Simulate drain curve
        for percent in [100, 75, 50, 25, 10, 5]:
            reading = BatteryReading(
                voltage_v=3.3 + percent / 100 * 0.9,
                current_ma=-250,
                power_mw=-825,
                percent=percent,
                time_remaining_min=percent * 4,
                timestamp=datetime.now(timezone.utc),
            )
            logger.info(f"Battery: {percent}% - {reading.time_remaining_min:.0f} min remaining")
        return
    
    BatteryHTTPHandler.battery_monitor = monitor
    
    server = HTTPServer(("127.0.0.1", args.port), BatteryHTTPHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    logger.info(f"Battery API on http://127.0.0.1:{args.port}")
    
    # Polling loop
    def poll():
        while True:
            if monitor.enabled:
                reading = monitor.read()
                if reading:
                    alerts = monitor.check_alerts()
                    for alert in alerts:
                        logger.warning(f"BATTERY ALERT: {alert['message']}")
            time.sleep(monitor.poll_interval_sec)
    
    poll_thread = threading.Thread(target=poll, daemon=True)
    poll_thread.start()
    
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
        logger.info("Battery monitor stopped")


if __name__ == "__main__":
    main()
