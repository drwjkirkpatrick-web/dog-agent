#!/usr/bin/env python3
"""
Emergency BLE Beacon Module — Dog Agent
========================================

Backup tracking system that activates if the main Pi fails completely.
Uses an nRF52840/nRF51822 module powered by a coin cell battery.

This module provides:
1. Hardware interface to nRF52840/nRF51822 via UART/I2C/SPI
2. Heartbeat monitoring from Pi via GPIO
3. BLE advertisement formatting for standard BLE scanners
4. Last known GPS coordinates storage before Pi failure
5. HTTP API for status and testing (when Pi is up)
6. Simulation mode for testing without hardware

Hardware Setup
--------------
- nRF52840 or nRF51822 module
- CR2032 coin cell battery (3V, 225mAh)
- Connection to Pi via:
  * UART (TX/RX) for command interface
  * GPIO for heartbeat detection
  * I2C for configuration

Power Consumption
-----------------
- Standby (listening for heartbeat): ~5µA
- BLE advertising (10s interval): ~10µA average
- Estimated battery life: 6+ months on CR2032

BLE Advertisement Format
------------------------
The beacon broadcasts a standard BLE advertisement packet:
- UUID: "dog-agent-emergency" (configurable)
- TX Power: -4 dBm (configurable, up to +4 dBm)
- Major: Last known latitude (encoded)
- Minor: Last known longitude (encoded)
- RSSI: Varies with distance (~100m max range at -4dBm)

Emergency UUID Format
---------------------
Standard BLE UUID format for emergency beacons:
- UUID: 0xDA01 (Dog Agent emergency service UUID)
- Data format: Encoded GPS coordinates + battery level
- Compatible with standard BLE scanner apps

Heartbeat Monitoring
--------------------
- Pi sends regular heartbeat signal on configured GPIO
- nRF module monitors this pin
- If no heartbeat for >60 seconds, beacon activates
- Beacon broadcasts every 10 seconds until Pi recovers

HTTP API (Port 9129)
--------------------
  GET  /beacon/status   — Current state, battery level, last heartbeat
  POST /beacon/test     — Trigger test broadcast
  GET  /beacon/health   — Module health status
  POST /beacon/config   — Update beacon configuration
  GET  /beacon/location — Last known GPS coordinates

Configuration (config.yaml)
-----------------------------
  emergency_beacon:
    enabled: true
    gpio_pin: 17              # GPIO pin for heartbeat from Pi
    uart_port: "/dev/ttyAMA1" # UART port for nRF communication
    uart_baudrate: 115200
    uuid: "dog-agent-emergency"
    tx_power: -4              # dBm: -40, -20, -16, -12, -8, -4, 0, +4
    broadcast_interval_ms: 10000  # 10 seconds
    heartbeat_timeout_sec: 60
    location_file: "data/last_known_location.json"
    api_port: 9129

Usage:
    python src/emergency_ble_beacon.py              # Normal mode
    python src/emergency_ble_beacon.py --simulate   # Simulation mode
    python src/emergency_ble_beacon.py --config /path/to/config.yaml
    python src/emergency_ble_beacon.py --port 9129

Dependencies:
    - pyserial (for UART communication)
    - smbus2 or adafruit-circuitpython-busdevice (for I2C)
    - RPi.GPIO (for GPIO heartbeat detection, Pi only)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import signal
import struct
import sys
import threading
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum, auto
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

import yaml

# Optional imports for hardware
try:
    import serial
    SERIAL_AVAILABLE = True
except ImportError:
    SERIAL_AVAILABLE = False

try:
    import RPi.GPIO as GPIO
    GPIO_AVAILABLE = True
except ImportError:
    GPIO_AVAILABLE = False

try:
    from smbus2 import SMBus
    SMBUS_AVAILABLE = True
except ImportError:
    SMBUS_AVAILABLE = False


# -----------------------------------------------------------------------------
# Logging
# -----------------------------------------------------------------------------
logger = logging.getLogger("emergency_beacon")
_handler = logging.StreamHandler(sys.stdout)
_handler.setFormatter(logging.Formatter(
    "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
))
logger.addHandler(_handler)
logger.setLevel(logging.INFO)


# -----------------------------------------------------------------------------
# Types and Enums
# -----------------------------------------------------------------------------
class BeaconState(Enum):
    """Current state of the emergency beacon."""
    STANDBY = "standby"          # Normal operation, monitoring heartbeat
    ACTIVE = "active"            # Broadcasting emergency beacon
    TESTING = "testing"          # Test broadcast mode
    ERROR = "error"              # Hardware error
    DISABLED = "disabled"        # Module disabled


class TXPower(Enum):
    """nRF52/51 TX power levels in dBm."""
    MINUS_40 = -40
    MINUS_20 = -20
    MINUS_16 = -16
    MINUS_12 = -12
    MINUS_8 = -8
    MINUS_4 = -4
    ZERO = 0
    PLUS_4 = 4


@dataclass
class GPSCoordinates:
    """GPS coordinates with timestamp."""
    lat: float
    lon: float
    timestamp: Optional[datetime] = None
    accuracy_m: Optional[float] = None
    source: str = "unknown"  # gps, manual, estimated
    
    def __post_init__(self):
        if self.timestamp is None:
            self.timestamp = datetime.now(timezone.utc)
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "lat": self.lat,
            "lon": self.lon,
            "timestamp": self.timestamp.isoformat() if self.timestamp else None,
            "accuracy_m": self.accuracy_m,
            "source": self.source,
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "GPSCoordinates":
        ts_str = data.get("timestamp")
        timestamp = datetime.fromisoformat(ts_str) if ts_str else None
        return cls(
            lat=data.get("lat", 0.0),
            lon=data.get("lon", 0.0),
            timestamp=timestamp,
            accuracy_m=data.get("accuracy_m"),
            source=data.get("source", "unknown"),
        )


@dataclass
class BeaconStatus:
    """Current beacon status."""
    state: BeaconState
    battery_level_percent: float
    last_heartbeat: Optional[datetime]
    last_gps_coordinates: Optional[GPSCoordinates]
    uptime_sec: float
    broadcasts_sent: int
    heartbeat_missed_count: int
    hardware_connected: bool
    error_message: Optional[str] = None
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "state": self.state.value,
            "battery_level_percent": self.battery_level_percent,
            "last_heartbeat": self.last_heartbeat.isoformat() if self.last_heartbeat else None,
            "last_gps_coordinates": self.last_gps_coordinates.to_dict() if self.last_gps_coordinates else None,
            "uptime_sec": self.uptime_sec,
            "broadcasts_sent": self.broadcasts_sent,
            "heartbeat_missed_count": self.heartbeat_missed_count,
            "hardware_connected": self.hardware_connected,
            "error_message": self.error_message,
        }


@dataclass
class BeaconConfig:
    """Beacon configuration."""
    enabled: bool = True
    gpio_pin: int = 17
    uart_port: str = "/dev/ttyAMA1"
    uart_baudrate: int = 115200
    i2c_bus: int = 1
    i2c_address: int = 0x50  # nRF default
    uuid: str = "dog-agent-emergency"
    tx_power: int = -4  # dBm
    broadcast_interval_ms: int = 10000
    heartbeat_timeout_sec: int = 60
    location_file: str = "data/last_known_location.json"
    api_port: int = 9129
    simulate: bool = False
    
    @classmethod
    def from_yaml(cls, config_path: Path) -> "BeaconConfig":
        """Load configuration from YAML file."""
        with open(config_path) as f:
            data = yaml.safe_load(f)
        
        beacon_data = data.get("emergency_beacon", {})
        
        return cls(
            enabled=beacon_data.get("enabled", True),
            gpio_pin=beacon_data.get("gpio_pin", 17),
            uart_port=beacon_data.get("uart_port", "/dev/ttyAMA1"),
            uart_baudrate=beacon_data.get("uart_baudrate", 115200),
            i2c_bus=beacon_data.get("i2c_bus", 1),
            i2c_address=beacon_data.get("i2c_address", 0x50),
            uuid=beacon_data.get("uuid", "dog-agent-emergency"),
            tx_power=beacon_data.get("tx_power", -4),
            broadcast_interval_ms=beacon_data.get("broadcast_interval_ms", 10000),
            heartbeat_timeout_sec=beacon_data.get("heartbeat_timeout_sec", 60),
            location_file=beacon_data.get("location_file", "data/last_known_location.json"),
            api_port=beacon_data.get("api_port", 9129),
            simulate=beacon_data.get("simulate", False),
        )


# -----------------------------------------------------------------------------
# nRF Hardware Interface
# -----------------------------------------------------------------------------
class NRFInterface:
    """
    Interface to nRF52840/nRF51822 BLE module.
    
    Supports communication via:
    - UART (command mode)
    - I2C (configuration)
    - SPI (alternative interface)
    
    Commands (UART):
    - AT+BEACON=START,<uuid>,<major>,<minor>,<tx_power>,<interval>
    - AT+BEACON=STOP
    - AT+STATUS
    - AT+BATTERY
    - AT+SLEEP
    - AT+WAKE
    """
    
    # nRF52/51 command responses
    RESPONSE_OK = "OK"
    RESPONSE_ERROR = "ERROR"
    RESPONSE_READY = "READY"
    
    def __init__(self, config: BeaconConfig):
        self.config = config
        self.serial: Optional[Any] = None
        self.i2c: Optional[Any] = None
        self._lock = threading.Lock()
        self._connected = False
        
    def connect(self) -> bool:
        """Connect to nRF module."""
        if self.config.simulate:
            logger.info("[SIMULATE] nRF interface initialized")
            self._connected = True
            return True
        
        # Try UART first
        if SERIAL_AVAILABLE:
            try:
                self.serial = serial.Serial(
                    port=self.config.uart_port,
                    baudrate=self.config.uart_baudrate,
                    timeout=1,
                )
                logger.info(f"Connected to nRF via UART: {self.config.uart_port}")
                self._connected = True
                
                # Test connection
                if self._send_command("AT") == self.RESPONSE_OK:
                    logger.info("nRF module ready")
                    return True
                else:
                    logger.warning("nRF not responding to AT command")
                    
            except serial.SerialException as e:
                logger.warning(f"UART connection failed: {e}")
        
        # Try I2C as fallback
        if SMBUS_AVAILABLE and not self._connected:
            try:
                self.i2c = SMBus(self.config.i2c_bus)
                # Test read from nRF
                self.i2c.read_byte(self.config.i2c_address)
                logger.info(f"Connected to nRF via I2C: bus {self.config.i2c_bus}, addr 0x{self.config.i2c_address:02X}")
                self._connected = True
                return True
            except Exception as e:
                logger.warning(f"I2C connection failed: {e}")
        
        if not self._connected:
            logger.error("Failed to connect to nRF module via UART or I2C")
        
        return self._connected
    
    def disconnect(self):
        """Disconnect from nRF module."""
        if self.serial:
            self.serial.close()
            self.serial = None
        if self.i2c:
            self.i2c.close()
            self.i2c = None
        self._connected = False
    
    def _send_command(self, cmd: str, timeout: float = 1.0) -> str:
        """Send command to nRF module."""
        if self.config.simulate:
            return self._simulate_command(cmd)
        
        if not self.serial:
            return self.RESPONSE_ERROR
        
        with self._lock:
            self.serial.write(f"{cmd}\r\n".encode())
            self.serial.flush()
            
            # Wait for response
            response = []
            start = time.time()
            while time.time() - start < timeout:
                if self.serial.in_waiting:
                    line = self.serial.readline().decode().strip()
                    if line:
                        response.append(line)
                        if line in [self.RESPONSE_OK, self.RESPONSE_ERROR]:
                            break
                time.sleep(0.01)
            
            return response[-1] if response else self.RESPONSE_ERROR
    
    def _simulate_command(self, cmd: str) -> str:
        """Simulate nRF responses for testing."""
        if cmd == "AT":
            return self.RESPONSE_OK
        elif cmd.startswith("AT+BEACON=START"):
            return self.RESPONSE_OK
        elif cmd == "AT+BEACON=STOP":
            return self.RESPONSE_OK
        elif cmd == "AT+STATUS":
            return f"STATE:STANDBY,BAT:95,{self.RESPONSE_OK}"
        elif cmd == "AT+BATTERY":
            return "BAT:95," + self.RESPONSE_OK
        elif cmd == "AT+SLEEP":
            return self.RESPONSE_OK
        elif cmd == "AT+WAKE":
            return self.RESPONSE_OK
        else:
            return self.RESPONSE_ERROR
    
    def start_beacon(
        self,
        uuid: str,
        major: int,
        minor: int,
        tx_power: int,
        interval_ms: int,
    ) -> bool:
        """Start BLE beacon broadcasting."""
        # Encode UUID to 16-bit major/minor if needed
        major = max(0, min(65535, major))
        minor = max(0, min(65535, minor))
        
        cmd = f"AT+BEACON=START,{uuid},{major},{minor},{tx_power},{interval_ms}"
        response = self._send_command(cmd)
        success = response == self.RESPONSE_OK
        
        if success:
            logger.info(f"Beacon started: UUID={uuid}, Major={major}, Minor={minor}, TX={tx_power}dBm")
        else:
            logger.error(f"Failed to start beacon: {response}")
        
        return success
    
    def stop_beacon(self) -> bool:
        """Stop BLE beacon broadcasting."""
        response = self._send_command("AT+BEACON=STOP")
        success = response == self.RESPONSE_OK
        
        if success:
            logger.info("Beacon stopped")
        
        return success
    
    def get_battery_level(self) -> Optional[float]:
        """Get battery level from nRF module (percentage)."""
        response = self._send_command("AT+BATTERY")
        
        if self.config.simulate:
            # Simulate battery drain over time
            return 95.0 - (time.time() % 86400) / 86400 * 5  # ~5% per day
        
        # Parse response: "BAT:95,OK"
        if "BAT:" in response:
            try:
                bat_str = response.split("BAT:")[1].split(",")[0]
                return float(bat_str)
            except (IndexError, ValueError):
                pass
        
        return None
    
    def get_status(self) -> Dict[str, Any]:
        """Get nRF module status."""
        response = self._send_command("AT+STATUS")
        
        if self.config.simulate:
            return {
                "state": "STANDBY",
                "battery_percent": 95.0,
                "temperature_c": 25.0,
                " broadcasts_sent": 0,
            }
        
        # Parse response: "STATE:STANDBY,BAT:95,TEMP:25,OK"
        status = {}
        if "STATE:" in response:
            for part in response.split(","):
                if ":" in part:
                    key, value = part.split(":", 1)
                    status[key.lower()] = value
        
        return status
    
    def sleep(self) -> bool:
        """Put nRF module in sleep mode."""
        response = self._send_command("AT+SLEEP")
        return response == self.RESPONSE_OK
    
    def wake(self) -> bool:
        """Wake nRF module from sleep."""
        response = self._send_command("AT+WAKE")
        return response == self.RESPONSE_OK


# -----------------------------------------------------------------------------
# Heartbeat Monitor
# -----------------------------------------------------------------------------
class HeartbeatMonitor:
    """
    Monitors Pi heartbeat via GPIO pin.
    
    The Pi should toggle this pin regularly (e.g., every 5 seconds).
    If no toggle is detected for heartbeat_timeout_sec, beacon activates.
    """
    
    def __init__(self, config: BeaconConfig):
        self.config = config
        self._last_beat: Optional[datetime] = None
        self._missed_count = 0
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._simulate_toggle = False
        
    def start(self) -> bool:
        """Start heartbeat monitoring."""
        if self._running:
            return True
        
        if not self.config.simulate and not GPIO_AVAILABLE:
            logger.warning("RPi.GPIO not available, using simulated heartbeat")
            self.config.simulate = True
        
        if not self.config.simulate:
            try:
                GPIO.setmode(GPIO.BCM)
                GPIO.setup(self.config.gpio_pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)
                
                # Add edge detection
                GPIO.add_event_detect(
                    self.config.gpio_pin,
                    GPIO.BOTH,
                    callback=self._on_heartbeat,
                    bouncetime=100,
                )
                logger.info(f"Heartbeat monitoring started on GPIO {self.config.gpio_pin}")
                
            except Exception as e:
                logger.error(f"Failed to setup GPIO: {e}")
                self.config.simulate = True
        
        self._running = True
        self._last_beat = datetime.now(timezone.utc)
        
        # Start monitoring thread
        self._thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self._thread.start()
        
        return True
    
    def stop(self):
        """Stop heartbeat monitoring."""
        self._running = False
        
        if self._thread:
            self._thread.join(timeout=2)
        
        if not self.config.simulate and GPIO_AVAILABLE:
            try:
                GPIO.remove_event_detect(self.config.gpio_pin)
                GPIO.cleanup(self.config.gpio_pin)
            except:
                pass
        
        logger.info("Heartbeat monitoring stopped")
    
    def _on_heartbeat(self, channel):
        """Called when heartbeat signal detected."""
        with self._lock:
            self._last_beat = datetime.now(timezone.utc)
            self._missed_count = 0
    
    def _monitor_loop(self):
        """Main monitoring loop."""
        while self._running:
            time.sleep(1)
            
            with self._lock:
                if self._last_beat is None:
                    continue
                
                elapsed = (datetime.now(timezone.utc) - self._last_beat).total_seconds()
                
                if elapsed > self.config.heartbeat_timeout_sec:
                    self._missed_count += 1
                    logger.warning(f"Heartbeat timeout! Elapsed: {elapsed:.1f}s")
    
    def get_status(self) -> Tuple[bool, Optional[datetime], int]:
        """
        Get heartbeat status.
        
        Returns:
            (is_alive, last_beat_time, missed_count)
        """
        with self._lock:
            if self._last_beat is None:
                return False, None, self._missed_count
            
            elapsed = (datetime.now(timezone.utc) - self._last_beat).total_seconds()
            is_alive = elapsed < self.config.heartbeat_timeout_sec
            
            return is_alive, self._last_beat, self._missed_count
    
    def simulate_heartbeat(self):
        """Simulate a heartbeat (for testing)."""
        with self._lock:
            self._last_beat = datetime.now(timezone.utc)
            self._missed_count = 0


# -----------------------------------------------------------------------------
# Location Storage
# -----------------------------------------------------------------------------
class LocationStorage:
    """
    Stores and retrieves last known GPS coordinates.
    
    The nRF module has limited storage, so we keep coordinates on the Pi
    and sync them to the nRF periodically. If Pi dies, nRF uses last synced
    coordinates.
    """
    
    def __init__(self, config: BeaconConfig):
        self.config = config
        self._location: Optional[GPSCoordinates] = None
        self._lock = threading.Lock()
        self._storage_path = Path(self.config.location_file)
        
        # Ensure directory exists
        self._storage_path.parent.mkdir(parents=True, exist_ok=True)
        
        # Load existing location
        self._load()
    
    def _load(self):
        """Load location from file."""
        if self._storage_path.exists():
            try:
                with open(self._storage_path) as f:
                    data = json.load(f)
                    self._location = GPSCoordinates.from_dict(data)
                    logger.info(f"Loaded last location: {self._location.lat:.6f}, {self._location.lon:.6f}")
            except Exception as e:
                logger.warning(f"Failed to load location: {e}")
    
    def _save(self):
        """Save location to file."""
        if self._location:
            try:
                with open(self._storage_path, "w") as f:
                    json.dump(self._location.to_dict(), f, indent=2)
            except Exception as e:
                logger.error(f"Failed to save location: {e}")
    
    def update(self, lat: float, lon: float, accuracy_m: Optional[float] = None, source: str = "gps"):
        """Update last known location."""
        with self._lock:
            self._location = GPSCoordinates(
                lat=lat,
                lon=lon,
                accuracy_m=accuracy_m,
                source=source,
            )
            self._save()
        
        logger.debug(f"Location updated: {lat:.6f}, {lon:.6f}")
    
    def get(self) -> Optional[GPSCoordinates]:
        """Get last known location."""
        with self._lock:
            return self._location
    
    def encode_for_beacon(self) -> Tuple[int, int]:
        """
        Encode coordinates to major/minor values for BLE beacon.
        
        Uses a compact encoding:
        - Major: (latitude + 90) * 327.67  (0-65535)
        - Minor: (longitude + 180) * 182.04  (0-65535)
        
        This gives ~0.003 degree resolution (~300m at equator).
        """
        with self._lock:
            if self._location is None:
                return (0, 0)
            
            lat = max(-90, min(90, self._location.lat))
            lon = max(-180, min(180, self._location.lon))
            
            major = int((lat + 90) * 327.67) & 0xFFFF
            minor = int((lon + 180) * 182.04) & 0xFFFF
            
            return (major, minor)


# -----------------------------------------------------------------------------
# BLE Advertisement Formatter
# -----------------------------------------------------------------------------
class BLEAdvertisementFormatter:
    """
    Formats BLE advertisement packets according to standards.
    
    Supports:
    - iBeacon format (Apple standard)
    - Eddystone-UID (Google standard)
    - Custom format (Dog Agent specific)
    """
    
    # Dog Agent emergency UUID (128-bit)
    EMERGENCY_UUID = "DA01-DOG-AGENT-EMRG-BEACON00"
    
    # iBeacon prefix
    IBEACON_PREFIX = bytes([0x02, 0x01, 0x06, 0x1A, 0xFF, 0x4C, 0x00, 0x02, 0x15])
    
    @classmethod
    def create_ibeacon_packet(
        cls,
        uuid: str,
        major: int,
        minor: int,
        tx_power: int,
    ) -> bytes:
        """
        Create an iBeacon advertisement packet.
        
        Packet format:
        - Flags: 0x02 0x01 0x06
        - Manufacturer data: 0x1A 0xFF 0x4C 0x00 0x02 0x15
        - UUID: 16 bytes
        - Major: 2 bytes (big-endian)
        - Minor: 2 bytes (big-endian)
        - TX Power: 1 byte (signed)
        """
        # Convert UUID string to bytes (simplified - uses hash for variable length)
        uuid_bytes = cls._uuid_to_bytes(uuid)
        
        packet = bytearray()
        packet.extend(cls.IBEACON_PREFIX)
        packet.extend(uuid_bytes[:16])  # Take first 16 bytes
        packet.extend(struct.pack(">H", major & 0xFFFF))
        packet.extend(struct.pack(">H", minor & 0xFFFF))
        packet.append(struct.pack("b", tx_power)[0])
        
        return bytes(packet)
    
    @classmethod
    def create_custom_packet(
        cls,
        uuid: str,
        lat: float,
        lon: float,
        battery_percent: float,
        status: str,
    ) -> bytes:
        """
        Create a custom Dog Agent advertisement packet.
        
        Service Data format:
        - UUID: 16 bytes (service UUID)
        - Latitude: 4 bytes (float, scaled)
        - Longitude: 4 bytes (float, scaled)
        - Battery: 1 byte (percent)
        - Status: 1 byte (encoded)
        """
        # Scale coordinates for transmission
        lat_scaled = int(lat * 1000000)  # 6 decimal places
        lon_scaled = int(lon * 1000000)
        
        packet = bytearray()
        packet.extend(cls._uuid_to_bytes(uuid)[:16])
        packet.extend(struct.pack(">i", lat_scaled))
        packet.extend(struct.pack(">i", lon_scaled))
        packet.append(int(battery_percent) & 0xFF)
        packet.append(cls._encode_status(status))
        
        return bytes(packet)
    
    @classmethod
    def _uuid_to_bytes(cls, uuid: str) -> bytes:
        """Convert UUID string to bytes (handles various formats)."""
        # Remove dashes and convert to bytes
        clean = uuid.replace("-", "").replace(" ", "").lower()
        
        # If it's a short UUID, pad it
        if len(clean) < 32:
            clean = clean.ljust(32, "0")
        
        # Take first 32 hex chars
        clean = clean[:32]
        
        try:
            return bytes.fromhex(clean)
        except ValueError:
            # If not valid hex, hash it
            import hashlib
            return hashlib.md5(uuid.encode()).digest()
    
    @classmethod
    def _encode_status(cls, status: str) -> int:
        """Encode status string to byte."""
        statuses = {
            "standby": 0x00,
            "active": 0x01,
            "emergency": 0x02,
            "low_battery": 0x03,
            "error": 0xFF,
        }
        return statuses.get(status.lower(), 0x00)


# -----------------------------------------------------------------------------
# Emergency Beacon Module
# -----------------------------------------------------------------------------
class EmergencyBeaconModule:
    """
    Main emergency beacon module.
    
    Coordinates all components:
    - nRF hardware interface
    - Heartbeat monitoring
    - Location storage
    - BLE advertisement control
    - HTTP API
    """
    
    def __init__(self, config: BeaconConfig):
        self.config = config
        self.nrf: Optional[NRFInterface] = None
        self.heartbeat: Optional[HeartbeatMonitor] = None
        self.location_store: Optional[LocationStorage] = None
        self._status = BeaconStatus(
            state=BeaconState.STANDBY,
            battery_level_percent=100.0,
            last_heartbeat=None,
            last_gps_coordinates=None,
            uptime_sec=0.0,
            broadcasts_sent=0,
            heartbeat_missed_count=0,
            hardware_connected=False,
        )
        self._start_time = time.time()
        self._running = False
        self._main_thread: Optional[threading.Thread] = None
        self._api_server: Optional[HTTPServer] = None
        self._api_thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        
    def start(self) -> bool:
        """Start the emergency beacon module."""
        if self._running:
            return True
        
        if not self.config.enabled:
            logger.info("Emergency beacon disabled in configuration")
            self._status.state = BeaconState.DISABLED
            return False
        
        logger.info("Starting Emergency BLE Beacon Module...")
        
        # Initialize location storage
        self.location_store = LocationStorage(self.config)
        
        # Initialize nRF interface
        self.nrf = NRFInterface(self.config)
        if not self.nrf.connect():
            logger.error("Failed to connect to nRF module")
            self._status.hardware_connected = False
            # Continue in simulation mode if requested
            if not self.config.simulate:
                self._status.state = BeaconState.ERROR
                self._status.error_message = "nRF hardware not connected"
                return False
        else:
            self._status.hardware_connected = True
        
        # Initialize heartbeat monitoring
        self.heartbeat = HeartbeatMonitor(self.config)
        self.heartbeat.start()
        
        # Update initial status
        self._status.last_gps_coordinates = self.location_store.get()
        self._update_battery_level()
        
        # Start main loop
        self._running = True
        self._main_thread = threading.Thread(target=self._main_loop, daemon=True)
        self._main_thread.start()
        
        # Start HTTP API
        self._start_api()
        
        logger.info("Emergency beacon module started successfully")
        return True
    
    def stop(self):
        """Stop the emergency beacon module."""
        logger.info("Stopping Emergency BLE Beacon Module...")
        
        self._running = False
        
        # Stop API server
        if self._api_server:
            self._api_server.shutdown()
            self._api_server = None
        
        if self._api_thread:
            self._api_thread.join(timeout=2)
        
        # Stop main thread
        if self._main_thread:
            self._main_thread.join(timeout=2)
        
        # Stop heartbeat monitoring
        if self.heartbeat:
            self.heartbeat.stop()
        
        # Stop beacon if active
        if self.nrf:
            self.nrf.stop_beacon()
            self.nrf.disconnect()
        
        logger.info("Emergency beacon module stopped")
    
    def _main_loop(self):
        """Main control loop."""
        while self._running:
            try:
                self._update_status()
                self._check_heartbeat()
                self._update_battery_level()
                
                time.sleep(1)
                
            except Exception as e:
                logger.error(f"Error in main loop: {e}")
                time.sleep(5)
    
    def _update_status(self):
        """Update internal status."""
        with self._lock:
            self._status.uptime_sec = time.time() - self._start_time
            
            if self.heartbeat:
                _, last_beat, missed = self.heartbeat.get_status()
                self._status.last_heartbeat = last_beat
                self._status.heartbeat_missed_count = missed
    
    def _check_heartbeat(self):
        """Check heartbeat and activate beacon if needed."""
        if not self.heartbeat:
            return
        
        is_alive, _, _ = self.heartbeat.get_status()
        current_state = self._status.state
        
        if not is_alive and current_state != BeaconState.ACTIVE:
            # Heartbeat lost - activate beacon
            logger.warning("Pi heartbeat lost! Activating emergency beacon...")
            self._activate_beacon()
        
        elif is_alive and current_state == BeaconState.ACTIVE:
            # Heartbeat restored - deactivate beacon
            logger.info("Pi heartbeat restored. Deactivating emergency beacon.")
            self._deactivate_beacon()
    
    def _activate_beacon(self):
        """Activate emergency beacon broadcasting."""
        if not self.nrf:
            return
        
        with self._lock:
            self._status.state = BeaconState.ACTIVE
            
            # Get coordinates for beacon
            major, minor = 0, 0
            if self.location_store:
                major, minor = self.location_store.encode_for_beacon()
            
            # Start beacon
            success = self.nrf.start_beacon(
                uuid=self.config.uuid,
                major=major,
                minor=minor,
                tx_power=self.config.tx_power,
                interval_ms=self.config.broadcast_interval_ms,
            )
            
            if success:
                self._status.broadcasts_sent += 1
                logger.info(f"Emergency beacon activated: Major={major}, Minor={minor}")
            else:
                logger.error("Failed to activate beacon")
                self._status.state = BeaconState.ERROR
    
    def _deactivate_beacon(self):
        """Deactivate emergency beacon."""
        if not self.nrf:
            return
        
        with self._lock:
            if self.nrf.stop_beacon():
                self._status.state = BeaconState.STANDBY
                logger.info("Emergency beacon deactivated")
            else:
                logger.warning("Failed to stop beacon")
    
    def _update_battery_level(self):
        """Update battery level from nRF."""
        if self.nrf:
            level = self.nrf.get_battery_level()
            if level is not None:
                with self._lock:
                    self._status.battery_level_percent = level
    
    def test_beacon(self) -> bool:
        """Trigger a test broadcast."""
        if not self.nrf:
            return False
        
        with self._lock:
            old_state = self._status.state
            self._status.state = BeaconState.TESTING
            
            # Get coordinates
            major, minor = 0, 0
            if self.location_store:
                major, minor = self.location_store.encode_for_beacon()
            
            # Start beacon briefly
            success = self.nrf.start_beacon(
                uuid=self.config.uuid,
                major=major,
                minor=minor,
                tx_power=self.config.tx_power,
                interval_ms=1000,  # 1 second interval for test
            )
            
            if success:
                self._status.broadcasts_sent += 1
                logger.info("Test beacon activated for 10 seconds...")
                time.sleep(10)
                self.nrf.stop_beacon()
                logger.info("Test beacon stopped")
            
            self._status.state = old_state
            return success
    
    def update_location(self, lat: float, lon: float, accuracy_m: Optional[float] = None):
        """Update last known location."""
        if self.location_store:
            self.location_store.update(lat, lon, accuracy_m)
            self._status.last_gps_coordinates = self.location_store.get()
    
    def get_status(self) -> BeaconStatus:
        """Get current beacon status."""
        with self._lock:
            return BeaconStatus(
                state=self._status.state,
                battery_level_percent=self._status.battery_level_percent,
                last_heartbeat=self._status.last_heartbeat,
                last_gps_coordinates=self._status.last_gps_coordinates,
                uptime_sec=self._status.uptime_sec,
                broadcasts_sent=self._status.broadcasts_sent,
                heartbeat_missed_count=self._status.heartbeat_missed_count,
                hardware_connected=self._status.hardware_connected,
                error_message=self._status.error_message,
            )
    
    def _start_api(self):
        """Start HTTP API server."""
        handler = self._create_api_handler()
        
        try:
            self._api_server = HTTPServer(("0.0.0.0", self.config.api_port), handler)
            self._api_thread = threading.Thread(target=self._api_server.serve_forever, daemon=True)
            self._api_thread.start()
            logger.info(f"HTTP API listening on port {self.config.api_port}")
        except Exception as e:
            logger.error(f"Failed to start HTTP API: {e}")
    
    def _create_api_handler(self):
        """Create HTTP request handler."""
        module = self
        
        class BeaconAPIHandler(BaseHTTPRequestHandler):
            """HTTP API handler for beacon control."""
            
            def log_message(self, format, *args):
                logger.debug(f"API: {format % args}")
            
            def do_GET(self):
                """Handle GET requests."""
                path = self.path
                
                if path == "/beacon/status":
                    self._send_json(module.get_status().to_dict())
                
                elif path == "/beacon/health":
                    health = {
                        "module": "emergency_ble_beacon",
                        "healthy": module._status.state not in [BeaconState.ERROR, BeaconState.DISABLED],
                        "hardware_connected": module._status.hardware_connected,
                        "state": module._status.state.value,
                        "battery_percent": module._status.battery_level_percent,
                        "uptime_sec": module._status.uptime_sec,
                    }
                    self._send_json(health)
                
                elif path == "/beacon/location":
                    loc = module.location_store.get() if module.location_store else None
                    if loc:
                        self._send_json(loc.to_dict())
                    else:
                        self._send_error(404, "No location data available")
                
                else:
                    self._send_error(404, "Not found")
            
            def do_POST(self):
                """Handle POST requests."""
                path = self.path
                
                if path == "/beacon/test":
                    success = module.test_beacon()
                    if success:
                        self._send_json({"success": True, "message": "Test beacon activated for 10 seconds"})
                    else:
                        self._send_error(500, "Failed to activate test beacon")
                
                else:
                    self._send_error(404, "Not found")
            
            def _send_json(self, data: Dict):
                """Send JSON response."""
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(data, indent=2).encode())
            
            def _send_error(self, code: int, message: str):
                """Send error response."""
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"error": message}).encode())
        
        return BeaconAPIHandler


# -----------------------------------------------------------------------------
# Main Entry Point
# -----------------------------------------------------------------------------
def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(description="Emergency BLE Beacon Module for Dog Agent")
    parser.add_argument("--config", "-c", type=Path, default=Path("config.yaml"),
                        help="Path to configuration file")
    parser.add_argument("--port", "-p", type=int, default=None,
                        help="HTTP API port (overrides config)")
    parser.add_argument("--simulate", "-s", action="store_true",
                        help="Run in simulation mode")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Enable verbose logging")
    
    args = parser.parse_args()
    
    if args.verbose:
        logger.setLevel(logging.DEBUG)
    
    # Load configuration
    config_path = Path(args.config)
    if not config_path.exists():
        # Try relative to script
        config_path = Path(__file__).parent.parent / "config.yaml"
    
    if config_path.exists():
        logger.info(f"Loading configuration from {config_path}")
        config = BeaconConfig.from_yaml(config_path)
    else:
        logger.warning("No configuration file found, using defaults")
        config = BeaconConfig()
    
    # Override with command line arguments
    if args.simulate:
        config.simulate = True
    if args.port:
        config.api_port = args.port
    
    # Create and start module
    beacon = EmergencyBeaconModule(config)
    
    def signal_handler(signum, frame):
        """Handle shutdown signals."""
        logger.info(f"Received signal {signum}, shutting down...")
        beacon.stop()
        sys.exit(0)
    
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    # Start the module
    if beacon.start():
        logger.info("Emergency BLE Beacon Module running. Press Ctrl+C to stop.")
        
        # Keep main thread alive
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        finally:
            beacon.stop()
    else:
        logger.error("Failed to start emergency beacon module")
        sys.exit(1)


if __name__ == "__main__":
    main()
