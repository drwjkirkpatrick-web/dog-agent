#!/usr/bin/env python3
"""
Dog Agent — Air Quality Monitor
==============================
Monitors air quality for smoke detection and respiratory safety.

Features:
  - SGP30 VOC sensor for volatile organic compounds
  - CCS811 CO2 equivalent sensing
  - Smoke detection during wildfire season
  - Air quality alerts for respiratory health

Usage:
    python src/air_quality.py
    python src/air_quality.py --simulate
"""

from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

# Logging
logger = logging.getLogger("air_quality")
handler = logging.StreamHandler(sys.stdout)
handler.setFormatter(logging.Formatter(
    "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
))
logger.addHandler(handler)
logger.setLevel(logging.INFO)

PROJECT_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_DIR / "config.yaml"

# Air quality thresholds (based on EPA/health guidelines)
VOC_THRESHOLDS = {
    "good": 0,
    "moderate": 200,
    "unhealthy_sensitive": 500,
    "unhealthy": 1000,
    "hazardous": 2000,
}

ECO2_THRESHOLDS = {
    "good": 400,
    "moderate": 1000,
    "unhealthy": 2000,
    "hazardous": 5000,
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
class AirQualityReading:
    voc_ppb: int
    eco2_ppm: int
    tvoc: Optional[float]
    timestamp: datetime
    
    def to_dict(self) -> dict:
        return {
            "voc_ppb": self.voc_ppb,
            "eco2_ppm": self.eco2_ppm,
            "tvoc": self.tvoc,
            "timestamp": self.timestamp.isoformat(),
        }
    
    def get_voc_level(self) -> str:
        if self.voc_ppb >= VOC_THRESHOLDS["hazardous"]:
            return "hazardous"
        elif self.voc_ppb >= VOC_THRESHOLDS["unhealthy"]:
            return "unhealthy"
        elif self.voc_ppb >= VOC_THRESHOLDS["unhealthy_sensitive"]:
            return "unhealthy_sensitive"
        elif self.voc_ppb >= VOC_THRESHOLDS["moderate"]:
            return "moderate"
        return "good"
    
    def get_eco2_level(self) -> str:
        if self.eco2_ppm >= ECO2_THRESHOLDS["hazardous"]:
            return "hazardous"
        elif self.eco2_ppm >= ECO2_THRESHOLDS["unhealthy"]:
            return "unhealthy"
        elif self.eco2_ppm >= ECO2_THRESHOLDS["moderate"]:
            return "moderate"
        return "good"


class AirQualityMonitor:
    """Monitors air quality via SGP30 or CCS811 sensors."""
    
    def __init__(self):
        self.enabled = get_cfg("air_quality.enabled", False)
        self.sensor_type = get_cfg("air_quality.sensor_type", "sgp30")
        self.i2c_bus = get_cfg("air_quality.i2c_bus", 1)
        self.poll_interval_sec = get_cfg("air_quality.poll_interval_sec", 10)
        self.baseline_save_interval = get_cfg("air_quality.baseline_save_interval_hours", 24)
        
        self._sgp30 = None
        self._ccs811 = None
        self._last_reading: Optional[AirQualityReading] = None
        self._readings: List[AirQualityReading] = []
        self._lock = threading.Lock()
        
        if self.enabled:
            self._init_sensor()
    
    def _init_sensor(self) -> None:
        try:
            import smbus2
            bus = smbus2.SMBus(self.i2c_bus)
            
            if self.sensor_type == "sgp30":
                from sgp30 import SGP30
                self._sgp30 = SGP30(bus)
                self._sgp30.init_sgp()
                logger.info("SGP30 air quality sensor initialized")
            elif self.sensor_type == "ccs811":
                # CCS811 initialization
                bus.write_byte_data(0x5A, 0xF4, 0x00)  # Reset
                time.sleep(0.1)
                bus.write_byte_data(0x5A, 0x01, 0x10)  # Mode 1
                self._ccs811 = bus
                logger.info("CCS811 air quality sensor initialized")
                
        except ImportError as e:
            logger.warning(f"Sensor library not available: {e}")
        except Exception as e:
            logger.warning(f"Failed to initialize sensor: {e}")
    
    def read(self) -> Optional[AirQualityReading]:
        if not self.enabled:
            return None
        
        try:
            if self._sgp30:
                result = self._sgp30.get_air_quality()
                reading = AirQualityReading(
                    voc_ppb=result.equivalent_co2,
                    eco2_ppm=result.total_voc,
                    tvoc=None,
                    timestamp=datetime.now(timezone.utc),
                )
            elif self._ccs811:
                status = self._ccs811.read_byte_data(0x5A, 0x00)
                if status & 0x08:  # Data ready
                    data = self._ccs811.read_i2c_block_data(0x5A, 0x02, 4)
                    eco2 = (data[0] << 8) | data[1]
                    tvoc = (data[2] << 8) | data[3]
                    reading = AirQualityReading(
                        voc_ppb=tvoc,
                        eco2_ppm=eco2,
                        tvoc=tvoc / 1000.0 if tvoc < 65535 else None,
                        timestamp=datetime.now(timezone.utc),
                    )
                else:
                    return None
            else:
                return None
            
            with self._lock:
                self._last_reading = reading
                self._readings.append(reading)
                if len(self._readings) > 1000:
                    self._readings.pop(0)
            
            return reading
            
        except Exception as e:
            logger.error(f"Failed to read air quality: {e}")
            return None
    
    def check_alerts(self) -> List[Dict]:
        """Check for air quality alerts."""
        reading = self.get_latest()
        if not reading:
            return []
        
        alerts = []
        voc_level = reading.get_voc_level()
        eco2_level = reading.get_eco2_level()
        
        if voc_level in ["unhealthy", "hazardous"]:
            alerts.append({
                "type": "voc",
                "level": voc_level,
                "value": reading.voc_ppb,
                "message": f"High VOC levels detected: {reading.voc_ppb} ppb",
            })
        
        if eco2_level in ["unhealthy", "hazardous"]:
            alerts.append({
                "type": "eco2",
                "level": eco2_level,
                "value": reading.eco2_ppm,
                "message": f"High CO2 equivalent: {reading.eco2_ppm} ppm",
            })
        
        return alerts
    
    def get_latest(self) -> Optional[AirQualityReading]:
        with self._lock:
            return self._last_reading
    
    def get_summary(self) -> dict:
        reading = self.get_latest()
        if not reading:
            return {"error": "No data available"}
        
        return {
            "current": reading.to_dict(),
            "voc_level": reading.get_voc_level(),
            "eco2_level": reading.get_eco2_level(),
            "sensor_type": self.sensor_type,
        }


class AirQualityHTTPHandler(BaseHTTPRequestHandler):
    """HTTP API for air quality monitoring."""
    
    monitor: Optional[AirQualityMonitor] = None
    
    def log_message(self, format, *args):
        logger.debug(f"HTTP: {format % args}")
    
    def _send_json(self, data: Any, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data, indent=2).encode())
    
    def _send_error(self, status: int, message: str) -> None:
        self._send_json({"error": message}, status)
    
    def do_GET(self):
        path = self.path.strip("/")
        
        if path == "air/health":
            self._send_json({
                "status": "ok",
                "service": "air_quality",
                "enabled": bool(self.monitor and self.monitor.enabled),
            })
        elif path == "air/current":
            if not self.monitor:
                self._send_error(503, "Air quality monitor not initialized")
                return
            reading = self.monitor.get_latest()
            if reading:
                self._send_json(reading.to_dict())
            else:
                self._send_error(503, "No air quality data available")
        elif path == "air/summary":
            if not self.monitor:
                self._send_error(503, "Air quality monitor not initialized")
                return
            self._send_json(self.monitor.get_summary())
        elif path == "air/alerts":
            if not self.monitor:
                self._send_error(503, "Air quality monitor not initialized")
                return
            alerts = self.monitor.check_alerts()
            self._send_json({"alerts": alerts})
        else:
            self._send_error(404, f"Unknown endpoint: {path}")


