#!/usr/bin/env python3
"""
Dog Agent — Multi-Constellation GPS
===================================
Enhanced GPS with GPS + GLONASS + Galileo + BeiDou support.

Uses u-blox NEO-M9N for faster, more accurate positioning.

Usage:
    python src/gps_multi.py
    python src/gps_multi.py --simulate
"""

from __future__ import annotations

import argparse
import json
import logging
import serial
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
logger = logging.getLogger("gps_multi")
handler = logging.StreamHandler(sys.stdout)
handler.setFormatter(logging.Formatter(
    "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
))
logger.addHandler(handler)
logger.setLevel(logging.INFO)

PROJECT_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_DIR / "config.yaml"

# u-blox NMEA extensions for multi-constellation
UBX_CMD_ENABLE_GNSS = bytes([
    0xB5, 0x62, 0x06, 0x3E, 0x3C, 0x00,  # UBX-CFG-GNSS
    0x00, 0x00, 0x20, 0x07,               # 7 GNSS systems
    0x00, 0x08, 0x10, 0x00, 0x01, 0x00, 0x01, 0x01,  # GPS
    0x01, 0x01, 0x03, 0x00, 0x01, 0x00, 0x01, 0x01,  # SBAS
    0x02, 0x04, 0x08, 0x00, 0x01, 0x00, 0x01, 0x01,  # Galileo
    0x03, 0x08, 0x10, 0x00, 0x00, 0x00, 0x01, 0x01,  # BeiDou
    0x04, 0x00, 0x08, 0x00, 0x00, 0x00, 0x01, 0x03,  # IMES
    0x05, 0x00, 0x03, 0x00, 0x00, 0x00, 0x01, 0x05,  # QZSS
    0x06, 0x08, 0x0E, 0x00, 0x01, 0x00, 0x01, 0x01,  # GLONASS
    0x7A, 0x5C  # Checksum
])


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
class GNSSFix:
    """Multi-constellation GPS fix."""
    latitude: float
    longitude: float
    altitude_m: Optional[float]
    speed_ms: Optional[float]
    heading: Optional[float]
    fix_quality: int
    satellites_used: int
    hdop: Optional[float]
    constellations: List[str]
    timestamp: datetime
    
    def to_dict(self) -> dict:
        return {
            "latitude": self.latitude,
            "longitude": self.longitude,
            "altitude_m": self.altitude_m,
            "speed_ms": self.speed_ms,
            "heading": self.heading,
            "fix_quality": self.fix_quality,
            "satellites_used": self.satellites_used,
            "hdop": self.hdop,
            "constellations": self.constellations,
            "timestamp": self.timestamp.isoformat(),
        }


