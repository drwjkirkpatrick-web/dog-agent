#!/usr/bin/env python3
"""
GPS Daemon — Dog Agent
=======================
Reads NMEA sentences from a UART serial port, parses $GPGGA and $GPRMC
sentences via pynmea2, and serves the current position via a local HTTP API.

Usage:
    python src/gps_daemon.py              # Normal mode (reads from serial)
    python src/gps_daemon.py --test        # Test mode (generates fake NMEA data)
    python src/gps_daemon.py --config /path/to/config.yaml
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import signal
import sys
import threading
import time
from contextlib import suppress
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import yaml

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logger = logging.getLogger("gps_daemon")
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
class GPSPosition:
    """Thread-safe container for the latest parsed GPS position."""

    __slots__ = (
        "_lock", "lat", "lon", "altitude", "speed", "heading",
        "fix_quality", "timestamp", "satellites", "valid",
    )

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.lat: float = 0.0
        self.lon: float = 0.0
        self.altitude: float = 0.0
        self.speed: float = 0.0       # knots
        self.heading: float = 0.0     # degrees true
        self.fix_quality: int = 0     # 0=invalid, 1=GPS, 2=DGPS, ...
        self.timestamp: Optional[datetime] = None
        self.satellites: int = 0
        self.valid: bool = False

    def update_from_gga(self, gga: Any) -> None:
        """Update from a pynmea2 $GPGGA sentence."""
        with self._lock:
            if gga.latitude and gga.longitude:
                self.lat = gga.latitude
                self.lon = gga.longitude
                self.altitude = gga.altitude if gga.altitude is not None else 0.0
                self.fix_quality = gga.gps_qual if gga.gps_qual is not None else 0
                self.satellites = gga.num_sats if gga.num_sats is not None else 0
                self.valid = self.fix_quality > 0
                # GGA has time but no date — preserve existing date if available
                if gga.timestamp:
                    now = datetime.now(timezone.utc)
                    self.timestamp = datetime(
                        now.year, now.month, now.day,
                        gga.timestamp.hour, gga.timestamp.minute,
                        gga.timestamp.second,
                        tzinfo=timezone.utc,
                    )

    def update_from_rmc(self, rmc: Any) -> None:
        """Update from a pynmea2 $GPRMC sentence (has date + speed + heading)."""
        with self._lock:
            if rmc.latitude and rmc.longitude:
                self.lat = rmc.latitude
                self.lon = rmc.longitude
                self.speed = rmc.spd_over_grnd if rmc.spd_over_grnd is not None else 0.0
                self.heading = rmc.true_course if rmc.true_course is not None else 0.0
                self.valid = rmc.status == "A"
                if rmc.datestamp and rmc.timestamp:
                    self.timestamp = datetime(
                        rmc.datestamp.year, rmc.datestamp.month,
                        rmc.datestamp.day,
                        rmc.timestamp.hour, rmc.timestamp.minute,
                        rmc.timestamp.second,
                        tzinfo=timezone.utc,
                    )

    def snapshot(self) -> Dict[str, Any]:
        """Return a thread-safe copy of the current position as a dict."""
        with self._lock:
            return {
                "lat": self.lat,
                "lon": self.lon,
                "altitude": self.altitude,
                "speed_knots": self.speed,
                "speed_mps": self.speed * 0.514444,
                "heading": self.heading,
                "fix_quality": self.fix_quality,
                "satellites": self.satellites,
                "valid": self.valid,
                "timestamp": self.timestamp.isoformat() if self.timestamp else None,
            }

    def to_csv_row(self) -> Dict[str, Any]:
        """Return a flat dict suitable for CSV logging."""
        snap = self.snapshot()
        snap["timestamp"] = snap["timestamp"] or datetime.now(timezone.utc).isoformat()
        return snap


# ---------------------------------------------------------------------------
# Fake NMEA generator (test mode)
# ---------------------------------------------------------------------------
class FakeNMEASource:
    """Generates plausible NMEA sentences for development without GPS hardware.

    Produces coordinates in DDDMM.MMMM format as required by the NMEA 0183
    standard (pynmea2 expects this format).
    """

    # Base coordinates in decimal degrees (Portland, OR area)
    BASE_LAT_DD = 45.5152
    BASE_LON_DD = -122.6784

    def __init__(self, update_hz: float = 10.0) -> None:
        self._interval = 1.0 / update_hz
        self._t0 = time.monotonic()
        self._step = 0

    @staticmethod
    def _dd_to_nmea_lat(dec_deg: float) -> str:
        """Convert decimal degrees latitude to NMEA DDDMM.MMMM format."""
        deg = int(abs(dec_deg))
        minutes = (abs(dec_deg) - deg) * 60.0
        return f"{deg:02d}{minutes:07.4f}"

    @staticmethod
    def _dd_to_nmea_lon(dec_deg: float) -> str:
        """Convert decimal degrees longitude to NMEA DDDMM.MMMM format."""
        deg = int(abs(dec_deg))
        minutes = (abs(dec_deg) - deg) * 60.0
        return f"{deg:03d}{minutes:07.4f}"

    def readline(self) -> str:
        """Return a synthetic NMEA sentence (alternates GGA and RMC)."""
        time.sleep(self._interval)
        self._step += 1
        # Simulate slow movement (~0.5 m/s) in a small circle
        offset_lat = 0.0001 * (self._step % 100) / 100.0
        offset_lon = 0.0001 * ((self._step * 3) % 100) / 100.0
        lat_dd = self.BASE_LAT_DD + offset_lat
        lon_dd = self.BASE_LON_DD + offset_lon
        now = datetime.now(timezone.utc)
        time_str = now.strftime("%H%M%S.%f")[:9]
        date_str = now.strftime("%d%m%y")

        lat_nmea = self._dd_to_nmea_lat(lat_dd)
        lon_nmea = self._dd_to_nmea_lon(lon_dd)
        lat_dir = "N" if lat_dd >= 0 else "S"
        lon_dir = "E" if lon_dd >= 0 else "W"

        if self._step % 2 == 0:
            # $GPGGA
            body = (
                f"GPGGA,{time_str},{lat_nmea},{lat_dir},{lon_nmea},{lon_dir},"
                f"1,08,1.2,{100.0 + self._step % 10:.1f},M,0.0,M,,"
            )
            return f"${body}*{self._checksum(body)}\r\n"
        else:
            # $GPRMC
            speed_knots = 1.0 + (self._step % 20) * 0.1
            heading = (self._step * 5) % 360
            body = (
                f"GPRMC,{time_str},A,{lat_nmea},{lat_dir},{lon_nmea},{lon_dir},"
                f"{speed_knots:.1f},{heading:.1f},{date_str},0.0,E,A"
            )
            return f"${body}*{self._checksum(body)}\r\n"

    @staticmethod
    def _checksum(sentence: str) -> str:
        cksum = 0
        for ch in sentence:
            cksum ^= ord(ch)
        return f"{cksum:02X}"

    def close(self) -> None:
        pass


# ---------------------------------------------------------------------------
# CSV Track Logger
# ---------------------------------------------------------------------------
class TrackLogger:
    """Logs GPS positions to a daily CSV file."""

    FIELD_NAMES = [
        "timestamp", "lat", "lon", "altitude", "speed_knots",
        "speed_mps", "heading", "fix_quality", "satellites", "valid",
    ]

    def __init__(self, directory: str) -> None:
        self._directory = Path(directory)
        self._directory.mkdir(parents=True, exist_ok=True)
        self._file: Optional[Any] = None
        self._writer: Optional[csv.DictWriter] = None
        self._current_date: Optional[str] = None
        self._lock = threading.Lock()

    def write(self, position: GPSPosition) -> None:
        """Write the current position to today's CSV file."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        with self._lock:
            if today != self._current_date:
                self._rotate(today)
            if self._writer:
                self._writer.writerow(position.to_csv_row())
                self._file.flush()

    def _rotate(self, today: str) -> None:
        """Close old file, open new file for today."""
        if self._file:
            self._file.close()
        filepath = self._directory / f"gps_track_{today}.csv"
        self._file = open(filepath, "a", newline="")  # noqa: SIM115
        self._writer = csv.DictWriter(self._file, fieldnames=self.FIELD_NAMES)
        # Write header if file is new/empty
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
# HTTP API Handler
# ---------------------------------------------------------------------------
class GPSAPIHandler(BaseHTTPRequestHandler):
    """Serves the current GPS position as JSON."""

    # Class-level reference set by the server
    position: GPSPosition = None  # type: ignore[assignment]

    def do_GET(self) -> None:
        if self.path == "/gps":
            self._json_response(self.position.snapshot())
        elif self.path == "/gps/health":
            self._json_response({"status": "ok", "service": "gps_daemon"})
        else:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b'{"error": "not found"}')

    def _json_response(self, data: dict) -> None:
        body = json.dumps(data, indent=2).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: Any) -> None:
        """Quiet the default HTTP server logging."""
        logger.debug(f"HTTP: {fmt % args}")


