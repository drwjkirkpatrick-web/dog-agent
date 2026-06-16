#!/usr/bin/env python3
"""
Dog Agent — Solar Charging Monitor
==================================
Monitors solar panel output and LiPo battery charging status.
Provides charging recommendations and alerts for power management.

Features:
  - INA219 current/voltage monitoring
  - Solar panel efficiency tracking
  - Battery charge state estimation
  - Charging recommendations
  - Low-light performance alerts

Usage:
    python src/solar_monitor.py
    python src/solar_monitor.py --simulate
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Dict, Optional

import yaml

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logger = logging.getLogger("solar")
_handler = logging.StreamHandler(sys.stdout)
_handler.setFormatter(logging.Formatter(
    "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
))
logger.addHandler(_handler)
logger.setLevel(logging.INFO)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
PROJECT_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_DIR / "config.yaml"

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
def load_config() -> dict:
    """Load configuration from config.yaml."""
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH) as f:
                return yaml.safe_load(f) or {}
        except Exception as e:
            logger.warning(f"Failed to load config: {e}")
    return {}


def get_cfg(path: str, default: Any = None) -> Any:
    """Get config value by dot-delimited path."""
    cfg = load_config()
    for key in path.split("."):
        if isinstance(cfg, dict):
            cfg = cfg.get(key)
        else:
            return default
    return cfg if cfg is not None else default


# ---------------------------------------------------------------------------
# Solar Data Models
# ---------------------------------------------------------------------------
@dataclass
class SolarReading:
    """A solar panel/battery reading."""
    panel_voltage_v: float
    panel_current_ma: float
    panel_power_mw: float
    battery_voltage_v: float
    battery_current_ma: float
    battery_percent: float
    charging: bool
    timestamp: datetime
    
    def to_dict(self) -> dict:
        return {
            "panel_voltage_v": self.panel_voltage_v,
            "panel_current_ma": self.panel_current_ma,
            "panel_power_mw": self.panel_power_mw,
            "battery_voltage_v": self.battery_voltage_v,
            "battery_current_ma": self.battery_current_ma,
            "battery_percent": self.battery_percent,
            "charging": self.charging,
            "timestamp": self.timestamp.isoformat(),
        }


# ---------------------------------------------------------------------------
# Solar Monitor
# ---------------------------------------------------------------------------
class SolarMonitor:
    """Monitors solar charging system via INA219 or ADC."""

    # LiPo battery curve (voltage -> percentage)
    BATTERY_CURVE = [
        (4.20, 100.0),
        (4.15, 95.0),
        (4.10, 90.0),
        (4.05, 85.0),
        (4.00, 75.0),
        (3.95, 65.0),
        (3.90, 55.0),
        (3.85, 45.0),
        (3.80, 35.0),
        (3.75, 25.0),
        (3.70, 15.0),
        (3.65, 10.0),
        (3.60, 5.0),
        (3.50, 2.0),
        (3.40, 1.0),
        (3.30, 0.0),
    ]

    def __init__(self):
        self.enabled = get_cfg("solar.enabled", False)
        self.i2c_address = get_cfg("solar.ina219_address", 0x40)
        self.panel_rating_w = get_cfg("solar.panel_rating_w", 2.0)
        self.battery_capacity_mah = get_cfg("solar.battery_capacity_mah", 2000)
        
        self._readings: list[SolarReading] = []
        self._max_readings = 1440  # 24 hours at 1/min
        self._lock = threading.Lock()
        
        # Try to import INA219 library
        self._ina219 = None
        if self.enabled:
            try:
                from ina219 import INA219
                self._ina219 = INA219(0.1, self.i2c_address)
                self._ina219.configure()
                logger.info(f"INA219 initialized at 0x{self.i2c_address:02X}")
            except ImportError:
                logger.warning("INA219 library not installed, using ADC fallback")
            except Exception as e:
                logger.warning(f"Failed to initialize INA219: {e}")

    def _voltage_to_percent(self, voltage_v: float) -> float:
        """Convert battery voltage to percentage."""
        if voltage_v >= 4.20:
            return 100.0
        if voltage_v <= 3.30:
            return 0.0
        
        # Interpolate from curve
        for i in range(len(self.BATTERY_CURVE) - 1):
            v_high, p_high = self.BATTERY_CURVE[i]
            v_low, p_low = self.BATTERY_CURVE[i + 1]
            if v_low <= voltage_v <= v_high:
                ratio = (voltage_v - v_low) / (v_high - v_low)
                return p_low + ratio * (p_high - p_low)
        return 0.0

    def read(self) -> Optional[SolarReading]:
        """Read current solar/battery status."""
        if not self.enabled:
            return None

        try:
            if self._ina219:
                # Read from INA219
                bus_voltage_v = self._ina219.voltage()
                current_ma = self._ina219.current()
                power_mw = self._ina219.power()
                
                # Assume bus voltage is battery voltage for single cell
                battery_v = bus_voltage_v
                charging = current_ma > 10  # Charging if positive current
                
                # Estimate panel voltage (typically 5V for 5V panels)
                panel_v = 5.0 if charging else battery_v
                panel_current = current_ma if charging else 0
                panel_power = panel_v * panel_current / 1000  # mW
                
            else:
                # Fallback: read from /sys/class/power_supply/
                battery_v = self._read_adc_voltage()
                current = self._read_adc_current()
                charging = current > 0
                
                panel_v = 5.0 if charging else battery_v
                panel_current = current
                panel_power = panel_v * panel_current
                current_ma = current * 1000
                power_mw = panel_power * 1000

            battery_percent = self._voltage_to_percent(battery_v)
            
            reading = SolarReading(
                panel_voltage_v=panel_v,
                panel_current_ma=panel_current,
                panel_power_mw=panel_power,
                battery_voltage_v=battery_v,
                battery_current_ma=current_ma,
                battery_percent=battery_percent,
                charging=charging,
                timestamp=datetime.now(timezone.utc),
            )
            
            with self._lock:
                self._readings.append(reading)
                if len(self._readings) > self._max_readings:
                    self._readings.pop(0)
            
            return reading
            
        except Exception as e:
            logger.error(f"Failed to read solar data: {e}")
            return None

    def _read_adc_voltage(self) -> float:
        """Read battery voltage from ADC (fallback)."""
        # Try to read from power supply sysfs
        try:
            voltage_now_path = "/sys/class/power_supply/BAT0/voltage_now"
            if os.path.exists(voltage_now_path):
                with open(voltage_now_path) as f:
                    return int(f.read().strip()) / 1_000_000  # µV to V
        except:
            pass
        
        # Simulation fallback
        return 3.85

    def _read_adc_current(self) -> float:
        """Read current from ADC (fallback)."""
        try:
            current_now_path = "/sys/class/power_supply/BAT0/current_now"
            if os.path.exists(current_now_path):
                with open(current_now_path) as f:
                    return int(f.read().strip()) / 1_000_000  # µA to A
        except:
            pass
        return 0.0

    def get_latest(self) -> Optional[SolarReading]:
        """Get most recent reading."""
        with self._lock:
            return self._readings[-1] if self._readings else None

    def get_day_summary(self) -> dict:
        """Get 24-hour summary."""
        with self._lock:
            if not self._readings:
                return {"error": "No data available"}
            
            recent = [r for r in self._readings 
                     if (datetime.now(timezone.utc) - r.timestamp).seconds < 86400]
            
            if not recent:
                return {"error": "No recent data"}
            
            avg_power = sum(r.panel_power_mw for r in recent) / len(recent)
            max_power = max(r.panel_power_mw for r in recent)
            min_battery = min(r.battery_percent for r in recent)
            max_battery = max(r.battery_percent for r in recent)
            
            charging_hours = sum(1 for r in recent if r.charging) / 60
            
            return {
                "period_hours": 24,
                "readings_count": len(recent),
                "avg_panel_power_mw": round(avg_power, 2),
                "max_panel_power_mw": round(max_power, 2),
                "min_battery_percent": round(min_battery, 1),
                "max_battery_percent": round(max_battery, 1),
                "charging_hours": round(charging_hours, 1),
                "efficiency_percent": round(avg_power / (self.panel_rating_w * 1000) * 100, 1) if self.panel_rating_w else 0,
            }

    def get_charging_recommendation(self) -> dict:
        """Get charging recommendations."""
        reading = self.get_latest()
        if not reading:
            return {"message": "Solar monitoring not available"}
        
        recommendations = []
        
        if reading.charging:
            if reading.panel_power_mw < 100:
                recommendations.append("Low light conditions. Consider moving to direct sunlight.")
            else:
                recommendations.append("Good charging conditions. Keep panel positioned toward sun.")
        else:
            if reading.battery_percent < 30:
                recommendations.append("Battery low and not charging. Connect to backup power.")
            elif reading.battery_percent < 50:
                recommendations.append("Battery below 50%. Seek charging opportunity soon.")
        
        if reading.battery_percent > 90:
            recommendations.append("Battery nearly full. System will switch to maintenance charging.")
        
        return {
            "charging": reading.charging,
            "battery_percent": reading.battery_percent,
            "panel_power_mw": reading.panel_power_mw,
            "recommendations": recommendations,
        }


# ---------------------------------------------------------------------------
# HTTP API Handler
# ---------------------------------------------------------------------------
class SolarHTTPHandler(BaseHTTPRequestHandler):
    """HTTP request handler for solar monitoring API."""

    solar_monitor: Optional[SolarMonitor] = None

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
        
        if path == "solar/health":
            self._send_json({
                "status": "ok",
                "service": "solar_monitor",
                "enabled": bool(self.solar_monitor and self.solar_monitor.enabled),
            })
            
        elif path == "solar/current":
            if not self.solar_monitor:
                self._send_error(503, "Solar monitor not initialized")
                return
            reading = self.solar_monitor.get_latest()
            if reading:
                self._send_json(reading.to_dict())
            else:
                self._send_error(503, "No solar data available")
                
        elif path == "solar/summary":
            if not self.solar_monitor:
                self._send_error(503, "Solar monitor not initialized")
                return
            summary = self.solar_monitor.get_day_summary()
            self._send_json(summary)
            
        elif path == "solar/recommendation":
            if not self.solar_monitor:
                self._send_error(503, "Solar monitor not initialized")
                return
            rec = self.solar_monitor.get_charging_recommendation()
            self._send_json(rec)
            
        else:
            self._send_error(404, f"Unknown endpoint: {path}")

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()


# ---------------------------------------------------------------------------
# Simulation Mode
# ---------------------------------------------------------------------------
class SolarSimulator:
    """Simulates solar charging for testing."""

    def __init__(self, monitor: SolarMonitor):
        self.monitor = monitor
        self.monitor.enabled = True  # Force enable for sim

    def run(self):
        """Run solar simulation scenarios."""
        logger.info("=== Solar Simulation Mode ===\n")
        
        scenarios = [
            # (panel_v, panel_ma, battery_v, battery_ma, desc)
            (5.0, 400, 3.9, 350, "Good sunlight, charging"),
            (5.0, 100, 4.0, 80, "Partial clouds, slow charging"),
            (4.5, 0, 3.7, -200, "Night, discharging"),
            (5.0, 600, 4.15, 50, "Full sun, nearly full battery"),
        ]
        
        for panel_v, panel_ma, battery_v, battery_ma, desc in scenarios:
            logger.info(f"[Scenario] {desc}")
            
            # Manually create reading
            reading = SolarReading(
                panel_voltage_v=panel_v,
                panel_current_ma=panel_ma,
                panel_power_mw=panel_v * panel_ma / 1000,
                battery_voltage_v=battery_v,
                battery_current_ma=battery_ma,
                battery_percent=self.monitor._voltage_to_percent(battery_v),
                charging=battery_ma > 0,
                timestamp=datetime.now(timezone.utc),
            )
            
            with self.monitor._lock:
                self.monitor._readings.append(reading)
            
            logger.info(f"  Battery: {reading.battery_percent:.1f}%")
            logger.info(f"  Panel power: {reading.panel_power_mw:.1f} mW")
            logger.info(f"  Charging: {reading.charging}")
            
            rec = self.monitor.get_charging_recommendation()
            for r in rec.get("recommendations", []):
                logger.info(f"  -> {r}")
            logger.info("")
        
        logger.info("=== Simulation Complete ===")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Dog Agent — Solar Charging Monitor")
    parser.add_argument("--port", type=int, default=9128, help="HTTP API port")
    parser.add_argument("--simulate", action="store_true", help="Run simulation mode")
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose logging")
    args = parser.parse_args()

    if args.verbose:
        logger.setLevel(logging.DEBUG)

    # Initialize monitor
    monitor = SolarMonitor()

    if args.simulate:
        sim = SolarSimulator(monitor)
        sim.run()
        return

    if not monitor.enabled:
        logger.warning("Solar monitoring disabled in config. Set solar.enabled: true")

    # Set up HTTP handler
    SolarHTTPHandler.solar_monitor = monitor

    # Start HTTP server
    server = HTTPServer(("127.0.0.1", args.port), SolarHTTPHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    logger.info(f"Solar API running on http://127.0.0.1:{args.port}")

    # Background polling
    def poll_loop():
        while True:
            if monitor.enabled:
                monitor.read()
            time.sleep(60)  # Read every minute

    poll_thread = threading.Thread(target=poll_loop, daemon=True)
    poll_thread.start()

    # Run until interrupted
    def signal_handler(sig, frame):
        logger.info(f"Received signal {sig}")
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
        logger.info("Solar monitor stopped")


if __name__ == "__main__":
    from dataclasses import dataclass
    main()