class MultiConstellationGPS:
    """u-blox NEO-M9N multi-GNSS receiver."""
    
    def __init__(self):
        self.enabled = get_cfg("gps_multi.enabled", False)
        self.port = get_cfg("gps_multi.port", "/dev/ttyACM0")
        self.baudrate = get_cfg("gps_multi.baudrate", 9600)
        self.timeout_sec = get_cfg("gps_multi.timeout_sec", 1)
        
        self._serial: Optional[serial.Serial] = None
        self._last_fix: Optional[GNSSFix] = None
        self._fix_history: List[GNSSFix] = []
        self._lock = threading.Lock()
        self._running = False
        
        if self.enabled:
            self._init_gps()
    
    def _init_gps(self) -> None:
        try:
            self._serial = serial.Serial(
                self.port,
                self.baudrate,
                timeout=self.timeout_sec
            )
            # Send configuration for multi-constellation
            self._serial.write(UBX_CMD_ENABLE_GNSS)
            logger.info(f"Multi-constellation GPS initialized on {self.port}")
        except Exception as e:
            logger.warning(f"Failed to initialize GPS: {e}")
    
    def _parse_nmea(self, line: str) -> Optional[GNSSFix]:
        """Parse NMEA sentence."""
        if not line.startswith("$"):
            return None
        
        parts = line.strip().split(",")
        
        if parts[0] == "$GNGGA":  # Multi-GNSS fix
            try:
                time_str = parts[1]
                lat = self._parse_coord(parts[2], parts[3])
                lon = self._parse_coord(parts[4], parts[5])
                fix_quality = int(parts[6])
                sats = int(parts[7])
                hdop = float(parts[8]) if parts[8] else None
                alt = float(parts[9]) if parts[9] else None
                
                return GNSSFix(
                    latitude=lat,
                    longitude=lon,
                    altitude_m=alt,
                    speed_ms=None,
                    heading=None,
                    fix_quality=fix_quality,
                    satellites_used=sats,
                    hdop=hdop,
                    constellations=[],
                    timestamp=datetime.now(timezone.utc),
                )
            except (ValueError, IndexError):
                return None
        
        elif parts[0] in ["$GNRMC", "$GPRMC"]:  # Recommended minimum
            try:
                lat = self._parse_coord(parts[3], parts[4])
                lon = self._parse_coord(parts[5], parts[6])
                speed = float(parts[7]) * 0.514444 if parts[7] else None  # knots to m/s
                heading = float(parts[8]) if parts[8] else None
                
                return GNSSFix(
                    latitude=lat,
                    longitude=lon,
                    altitude_m=None,
                    speed_ms=speed,
                    heading=heading,
                    fix_quality=1 if parts[2] == "A" else 0,
                    satellites_used=0,
                    hdop=None,
                    constellations=[],
                    timestamp=datetime.now(timezone.utc),
                )
            except (ValueError, IndexError):
                return None
        
        return None
    
    def _parse_coord(self, coord: str, direction: str) -> float:
        """Parse coordinate from NMEA format."""
        if not coord:
            return 0.0
        
        degrees = float(coord[:2]) if len(coord) > 5 else float(coord[:3])
        minutes = float(coord[2:] if len(coord) > 5 else coord[3:])
        decimal = degrees + minutes / 60
        
        if direction in ["S", "W"]:
            decimal = -decimal
        return decimal
    
    def read(self) -> Optional[GNSSFix]:
        if not self.enabled or not self._serial:
            return None
        
        try:
            line = self._serial.readline().decode("ascii", errors="ignore")
            fix = self._parse_nmea(line)
            
            if fix:
                with self._lock:
                    self._last_fix = fix
                    self._fix_history.append(fix)
                    if len(self._fix_history) > 100:
                        self._fix_history.pop(0)
            
            return fix
        except Exception as e:
            logger.error(f"GPS read error: {e}")
            return None
    
    def get_latest(self) -> Optional[GNSSFix]:
        with self._lock:
            return self._last_fix
    
    def get_stats(self) -> dict:
        fix = self.get_latest()
        if not fix:
            return {"error": "No GPS fix available"}
        
        return {
            "current": fix.to_dict(),
            "constellations": ["GPS", "GLONASS", "Galileo", "BeiDou"],
            "fix_quality": fix.fix_quality,
            "satellites_used": fix.satellites_used,
            "hdop": fix.hdop,
        }


class MultiGPSHTTPHandler(BaseHTTPRequestHandler):
    """HTTP API for multi-constellation GPS."""
    
    gps: Optional[MultiConstellationGPS] = None
    
    def log_message(self, format, *args):
        logger.debug(f"HTTP: {format % args}")
    
    def _send_json(self, data: Any, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data, indent=2).encode())
    
    def do_GET(self):
        path = self.path.strip("/")
        
        if path == "gps_multi/health":
            self._send_json({
                "status": "ok",
                "service": "gps_multi",
                "enabled": bool(self.gps and self.gps.enabled),
            })
        elif path == "gps_multi/current":
            if not self.gps:
                self._send_json({"error": "GPS not initialized"}, 503)
                return
            fix = self.gps.get_latest()
            if fix:
                self._send_json(fix.to_dict())
            else:
                self._send_json({"error": "No fix available"}, 503)
        elif path == "gps_multi/stats":
            if not self.gps:
                self._send_json({"error": "GPS not initialized"}, 503)
                return
            self._send_json(self.gps.get_stats())
        else:
            self._send_json({"error": f"Unknown endpoint: {path}"}, 404)


def main():
    parser = argparse.ArgumentParser(description="Dog Agent — Multi-Constellation GPS")
    parser.add_argument("--port", type=int, default=9138, help="HTTP API port")
    parser.add_argument("--simulate", action="store_true", help="Run simulation")
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose logging")
    args = parser.parse_args()
    
    if args.verbose:
        logger.setLevel(logging.DEBUG)
    
    gps = MultiConstellationGPS()
    
    if args.simulate:
        logger.info("=== Multi-GPS Simulation ===")
        logger.info("Supports: GPS, GLONASS, Galileo, BeiDou")
        return
    
    MultiGPSHTTPHandler.gps = gps
    
    server = HTTPServer(("127.0.0.1", args.port), MultiGPSHTTPHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    logger.info(f"Multi-GPS API on http://127.0.0.1:{args.port}")
    
    # Reading loop
    def read_loop():
        while True:
            if gps.enabled:
                gps.read()
            time.sleep(0.1)
    
    read_thread = threading.Thread(target=read_loop, daemon=True)
    read_thread.start()
    
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
        logger.info("Multi-GPS stopped")


if __name__ == "__main__":
    main()