# ---------------------------------------------------------------------------
# Serial reader thread
# ---------------------------------------------------------------------------
def serial_reader(
    source: Any,
    position: GPSPosition,
    stop_event: threading.Event,
    track_logger: Optional[TrackLogger] = None,
) -> None:
    """Read NMEA lines from *source*, parse them, and update *position*."""
    import pynmea2

    while not stop_event.is_set():
        try:
            line = source.readline()
        except Exception:
            logger.exception("Error reading from source")
            time.sleep(1)
            continue

        if not line:
            continue

        line = line.strip()
        if not line or not line.startswith("$"):
            continue

        try:
            msg = pynmea2.parse(line)
        except pynmea2.ParseError:
            continue  # silently skip malformed lines

        if isinstance(msg, pynmea2.types.talker.GGA):
            position.update_from_gga(msg)
            if track_logger and position.valid:
                track_logger.write(position)
        elif isinstance(msg, pynmea2.types.talker.RMC):
            position.update_from_rmc(msg)
            if track_logger and position.valid:
                track_logger.write(position)


# ---------------------------------------------------------------------------
# Configuration loader
# ---------------------------------------------------------------------------
def load_config(path: str) -> dict:
    """Load YAML config, returning defaults for missing GPS keys."""
    with open(path) as f:
        cfg = yaml.safe_load(f) or {}

    gps_cfg = cfg.get("gps", {})
    log_cfg = cfg.get("logging", {})

    return {
        "port": gps_cfg.get("port", "/dev/ttyS0"),
        "baudrate": gps_cfg.get("baudrate", 9600),
        "update_hz": gps_cfg.get("update_hz", 10),
        "enabled": gps_cfg.get("enabled", True),
        "gps_track_dir": log_cfg.get("gps_track_dir", "data/gps_tracks"),
        "api_port": cfg.get("hermes", {}).get("api_port", 9110),
    }


