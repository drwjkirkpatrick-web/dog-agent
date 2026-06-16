#!/usr/bin/env python3
"""
Sensor Daemon — Dog Agent
==========================
Reads LilyPad Arduino sensors over I2C (heart rate, temperature, accelerometer)
and serves the current readings via a local HTTP API.

I2C Protocol (LilyPad Arduino):
  - HR sensor:     read 2 bytes from register 0x00, convert to BPM
  - Temp sensor:   read 2 bytes from register 0x00, convert to °C (0.0625 °C/LSB)
  - Accelerometer: read 6 bytes from register 0x02 (X low, X high, Y low, Y high, Z low, Z high)

Usage:
    python src/sensor_daemon.py               # Normal mode (reads from I2C bus)
    python src/sensor_daemon.py --simulate     # Simulate mode (fake sensor data)
    python src/sensor_daemon.py --config /path/to/config.yaml
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import random
import signal
import sys
import threading
import time
from contextlib import suppress
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Dict, Optional

import yaml


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logger = logging.getLogger("sensor_daemon")
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
class SensorReadings:
    """Thread-safe container for the latest sensor readings."""

    __slots__ = (
        "_lock", "heart_rate_bpm", "temperature_c",
        "accel_x_g", "accel_y_g", "accel_z_g",
        "timestamp", "valid",
    )

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.heart_rate_bpm: float = 0.0
        self.temperature_c: float = 0.0
        self.accel_x_g: float = 0.0
        self.accel_y_g: float = 0.0
        self.accel_z_g: float = 0.0
        self.timestamp: Optional[datetime] = None
        self.valid: bool = False

    def update(
        self,
        heart_rate_bpm: float,
        temperature_c: float,
        accel_x_g: float,
        accel_y_g: float,
        accel_z_g: float,
    ) -> None:
        """Atomically update all readings."""
        with self._lock:
            self.heart_rate_bpm = heart_rate_bpm
            self.temperature_c = temperature_c
            self.accel_x_g = accel_x_g
            self.accel_y_g = accel_y_g
            self.accel_z_g = accel_z_g
            self.timestamp = datetime.now(timezone.utc)
            self.valid = True

    def snapshot(self) -> Dict[str, Any]:
        """Return a thread-safe copy of the current readings as a dict."""
        with self._lock:
            magnitude_g = math.sqrt(
                self.accel_x_g ** 2 +
                self.accel_y_g ** 2 +
                self.accel_z_g ** 2
            )
            return {
                "heart_rate_bpm": self.heart_rate_bpm,
                "temperature_c": self.temperature_c,
                "temperature_f": self.temperature_c * 9.0 / 5.0 + 32.0,
                "acceleration": {
                    "x_g": self.accel_x_g,
                    "y_g": self.accel_y_g,
                    "z_g": self.accel_z_g,
                    "magnitude_g": round(magnitude_g, 3),
                },
                "valid": self.valid,
                "timestamp": self.timestamp.isoformat() if self.timestamp else None,
            }


# ---------------------------------------------------------------------------
# Configuration loader
# ---------------------------------------------------------------------------
def load_config(path: str) -> dict:
    """Load YAML config, returning defaults for missing sensor keys."""
    with open(path) as f:
        cfg = yaml.safe_load(f) or {}

    sensor_cfg = cfg.get("sensors", {})
    hermes_cfg = cfg.get("hermes", {})

    return {
        "enabled": sensor_cfg.get("enabled", False),
        "i2c_bus": sensor_cfg.get("i2c_bus", 1),
        "hr_sensor_addr": sensor_cfg.get("hr_sensor_addr", 0x57),
        "temp_sensor_addr": sensor_cfg.get("temp_sensor_addr", 0x48),
        "accel_addr": sensor_cfg.get("accel_addr", 0x18),
        "api_port": hermes_cfg.get("api_port", 9110),
    }


# ---------------------------------------------------------------------------
# Simulated sensor (fake data generator)
# ---------------------------------------------------------------------------
class SimulatedSensors:
    """Generates realistic fake LilyPad sensor data for development.

    Produces plausible values:
      - Heart rate: 60–120 BPM, with occasional spikes
      - Temperature: 37.5–39.5 °C (normal canine range)
      - Accelerometer: -2G to +2G, with ~1G on Z (gravity)
    """

    def __init__(self, seed: int = 42) -> None:
        self._rng = random.Random(seed)
        self._t0 = time.monotonic()
        # Baseline values
        self._hr_baseline = 70.0
        self._temp_baseline = 38.5
        # Slow drift target
        self._hr_target = 70.0
        self._temp_target = 38.5

    def read_all(self) -> Dict[str, float]:
        """Return a dict of simulated sensor readings.

        Simulates:
          - Gradual HR drift with occasional spikes (simulating movement/excitement)
          - Small temperature oscillations
          - Accelerometer with gravity on Z, plus gentle movement noise
        """
        elapsed = time.monotonic() - self._t0

        # -- Heart rate --
        # Every 15–45 seconds, pick a new target (resting vs active)
        if self._rng.random() < 0.02:  # ~2% chance per call
            if self._rng.random() < 0.3:
                # Activity spike: 90–140 BPM
                self._hr_target = self._rng.uniform(90.0, 140.0)
            else:
                # Resting: 60–85 BPM
                self._hr_target = self._rng.uniform(60.0, 85.0)
        # Smoothly glide toward target
        self._hr_baseline += (self._hr_target - self._hr_baseline) * 0.05
        hr_noise = self._rng.gauss(0, 2.0)  # ±2 BPM noise
        heart_rate = max(35.0, min(200.0, self._hr_baseline + hr_noise))

        # -- Temperature --
        # Small sinusoidal drift + noise
        temp_oscillation = 0.3 * math.sin(elapsed * 0.01)
        if self._rng.random() < 0.005:
            # Brief spike from exertion
            self._temp_target = self._rng.uniform(38.8, 39.3)
        else:
            self._temp_target = 38.5
        self._temp_baseline += (self._temp_target - self._temp_baseline) * 0.02
        temp_noise = self._rng.gauss(0, 0.05)  # ±0.05 °C noise
        temperature = max(36.0, min(41.0, self._temp_baseline + temp_oscillation + temp_noise))

        # -- Accelerometer --
        # Simulate a dog that is mostly still with small movements
        # Gravity vector: ~1G down on Z
        base_z = 0.98 + self._rng.gauss(0, 0.02)
        # Gentle swaying (simulates breathing, small shifts)
        sway_x = 0.05 * math.sin(elapsed * 0.3) + self._rng.gauss(0, 0.02)
        sway_y = 0.05 * math.sin(elapsed * 0.4 + 1.2) + self._rng.gauss(0, 0.02)
        sway_z = 0.03 * math.sin(elapsed * 0.2 + 0.7)

        # Occasional movement burst
        if self._rng.random() < 0.01:  # ~1% chance
            burst = self._rng.uniform(0.1, 0.8)
            angle = self._rng.uniform(0, 2 * math.pi)
            sway_x += burst * math.cos(angle)
            sway_y += burst * math.sin(angle)
            sway_z += burst * self._rng.uniform(-0.3, 0.3)

        accel_x = round(sway_x, 3)
        accel_y = round(sway_y, 3)
        accel_z = round(base_z + sway_z, 3)

        return {
            "heart_rate_bpm": round(heart_rate, 1),
            "temperature_c": round(temperature, 2),
            "accel_x_g": accel_x,
            "accel_y_g": accel_y,
            "accel_z_g": accel_z,
        }

    def close(self) -> None:
        pass


# ---------------------------------------------------------------------------
# Real I2C sensor reader
# ---------------------------------------------------------------------------
class I2CSensors:
    """Reads LilyPad Arduino sensors over the I2C bus using smbus2.

    I2C Protocol:
      - HR sensor:     read 2 bytes from register 0x00 -> BPM
      - Temp sensor:   read 2 bytes from register 0x00 -> °C (0.0625 °C/LSB, signed)
      - Accelerometer: read 6 bytes from register 0x02 ->
                       X low, X high, Y low, Y high, Z low, Z high (signed 16-bit each)
    """

    def __init__(self, bus: int, hr_addr: int, temp_addr: int, accel_addr: int) -> None:
        import smbus2

        self._bus = smbus2.SMBus(bus)
        self._hr_addr = hr_addr
        self._temp_addr = temp_addr
        self._accel_addr = accel_addr
        # Accelerometer sensitivity for ±2G range (typical for LilyPad)
        # 16384 LSB/G for default ±2G range
        self._accel_scale = 1.0 / 16384.0

        logger.info(
            "I2C sensors configured: HR=0x%02X, Temp=0x%02X, Accel=0x%02X on bus %d",
            hr_addr, temp_addr, accel_addr, bus,
        )

    @staticmethod
    def _read_i2c_word(bus: Any, addr: int, register: int) -> int:
        """Read 2 bytes from *register* and return as a signed 16-bit integer."""
        data = bus.read_i2c_block_data(addr, register, 2)
        # Big-endian (MSB first, as is standard for most I2C sensors)
        raw = (data[0] << 8) | data[1]
        # Convert to signed 16-bit
        if raw >= 0x8000:
            raw -= 0x10000
        return raw

    @staticmethod
    def _read_i2c_block(bus: Any, addr: int, register: int, count: int) -> bytes:
        """Read *count* bytes starting from *register*."""
        return bytes(bus.read_i2c_block_data(addr, register, count))

    def read_all(self) -> Dict[str, float]:
        """Read all sensors and return a dict with current values.

        Raises OSError or IOError on I2C communication failures.
        """
        # -- Heart rate --
        hr_raw = self._read_i2c_word(self._bus, self._hr_addr, 0x00)
        heart_rate = float(max(0, hr_raw))  # BPM should be non-negative

        # -- Temperature --
        temp_raw = self._read_i2c_word(self._bus, self._temp_addr, 0x00)
        # 0.0625 °C per LSB, signed
        temperature = temp_raw * 0.0625

        # -- Accelerometer (read 6 bytes from register 0x02) --
        accel_data = self._read_i2c_block(self._bus, self._accel_addr, 0x02, 6)
        # X: bytes 0-1, Y: bytes 2-3, Z: bytes 4-5 (big-endian, signed)
        x_raw = (accel_data[0] << 8) | accel_data[1]
        y_raw = (accel_data[2] << 8) | accel_data[3]
        z_raw = (accel_data[4] << 8) | accel_data[5]
        # Convert to signed 16-bit
        for val in (x_raw, y_raw, z_raw):
            if val >= 0x8000:
                val -= 0x10000
        # Actually we need to re-assign properly
        x_raw = x_raw if x_raw < 0x8000 else x_raw - 0x10000
        y_raw = y_raw if y_raw < 0x8000 else y_raw - 0x10000
        z_raw = z_raw if z_raw < 0x8000 else z_raw - 0x10000

        accel_x = round(x_raw * self._accel_scale, 3)
        accel_y = round(y_raw * self._accel_scale, 3)
        accel_z = round(z_raw * self._accel_scale, 3)

        return {
            "heart_rate_bpm": round(heart_rate, 1),
            "temperature_c": round(temperature, 2),
            "accel_x_g": accel_x,
            "accel_y_g": accel_y,
            "accel_z_g": accel_z,
        }

    def close(self) -> None:
        """Close the I2C bus."""
        with suppress(Exception):
            self._bus.close()
            logger.info("I2C bus closed.")


# ---------------------------------------------------------------------------
# Sensor reader thread
# ---------------------------------------------------------------------------
def sensor_reader(
    sensor_source: Any,
    readings: SensorReadings,
    stop_event: threading.Event,
    update_interval: float = 1.0,
) -> None:
    """Periodically read sensors and update *readings*.

    Args:
        sensor_source: Object with a ``read_all()`` method and ``close()`` method.
        readings: Thread-safe container to update.
        stop_event: Set to signal shutdown.
        update_interval: Seconds between sensor reads.
    """
    while not stop_event.is_set():
        try:
            data = sensor_source.read_all()
            readings.update(
                heart_rate_bpm=data["heart_rate_bpm"],
                temperature_c=data["temperature_c"],
                accel_x_g=data["accel_x_g"],
                accel_y_g=data["accel_y_g"],
                accel_z_g=data["accel_z_g"],
            )
            logger.debug(
                "Sensors: HR=%.1f bpm, Temp=%.1f°C, Accel=(%.2f, %.2f, %.2f) G",
                data["heart_rate_bpm"],
                data["temperature_c"],
                data["accel_x_g"],
                data["accel_y_g"],
                data["accel_z_g"],
            )
        except (OSError, IOError) as exc:
            logger.error("I2C communication error: %s", exc)
            # Mark readings as stale but keep last values
            with readings._lock:
                readings.timestamp = datetime.now(timezone.utc)
                readings.valid = False
        except Exception:
            logger.exception("Unexpected error in sensor reader")
            with readings._lock:
                readings.valid = False

        # Wait for next interval (check stop_event periodically)
        for _ in range(int(update_interval * 10)):
            if stop_event.is_set():
                return
            time.sleep(0.1)


# ---------------------------------------------------------------------------
# HTTP API Handler
# ---------------------------------------------------------------------------
class SensorAPIHandler(BaseHTTPRequestHandler):
    """Serves the current sensor readings as JSON."""

    # Class-level reference set by the server
    readings: SensorReadings = None  # type: ignore[assignment]

    def do_GET(self) -> None:
        if self.path == "/sensors":
            self._json_response(self.readings.snapshot())
        elif self.path == "/sensors/health":
            self._json_response({"status": "ok", "service": "sensor_daemon"})
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
# Main daemon
# ---------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(description="Dog Agent Sensor Daemon")
    parser.add_argument(
        "--config",
        default=None,
        help="Path to config.yaml (default: ../config.yaml relative to this script)",
    )
    parser.add_argument(
        "--simulate",
        action="store_true",
        help="Simulate mode — generate fake sensor data (no I2C hardware needed)",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=1.0,
        help="Sensor read interval in seconds (default: 1.0)",
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
        cfg = {
            "enabled": True,
            "i2c_bus": 1,
            "hr_sensor_addr": 0x57,
            "temp_sensor_addr": 0x48,
            "accel_addr": 0x18,
            "api_port": 9110,
        }

    if not cfg.get("enabled", True) and not args.simulate:
        logger.info("Sensor module is disabled in config. Exiting.")
        return

    # Shared state
    readings = SensorReadings()
    stop_event = threading.Event()

    # Open sensor source
    if args.simulate:
        logger.info("SIMULATE MODE — using fake sensor data")
        sensor_source = SimulatedSensors()
    else:
        logger.info(
            "Opening I2C bus %d with HR=0x%02X, Temp=0x%02X, Accel=0x%02X",
            cfg["i2c_bus"],
            cfg["hr_sensor_addr"],
            cfg["temp_sensor_addr"],
            cfg["accel_addr"],
        )
        try:
            sensor_source = I2CSensors(
                bus=cfg["i2c_bus"],
                hr_addr=cfg["hr_sensor_addr"],
                temp_addr=cfg["temp_sensor_addr"],
                accel_addr=cfg["accel_addr"],
            )
        except ImportError:
            logger.error(
                "smbus2 is not installed. Install with: pip install smbus2\n"
                "Use --simulate mode for development without hardware."
            )
            sys.exit(1)
        except Exception as e:
            logger.error("Failed to open I2C bus: %s", e)
            logger.error(
                "Use --simulate mode for development without hardware."
            )
            sys.exit(1)

    # Start sensor reader thread
    reader_thread = threading.Thread(
        target=sensor_reader,
        args=(sensor_source, readings, stop_event, args.interval),
        name="sensor-reader",
        daemon=True,
    )
    reader_thread.start()
    logger.info("Sensor reader thread started (interval: %.1f sec)", args.interval)

    # Start HTTP API server
    SensorAPIHandler.readings = readings
    api_port = cfg["api_port"]
    server = HTTPServer(("127.0.0.1", api_port), SensorAPIHandler)

    # We need to avoid port conflict with the GPS daemon on the same port.
    # GPS daemon uses /gps, we use /sensors — they can coexist on the same port
    # if run in the same process, but as separate daemons they need separate ports.
    # Use the configured api_port; if GPS daemon is already running there,
    # this will fail. The user should run them on different ports or in one
    # process. We'll log a warning if binding fails.
    try:
        server_thread = threading.Thread(
            target=server.serve_forever,
            name="sensor-api",
            daemon=True,
        )
        server_thread.start()
        logger.info(
            "Sensor API server listening on http://127.0.0.1:%d/sensors",
            api_port,
        )
    except OSError as e:
        logger.error("Failed to start HTTP server on port %d: %s", api_port, e)
        logger.error(
            "Port %d may be in use by gps_daemon. Try running on a different port, "
            "or combine both daemons in a single process.",
            api_port,
        )
        stop_event.set()
        sensor_source.close()
        sys.exit(1)

    # ------------------------------------------------------------------
    # Graceful shutdown
    # ------------------------------------------------------------------
    def shutdown(signum: int, frame: Any) -> None:
        logger.info("Received signal %d — shutting down...", signum)
        stop_event.set()
        server.shutdown()
        sensor_source.close()
        logger.info("Sensor daemon stopped.")
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