def main():
    parser = argparse.ArgumentParser(description="Dog Agent — Air Quality Monitor")
    parser.add_argument("--port", type=int, default=9134, help="HTTP API port")
    parser.add_argument("--simulate", action="store_true", help="Run simulation")
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose logging")
    args = parser.parse_args()
    
    if args.verbose:
        logger.setLevel(logging.DEBUG)
    
    monitor = AirQualityMonitor()
    
    if args.simulate:
        logger.info("=== Air Quality Simulation ===")
        for voc in [0, 200, 500, 1000, 2000]:
            reading = AirQualityReading(
                voc_ppb=voc,
                eco2_ppm=400 + voc // 2,
                tvoc=voc / 1000.0,
                timestamp=datetime.now(timezone.utc),
            )
            logger.info(f"VOC: {voc} ppb - Level: {reading.get_voc_level()}")
        return
    
    AirQualityHTTPHandler.monitor = monitor
    
    server = HTTPServer(("127.0.0.1", args.port), AirQualityHTTPHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    logger.info(f"Air quality API on http://127.0.0.1:{args.port}")
    
    # Polling loop
    def poll():
        while True:
            if monitor.enabled:
                reading = monitor.read()
                if reading:
                    alerts = monitor.check_alerts()
                    for alert in alerts:
                        logger.warning(f"AIR ALERT: {alert['message']}")
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
        logger.info("Air quality monitor stopped")


if __name__ == "__main__":
    main()