# ---------------------------------------------------------------------------
# Main daemon
# ---------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(description="Dog Agent GPS Daemon")
    parser.add_argument(
        "--config",
        default=None,
        help="Path to config.yaml (default: ../config.yaml relative to this script)",
    )
    parser.add_argument("--test", action="store_true", help="Test mode — fake NMEA data")
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
        cfg = {
            "port": "/dev/ttyS0",
            "baudrate": 9600,
            "update_hz": 10,
            "enabled": True,
            "gps_track_dir": "data/gps_tracks",
            "api_port": 9110,
        }

    if not cfg["enabled"]:
        logger.info("GPS module is disabled in config. Exiting.")
        return

    # Shared state
    position = GPSPosition()
    stop_event = threading.Event()

    # Track logger
    track_dir = cfg["gps_track_dir"]
    if not os.path.isabs(track_dir):
        # Resolve relative to project root (parent of src/)
        script_dir = Path(__file__).resolve().parent
        track_dir = str(script_dir.parent / track_dir)
    track_logger = TrackLogger(track_dir)
    logger.info("GPS tracks logged to %s", track_dir)

    # Open serial source
    if args.test:
        logger.info("TEST MODE — using fake NMEA data")
        source = FakeNMEASource(update_hz=cfg["update_hz"])
    else:
        import serial

        logger.info(
            "Opening serial port %s @ %d baud",
            cfg["port"],
            cfg["baudrate"],
        )
        try:
            source = serial.Serial(
                port=cfg["port"],
                baudrate=cfg["baudrate"],
                timeout=1,
            )
        except serial.SerialException as e:
            logger.error("Failed to open serial port: %s", e)
            logger.error(
                "Use --test mode for development without hardware."
            )
            sys.exit(1)

    # Start serial reader thread
    reader_thread = threading.Thread(
        target=serial_reader,
        args=(source, position, stop_event, track_logger),
        name="gps-reader",
        daemon=True,
    )
    reader_thread.start()
    logger.info("GPS reader thread started")

    # Start HTTP API server
    GPSAPIHandler.position = position
    api_port = cfg["api_port"]
    server = HTTPServer(("127.0.0.1", api_port), GPSAPIHandler)
    server_thread = threading.Thread(
        target=server.serve_forever,
        name="gps-api",
        daemon=True,
    )
    server_thread.start()
    logger.info("GPS API server listening on http://127.0.0.1:%d/gps", api_port)

    # ------------------------------------------------------------------
    # Graceful shutdown
    # ------------------------------------------------------------------
    def shutdown(signum: int, frame: Any) -> None:
        logger.info("Received signal %d — shutting down...", signum)
        stop_event.set()
        server.shutdown()
        source.close()
        track_logger.close()
        logger.info("GPS daemon stopped.")
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
