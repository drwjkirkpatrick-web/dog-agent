#!/usr/bin/env python3
"""
LoRaWAN Backup Module — Dog Agent
==================================
Uses RFM95W LoRa module for off-grid tracking in areas without WiFi/cellular.

Features:
    - Hardware: RFM95W (868/915 MHz) + Pi
    - Range: 2-5km urban, 10km+ rural
    - Transmits GPS coordinates every 5 minutes when cellular/WiFi unavailable
    - Compact payload format (16 bytes: lat, lon, battery, status)
    - Can receive commands from gateway (check-in request)

LoRa Packet Format:
    - 16-byte payload for efficiency
    - Byte 0-3:   Latitude (IEEE 754 float, compressed to int32 * 1000000)
    - Byte 4-7:   Longitude (IEEE 754 float, compressed to int32 * 1000000)
    - Byte 8-9:   Battery level (0-100%, uint8)
    - Byte 10:    Status flags (bitfield)
    - Byte 11:    GPS fix quality (0-255)
    - Byte 12-13: Speed (knots * 10, uint16)
    - Byte 14-15: Reserved / CRC

HTTP API (Port 9140)
--------------------
    GET  /lora/status      — connection state, last transmission
    GET  /lora/health      — module health
    POST /lora/send        — manually send message
    GET  /lora/messages    — received messages
    POST /lora/config      — update configuration

Configuration (config.yaml)
---------------------------
    lorawan:
        enabled: true/false
        frequency_mhz: 915.0          # 868.0 for EU, 915.0 for US
        spreading_factor: 7           # 7-12 (higher = longer range, slower)
        tx_power_dbm: 20              # 5-23 dBm
        bandwidth_khz: 125            # 125/250 kHz
        coding_rate: 4/5              # 4/5, 4/6, 4/7, 4/8
        tx_interval_sec: 300          # 5 minutes default
        max_retries: 3
        gateway_id: "GATEWAY_001"

Hardware Connections (RFM95W)
-----------------------------
    RFM95W    Raspberry Pi
    ------    ------------
    VCC       3.3V (Pin 1 or 17)
    GND       GND (Pin 6, 9, 14, 20, 25, 30, 34, 39)
    MISO      GPIO 9 (Pin 21)
    MOSI      GPIO 10 (Pin 19)
    SCK       GPIO 11 (Pin 23)
    NSS       GPIO 8 (Pin 24) - CE0
    RST       GPIO 22 (Pin 15)
    DIO0      GPIO 4 (Pin 7) - Interrupt for RX done

Usage:
    python src/lorawan_backup.py              # Normal mode
    python src/lorawan_backup.py --simulate  # Simulation mode (no hardware)
    python src/lorawan_backup.py --port 9140
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import signal
import struct
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from enum import IntEnum
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import yaml

# ---------------------------------------------------------------------------
# Logging Setup
# ---------------------------------------------------------------------------
logger = logging.getLogger("lorawan_backup")
_handler = logging.StreamHandler(sys.stdout)
_handler.setFormatter(logging.Formatter(
    "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
))
logger.addHandler(_handler)
logger.setLevel(logging.INFO)

# ---------------------------------------------------------------------------
# Paths & Constants
# ---------------------------------------------------------------------------
PROJECT_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_DIR / "config.yaml"
DATA_DIR = PROJECT_DIR / "data"
LORA_LOG_FILE = DATA_DIR / "lorawan_messages.jsonl"

DEFAULT_PORT = 9140

# RFM95W SPI Registers
REG_FIFO = 0x00
REG_OP_MODE = 0x01
REG_FR_MSB = 0x06
REG_FR_MID = 0x07
REG_FR_LSB = 0x08
REG_PA_CONFIG = 0x09
REG_PA_RAMP = 0x0A
REG_LNA = 0x0C
REG_FIFO_ADDR_PTR = 0x0D
REG_FIFO_TX_BASE_ADDR = 0x0E
REG_FIFO_RX_BASE_ADDR = 0x0F
REG_FIFO_RX_CURRENT_ADDR = 0x10
REG_IRQ_FLAGS = 0x12
REG_RX_NB_BYTES = 0x13
REG_PKT_SNR_VALUE = 0x19
REG_PKT_RSSI_VALUE = 0x1A
REG_RSSI_VALUE = 0x1B
REG_MODEM_CONFIG_1 = 0x1D
REG_MODEM_CONFIG_2 = 0x1E
REG_PREAMBLE_MSB = 0x20
REG_PREAMBLE_LSB = 0x21
REG_PAYLOAD_LENGTH = 0x22
REG_MODEM_CONFIG_3 = 0x26
REG_FREQ_ERROR_MSB = 0x28
REG_FREQ_ERROR_MID = 0x29
REG_FREQ_ERROR_LSB = 0x2A
REG_RSSI_WIDEBAND = 0x2C
REG_DETECTION_OPTIMIZE = 0x31
REG_INVERT_IQ = 0x33
REG_DETECTION_THRESHOLD = 0x37
REG_SYNC_WORD = 0x39
REG_DIO_MAPPING_1 = 0x40
REG_VERSION = 0x42
REG_PA_DAC = 0x4D

# Operating modes
MODE_SLEEP = 0x00
MODE_STDBY = 0x01
MODE_TX = 0x03
MODE_RX_CONTINUOUS = 0x05
MODE_RX_SINGLE = 0x06

# IRQ masks
IRQ_TX_DONE_MASK = 0x08
IRQ_RX_DONE_MASK = 0x40
IRQ_PAYLOAD_CRC_ERROR_MASK = 0x20

# Default sync word for public LoRaWAN
LORAWAN_PUBLIC_SYNCWORD = 0x34

# ---------------------------------------------------------------------------
# Data Classes
# ---------------------------------------------------------------------------
@dataclass
class LoRaPacket:
    """Represents a LoRa packet payload."""
    lat: float = 0.0
    lon: float = 0.0
    battery_percent: int = 0
    status_flags: int = 0
    fix_quality: int = 0
    speed_knots: float = 0.0
    timestamp: Optional[datetime] = None
    rssi: Optional[int] = None
    snr: Optional[float] = None

    def to_bytes(self) -> bytes:
        """Pack to compact payload."""
        # Convert lat/lon to microdegrees for compact storage
        lat_micro = int(self.lat * 1_000_000)
        lon_micro = int(self.lon * 1_000_000)
        speed_dec = int(self.speed_knots * 10)
        
        # 14-byte payload: i i B B B B H = 4+4+1+1+1+1+2 = 14 bytes
        payload = struct.pack('>iiBBBBH',
            lat_micro,
            lon_micro,
            self.battery_percent & 0xFF,
            self.status_flags & 0xFF,
            self.fix_quality & 0xFF,
            speed_dec & 0xFF,           # Speed low byte
            (speed_dec >> 8) & 0xFF   # Speed high byte  
        )
        return payload

    @classmethod
    def from_bytes(cls, data: bytes) -> "LoRaPacket":
        """Unpack from payload."""
        if len(data) < 14:
            raise ValueError(f"Payload too short: {len(data)} bytes, need 14")
        
        # Unpack: 4 + 4 + 1 + 1 + 1 + 2 = 13? Let me recalculate
        # >iiBBBBH = 4 + 4 + 1 + 1 + 1 + 1 + 2 = 14 bytes
        lat_micro, lon_micro, battery, status, fix, speed_lo, speed_hi = struct.unpack('>iiBBBBH', data[:14])
        speed_dec = speed_lo | (speed_hi << 8)
        return cls(
            lat=lat_micro / 1_000_000,
            lon=lon_micro / 1_000_000,
            battery_percent=battery,
            status_flags=status,
            fix_quality=fix,
            speed_knots=speed_dec / 10,
            timestamp=datetime.now(timezone.utc)
        )


@dataclass
class LoRaStatus:
    """Current LoRaWAN module status."""
    connected: bool = False
    last_tx_time: Optional[datetime] = None
    last_rx_time: Optional[datetime] = None
    tx_count: int = 0
    rx_count: int = 0
    tx_fail_count: int = 0
    rssi: Optional[int] = None
    snr: Optional[float] = None
    frequency_mhz: float = 915.0
    spreading_factor: int = 7
    tx_power_dbm: int = 20
    simulation_mode: bool = False


@dataclass
class ReceivedMessage:
    """A received LoRa message."""
    timestamp: datetime
    payload: bytes
    payload_hex: str
    rssi: int
    snr: float
    frequency_error: int


class StatusFlags(IntEnum):
    """Status flag bit definitions."""
    GPS_VALID = 0x01
    WIFI_CONNECTED = 0x02
    CELLULAR_CONNECTED = 0x04
    EMERGENCY_MODE = 0x08
    LOW_BATTERY = 0x10
    MOVING = 0x20


# ---------------------------------------------------------------------------
# RFM95W Driver
# ---------------------------------------------------------------------------
class RFM95W:
    """RFM95W LoRa transceiver driver using SPI."""

    def __init__(
        self,
        spi_bus: int = 0,
        cs_pin: int = 0,
        rst_pin: int = 22,
        frequency_mhz: float = 915.0,
        spreading_factor: int = 7,
        bandwidth_khz: int = 125,
        tx_power_dbm: int = 20,
        coding_rate: str = "4/5",
        sync_word: int = LORAWAN_PUBLIC_SYNCWORD,
    ):
        self.spi_bus = spi_bus
        self.cs_pin = cs_pin
        self.rst_pin = rst_pin
        self.frequency_mhz = frequency_mhz
        self.spreading_factor = spreading_factor
        self.bandwidth_khz = bandwidth_khz
        self.tx_power_dbm = tx_power_dbm
        self.coding_rate = coding_rate
        self.sync_word = sync_word
        
        self._spi = None
        self._gpio = None
        self._initialized = False
        self._lock = threading.Lock()
        
        # Callbacks
        self.on_rx_done: Optional[Callable[[bytes, int, float], None]] = None

    def _init_spi(self) -> bool:
        """Initialize SPI interface."""
        try:
            import spidev
            self._spi = spidev.SpiDev()
            self._spi.open(self.spi_bus, self.cs_pin)
            self._spi.max_speed_hz = 5000000  # 5MHz
            self._spi.mode = 0
            return True
        except Exception as e:
            logger.error(f"Failed to initialize SPI: {e}")
            return False

    def _init_gpio(self) -> bool:
        """Initialize GPIO for reset and interrupts."""
        try:
            import RPi.GPIO as GPIO
            self._gpio = GPIO
            self._gpio.setmode(GPIO.BCM)
            self._gpio.setup(self.rst_pin, GPIO.OUT)
            return True
        except Exception as e:
            logger.error(f"Failed to initialize GPIO: {e}")
            return False

    def _reset(self) -> None:
        """Reset the RFM95W module."""
        if self._gpio:
            self._gpio.output(self.rst_pin, GPIO.LOW)
            time.sleep(0.01)
            self._gpio.output(self.rst_pin, GPIO.HIGH)
            time.sleep(0.01)

    def _spi_transfer(self, address: int, value: int = 0) -> int:
        """Transfer data via SPI."""
        if self._spi:
            result = self._spi.xfer2([address | 0x80 if value else address & 0x7F, value])
            return result[1] if len(result) > 1 else 0
        return 0

    def _read_register(self, address: int) -> int:
        """Read a register."""
        return self._spi_transfer(address & 0x7F)

    def _write_register(self, address: int, value: int) -> None:
        """Write to a register."""
        self._spi_transfer(address | 0x80, value)

    def _set_frequency(self, freq_mhz: float) -> None:
        """Set carrier frequency."""
        # FRF = freq * 2^19 / 32MHz
        frf = int(freq_mhz * 1_000_000.0 * 524288 / 32_000_000.0)
        self._write_register(REG_FR_MSB, (frf >> 16) & 0xFF)
        self._write_register(REG_FR_MID, (frf >> 8) & 0xFF)
        self._write_register(REG_FR_LSB, frf & 0xFF)

    def _set_tx_power(self, level: int) -> None:
        """Set TX power (5-23 dBm)."""
        if level > 20:
            self._write_register(REG_PA_DAC, 0x87)  # Enable +20dBm
            level = 20
        else:
            self._write_register(REG_PA_DAC, 0x84)  # Disable +20dBm
        
        self._write_register(REG_PA_CONFIG, 0x80 | (level - 5))

    def _set_spreading_factor(self, sf: int) -> None:
        """Set spreading factor (6-12)."""
        if sf < 6:
            sf = 6
        elif sf > 12:
            sf = 12
        
        # Set detection optimize and threshold based on SF
        if sf == 6:
            self._write_register(REG_DETECTION_OPTIMIZE, 0x05)
            self._write_register(REG_DETECTION_THRESHOLD, 0x0C)
        else:
            self._write_register(REG_DETECTION_OPTIMIZE, 0x03)
            self._write_register(REG_DETECTION_THRESHOLD, 0x0A)
        
        # Set SF in modem config 2
        mc2 = self._read_register(REG_MODEM_CONFIG_2)
        mc2 = (mc2 & 0x0F) | ((sf << 4) & 0xF0)
        self._write_register(REG_MODEM_CONFIG_2, mc2)

    def _set_bandwidth(self, bw_khz: int) -> None:
        """Set bandwidth (7.8, 10.4, 15.6, 20.8, 31.25, 41.7, 62.5, 125, 250 kHz)."""
        bw_map = {7.8: 0, 10.4: 1, 15.6: 2, 20.8: 3, 31.25: 4, 41.7: 5, 62.5: 6, 125: 7, 250: 8}
        bw_val = bw_map.get(bw_khz, 7)  # Default 125kHz
        
        mc1 = self._read_register(REG_MODEM_CONFIG_1)
        mc1 = (mc1 & 0x0F) | (bw_val << 4)
        self._write_register(REG_MODEM_CONFIG_1, mc1)

    def _set_coding_rate(self, cr: str) -> None:
        """Set coding rate (4/5, 4/6, 4/7, 4/8)."""
        cr_map = {"4/5": 1, "4/6": 2, "4/7": 3, "4/8": 4}
        cr_val = cr_map.get(cr, 1)
        
        mc1 = self._read_register(REG_MODEM_CONFIG_1)
        mc1 = (mc1 & 0xF1) | (cr_val << 1)
        self._write_register(REG_MODEM_CONFIG_1, mc1)

    def _set_preamble_length(self, length: int) -> None:
        """Set preamble length."""
        self._write_register(REG_PREAMBLE_MSB, (length >> 8) & 0xFF)
        self._write_register(REG_PREAMBLE_LSB, length & 0xFF)

    def _set_mode(self, mode: int) -> None:
        """Set operating mode."""
        self._write_register(REG_OP_MODE, mode)

    def _explicit_header_mode(self) -> None:
        """Set explicit header mode."""
        mc1 = self._read_register(REG_MODEM_CONFIG_1)
        self._write_register(REG_MODEM_CONFIG_1, mc1 & 0xFE)

    def _implicit_header_mode(self) -> None:
        """Set implicit header mode."""
        mc1 = self._read_register(REG_MODEM_CONFIG_1)
        self._write_register(REG_MODEM_CONFIG_1, mc1 | 0x01)

    def init(self) -> bool:
        """Initialize the RFM95W module."""
        with self._lock:
            if not self._init_spi():
                return False
            if not self._init_gpio():
                return False
            
            self._reset()
            
            # Check version
            version = self._read_register(REG_VERSION)
            if version != 0x12:
                logger.warning(f"Unexpected RFM95W version: 0x{version:02X} (expected 0x12)")
                # Continue anyway - might be clone or different revision
            
            # Put in sleep mode
            self._set_mode(MODE_SLEEP)
            time.sleep(0.01)
            
            # Set frequency
            self._set_frequency(self.frequency_mhz)
            
            # Set spreading factor
            self._set_spreading_factor(self.spreading_factor)
            
            # Set bandwidth
            self._set_bandwidth(self.bandwidth_khz)
            
            # Set coding rate
            self._set_coding_rate(self.coding_rate)
            
            # Set TX power
            self._set_tx_power(self.tx_power_dbm)
            
            # Set sync word
            self._write_register(REG_SYNC_WORD, self.sync_word)
            
            # Set preamble length
            self._set_preamble_length(8)
            
            # Set base addresses
            self._write_register(REG_FIFO_TX_BASE_ADDR, 0)
            self._write_register(REG_FIFO_RX_BASE_ADDR, 0)
            
            # Set LNA boost
            self._write_register(REG_LNA, 0x23)
            
            # Set auto AGC
            self._write_register(REG_MODEM_CONFIG_3, 0x04)
            
            # Standby mode
            self._set_mode(MODE_STDBY)
            
            self._initialized = True
            logger.info(f"RFM95W initialized: {self.frequency_mhz}MHz, SF{self.spreading_factor}")
            return True

    def send(self, data: bytes) -> bool:
        """Send data via LoRa."""
        with self._lock:
            if not self._initialized:
                return False
            
            try:
                # Standby mode
                self._set_mode(MODE_STDBY)
                
                # Clear IRQ flags
                self._write_register(REG_IRQ_FLAGS, 0xFF)
                
                # Set payload length
                self._write_register(REG_PAYLOAD_LENGTH, len(data))
                
                # Write data to FIFO
                self._write_register(REG_FIFO_ADDR_PTR, 0)
                for byte in data:
                    self._write_register(REG_FIFO, byte)
                
                # Start transmission
                self._set_mode(MODE_TX)
                
                # Wait for TX done (with timeout)
                timeout = time.time() + 5.0
                while time.time() < timeout:
                    irq = self._read_register(REG_IRQ_FLAGS)
                    if irq & IRQ_TX_DONE_MASK:
                        break
                    time.sleep(0.001)
                
                # Clear IRQ flags
                self._write_register(REG_IRQ_FLAGS, 0xFF)
                
                # Back to standby
                self._set_mode(MODE_STDBY)
                
                return True
            except Exception as e:
                logger.error(f"TX failed: {e}")
                return False

    def receive(self, timeout_ms: int = 1000) -> Optional[Tuple[bytes, int, float]]:
        """Receive data with timeout. Returns (data, rssi, snr)."""
        with self._lock:
            if not self._initialized:
                return None
            
            try:
                # Set RX single mode
                self._write_register(REG_IRQ_FLAGS, 0xFF)
                self._set_mode(MODE_RX_SINGLE)
                
                # Wait for RX done or timeout
                start = time.time()
                timeout_sec = timeout_ms / 1000.0
                
                while time.time() - start < timeout_sec:
                    irq = self._read_register(REG_IRQ_FLAGS)
                    if irq & IRQ_RX_DONE_MASK:
                        break
                    if irq & IRQ_PAYLOAD_CRC_ERROR_MASK:
                        self._write_register(REG_IRQ_FLAGS, 0xFF)
                        return None
                    time.sleep(0.001)
                else:
                    # Timeout
                    self._set_mode(MODE_STDBY)
                    return None
                
                # Get packet info
                rssi = self._read_register(REG_PKT_RSSI_VALUE) - 164  # Adjust for RFM95W
                snr_raw = self._read_register(REG_PKT_SNR_VALUE)
                snr = snr_raw / 4.0 if snr_raw & 0x80 == 0 else (snr_raw - 256) / 4.0
                
                # Read received data
                length = self._read_register(REG_RX_NB_BYTES)
                current_addr = self._read_register(REG_FIFO_RX_CURRENT_ADDR)
                self._write_register(REG_FIFO_ADDR_PTR, current_addr)
                
                data = bytearray()
                for _ in range(length):
                    data.append(self._read_register(REG_FIFO))
                
                # Clear IRQ flags
                self._write_register(REG_IRQ_FLAGS, 0xFF)
                
                # Back to standby
                self._set_mode(MODE_STDBY)
                
                return bytes(data), rssi, snr
            except Exception as e:
                logger.error(f"RX failed: {e}")
                return None

    def start_receive_continuous(self) -> None:
        """Start continuous receive mode."""
        with self._lock:
            if self._initialized:
                self._write_register(REG_IRQ_FLAGS, 0xFF)
                self._set_mode(MODE_RX_CONTINUOUS)

    def get_rssi(self) -> int:
        """Get current RSSI."""
        with self._lock:
            if self._initialized:
                return self._read_register(REG_RSSI_VALUE) - 164
            return -999

    def close(self) -> None:
        """Close SPI and cleanup."""
        with self._lock:
            if self._initialized:
                self._set_mode(MODE_SLEEP)
            if self._spi:
                self._spi.close()
                self._spi = None
            if self._gpio:
                self._gpio.cleanup()
                self._gpio = None
            self._initialized = False


# ---------------------------------------------------------------------------
# Simulated RFM95W
# ---------------------------------------------------------------------------
class SimulatedRFM95W(RFM95W):
    """Simulated RFM95W for testing without hardware."""

    def __init__(self, **kwargs):
        # Don't call parent init
        self.frequency_mhz = kwargs.get('frequency_mhz', 915.0)
        self.spreading_factor = kwargs.get('spreading_factor', 7)
        self.bandwidth_khz = kwargs.get('bandwidth_khz', 125)
        self.tx_power_dbm = kwargs.get('tx_power_dbm', 20)
        self.coding_rate = kwargs.get('coding_rate', '4/5')
        self.sync_word = kwargs.get('sync_word', LORAWAN_PUBLIC_SYNCWORD)
        self._initialized = False
        self._mode = MODE_STDBY
        self._sim_rssi = -75
        self._lock = threading.Lock()
        self.on_rx_done = None
        self._pending_rx: Optional[bytes] = None

    def _init_spi(self) -> bool:
        return True

    def _init_gpio(self) -> bool:
        return True

    def _reset(self) -> None:
        time.sleep(0.1)

    def _spi_transfer(self, address: int, value: int = 0) -> int:
        return 0x12 if address == REG_VERSION else 0

    def _read_register(self, address: int) -> int:
        return 0

    def _write_register(self, address: int, value: int) -> None:
        pass

    def _set_frequency(self, freq_mhz: float) -> None:
        self.frequency_mhz = freq_mhz

    def _set_tx_power(self, level: int) -> None:
        self.tx_power_dbm = level

    def _set_spreading_factor(self, sf: int) -> None:
        self.spreading_factor = sf

    def _set_bandwidth(self, bw_khz: int) -> None:
        self.bandwidth_khz = bw_khz

    def _set_coding_rate(self, cr: str) -> None:
        self.coding_rate = cr

    def _set_mode(self, mode: int) -> None:
        self._mode = mode

    def init(self) -> bool:
        """Simulated init - always succeeds."""
        with self._lock:
            self._initialized = True
            logger.info(f"[SIMULATED] RFM95W initialized: {self.frequency_mhz}MHz, SF{self.spreading_factor}")
            return True

    def send(self, data: bytes) -> bool:
        """Simulated send - always succeeds with random delay."""
        with self._lock:
            if not self._initialized:
                return False
            # Simulate transmission time
            air_time = self._calculate_air_time(len(data))
            time.sleep(air_time)
            logger.debug(f"[SIMULATED] Sent {len(data)} bytes")
            return True

    def receive(self, timeout_ms: int = 1000) -> Optional[Tuple[bytes, int, float]]:
        """Simulated receive - occasionally returns mock data."""
        with self._lock:
            if not self._initialized:
                return None
            
            # Occasionally simulate receiving a message
            if self._pending_rx:
                data = self._pending_rx
                self._pending_rx = None
                return data, self._sim_rssi, 8.0
            
            # Simulate timeout
            time.sleep(min(timeout_ms / 1000.0, 0.5))
            return None

    def _calculate_air_time(self, payload_len: int) -> float:
        """Calculate simulated air time based on LoRa parameters."""
        # Simplified air time calculation
        symbol_time = (2 ** self.spreading_factor) / (self.bandwidth_khz * 1000)
        payload_symbols = 8 + payload_len
        return payload_symbols * symbol_time

    def inject_received_message(self, data: bytes) -> None:
        """Inject a message for simulation testing."""
        with self._lock:
            self._pending_rx = data

    def start_receive_continuous(self) -> None:
        self._mode = MODE_RX_CONTINUOUS

    def get_rssi(self) -> int:
        return self._sim_rssi + (hash(time.time()) % 20 - 10)

    def close(self) -> None:
        self._initialized = False


# ---------------------------------------------------------------------------
# LoRaWAN Manager
# ---------------------------------------------------------------------------
class LoRaWANManager:
    """Manages LoRaWAN backup communication."""

    def __init__(self, config: Dict[str, Any], simulate: bool = False):
        self.config = config
        self.simulate = simulate
        self.enabled = config.get('enabled', True)
        
        # LoRa parameters
        self.frequency_mhz = config.get('frequency_mhz', 915.0)
        self.spreading_factor = config.get('spreading_factor', 7)
        self.bandwidth_khz = config.get('bandwidth_khz', 125)
        self.tx_power_dbm = config.get('tx_power_dbm', 20)
        self.coding_rate = config.get('coding_rate', '4/5')
        self.tx_interval_sec = config.get('tx_interval_sec', 300)  # 5 minutes
        self.max_retries = config.get('max_retries', 3)
        self.gateway_id = config.get('gateway_id', 'GATEWAY_001')
        
        # Initialize radio
        radio_class = SimulatedRFM95W if simulate else RFM95W
        self.radio = radio_class(
            frequency_mhz=self.frequency_mhz,
            spreading_factor=self.spreading_factor,
            bandwidth_khz=self.bandwidth_khz,
            tx_power_dbm=self.tx_power_dbm,
            coding_rate=self.coding_rate,
        )
        
        # State
        self.status = LoRaStatus(
            simulation_mode=simulate,
            frequency_mhz=self.frequency_mhz,
            spreading_factor=self.spreading_factor,
            tx_power_dbm=self.tx_power_dbm,
        )
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._last_gps: Optional[Dict[str, Any]] = None
        self._battery_percent = 100
        self._wifi_connected = False
        self._cellular_connected = False
        self._received_messages: deque = deque(maxlen=100)
        
        # Ensure data directory exists
        DATA_DIR.mkdir(parents=True, exist_ok=True)

    def init(self) -> bool:
        """Initialize the module."""
        if not self.enabled:
            logger.info("LoRaWAN backup module disabled in config")
            return False
        
        if self.radio.init():
            self.status.connected = True
            logger.info("LoRaWAN backup module initialized")
            return True
        else:
            logger.error("Failed to initialize LoRaWAN module")
            return False

    def update_gps(self, lat: float, lon: float, fix_quality: int = 0, speed: float = 0.0) -> None:
        """Update latest GPS position."""
        self._last_gps = {
            'lat': lat,
            'lon': lon,
            'fix_quality': fix_quality,
            'speed': speed,
            'timestamp': datetime.now(timezone.utc).isoformat(),
        }

    def update_battery(self, percent: int) -> None:
        """Update battery percentage."""
        self._battery_percent = max(0, min(100, percent))

    def update_connectivity(self, wifi: bool = False, cellular: bool = False) -> None:
        """Update connectivity status."""
        self._wifi_connected = wifi
        self._cellular_connected = cellular

    def _build_status_flags(self) -> int:
        """Build status flag byte."""
        flags = 0
        if self._last_gps and self._last_gps.get('fix_quality', 0) > 0:
            flags |= StatusFlags.GPS_VALID
        if self._wifi_connected:
            flags |= StatusFlags.WIFI_CONNECTED
        if self._cellular_connected:
            flags |= StatusFlags.CELLULAR_CONNECTED
        if self._battery_percent < 20:
            flags |= StatusFlags.LOW_BATTERY
        if self._last_gps and self._last_gps.get('speed', 0) > 1.0:
            flags |= StatusFlags.MOVING
        return flags

    def transmit_position(self) -> bool:
        """Transmit current position via LoRa."""
        if not self.status.connected:
            return False
        
        if not self._last_gps:
            logger.warning("No GPS data available for transmission")
            return False
        
        packet = LoRaPacket(
            lat=self._last_gps.get('lat', 0.0),
            lon=self._last_gps.get('lon', 0.0),
            battery_percent=self._battery_percent,
            status_flags=self._build_status_flags(),
            fix_quality=self._last_gps.get('fix_quality', 0),
            speed_knots=self._last_gps.get('speed', 0.0),
            timestamp=datetime.now(timezone.utc),
        )
        
        payload = packet.to_bytes()
        
        # Attempt transmission with retries
        for attempt in range(self.max_retries):
            if self.radio.send(payload):
                self.status.last_tx_time = datetime.now(timezone.utc)
                self.status.tx_count += 1
                logger.info(f"LoRa TX success: {packet.lat:.6f}, {packet.lon:.6f} "
                           f"(attempt {attempt + 1}/{self.max_retries})")
                return True
            else:
                logger.warning(f"LoRa TX failed (attempt {attempt + 1}/{self.max_retries})")
                if attempt < self.max_retries - 1:
                    time.sleep(0.5 * (attempt + 1))  # Exponential backoff
        
        self.status.tx_fail_count += 1
        return False

    def check_incoming(self) -> Optional[ReceivedMessage]:
        """Check for incoming messages."""
        if not self.status.connected:
            return None
        
        result = self.radio.receive(timeout_ms=100)
        if result:
            data, rssi, snr = result
            msg = ReceivedMessage(
                timestamp=datetime.now(timezone.utc),
                payload=data,
                payload_hex=data.hex(),
                rssi=rssi,
                snr=snr,
                frequency_error=0,
            )
            self._received_messages.append(msg)
            self.status.rx_count += 1
            self.status.last_rx_time = msg.timestamp
            self.status.rssi = rssi
            self.status.snr = snr
            
            # Log to file
            self._log_message(msg)
            
            logger.info(f"LoRa RX: {len(data)} bytes, RSSI={rssi}dBm, SNR={snr:.1f}dB")
            return msg
        return None

    def _log_message(self, msg: ReceivedMessage) -> None:
        """Log received message to file."""
        try:
            with open(LORA_LOG_FILE, 'a') as f:
                f.write(json.dumps({
                    'timestamp': msg.timestamp.isoformat(),
                    'payload_hex': msg.payload_hex,
                    'rssi': msg.rssi,
                    'snr': msg.snr,
                }) + '\n')
        except Exception as e:
            logger.debug(f"Failed to log message: {e}")

    def _transmission_loop(self) -> None:
        """Background thread for periodic transmissions."""
        logger.info("LoRaWAN transmission loop started")
        last_tx = 0.0
        
        while self._running:
            now = time.time()
            
            # Check if we should transmit (no WiFi/cellular and interval elapsed)
            should_tx = (
                not self._wifi_connected and 
                not self._cellular_connected and
                now - last_tx >= self.tx_interval_sec
            )
            
            if should_tx:
                if self.transmit_position():
                    last_tx = now
            
            # Check for incoming messages (brief poll)
            self.check_incoming()
            
            # Sleep for a bit
            time.sleep(1.0)

    def start(self) -> None:
        """Start the background transmission thread."""
        if not self.enabled or not self.status.connected:
            return
        
        self._running = True
        self._thread = threading.Thread(target=self._transmission_loop, daemon=True)
        self._thread.start()
        logger.info("LoRaWAN backup module started")

    def stop(self) -> None:
        """Stop the module."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=5.0)
        self.radio.close()
        self.status.connected = False
        logger.info("LoRaWAN backup module stopped")

    def get_status(self) -> Dict[str, Any]:
        """Get current status."""
        return {
            'enabled': self.enabled,
            'connected': self.status.connected,
            'simulation_mode': self.status.simulation_mode,
            'frequency_mhz': self.status.frequency_mhz,
            'spreading_factor': self.status.spreading_factor,
            'tx_power_dbm': self.status.tx_power_dbm,
            'last_tx_time': self.status.last_tx_time.isoformat() if self.status.last_tx_time else None,
            'last_rx_time': self.status.last_rx_time.isoformat() if self.status.last_rx_time else None,
            'tx_count': self.status.tx_count,
            'rx_count': self.status.rx_count,
            'tx_fail_count': self.status.tx_fail_count,
            'rssi': self.status.rssi,
            'snr': self.status.snr,
            'tx_interval_sec': self.tx_interval_sec,
        }

    def get_received_messages(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Get recent received messages."""
        messages = list(self._received_messages)[-limit:]
        return [
            {
                'timestamp': m.timestamp.isoformat(),
                'payload_hex': m.payload_hex,
                'rssi': m.rssi,
                'snr': m.snr,
            }
            for m in messages
        ]


# ---------------------------------------------------------------------------
# Configuration Helpers
# ---------------------------------------------------------------------------
def load_config() -> Dict[str, Any]:
    """Load configuration from YAML file."""
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH) as f:
                config = yaml.safe_load(f) or {}
                return config.get('lorawan', {})
        except Exception as e:
            logger.warning(f"Failed to load config: {e}")
    return {}


def save_lorawan_config(updates: Dict[str, Any]) -> bool:
    """Save LoRaWAN configuration updates."""
    try:
        config = {}
        if CONFIG_PATH.exists():
            with open(CONFIG_PATH) as f:
                config = yaml.safe_load(f) or {}
        
        if 'lorawan' not in config:
            config['lorawan'] = {}
        
        config['lorawan'].update(updates)
        
        with open(CONFIG_PATH, 'w') as f:
            yaml.dump(config, f, default_flow_style=False)
        return True
    except Exception as e:
        logger.error(f"Failed to save config: {e}")
        return False


# ---------------------------------------------------------------------------
# HTTP API Handler
# ---------------------------------------------------------------------------
class LoRaWANHandler(BaseHTTPRequestHandler):
    """HTTP request handler for LoRaWAN module API."""

    manager: Optional[LoRaWANManager] = None

    def _json_response(self, data: dict, status: int = 200) -> None:
        """Send JSON response."""
        body = json.dumps(data, indent=2, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> Dict[str, Any]:
        """Read and parse JSON body."""
        content_length = int(self.headers.get('Content-Length', 0))
        if content_length:
            body = self.rfile.read(content_length).decode('utf-8')
            try:
                return json.loads(body)
            except json.JSONDecodeError:
                pass
        return {}

    def do_GET(self) -> None:
        """Handle GET requests."""
        if self.path == '/lora/status' or self.path == '/lora/status/':
            self._handle_status()
        elif self.path == '/lora/health' or self.path == '/lora/health/':
            self._handle_health()
        elif self.path == '/lora/messages' or self.path == '/lora/messages/':
            self._handle_messages()
        elif self.path == '/lora/config' or self.path == '/lora/config/':
            self._handle_get_config()
        else:
            self._json_response({'error': 'Not found', 'path': self.path}, 404)

    def do_POST(self) -> None:
        """Handle POST requests."""
        if self.path == '/lora/send' or self.path == '/lora/send/':
            self._handle_send()
        elif self.path == '/lora/gps' or self.path == '/lora/gps/':
            self._handle_gps_update()
        elif self.path == '/lora/config' or self.path == '/lora/config/':
            self._handle_update_config()
        else:
            self._json_response({'error': 'Not found', 'path': self.path}, 404)

    def _handle_status(self) -> None:
        """GET /lora/status - Get module status."""
        if not self.manager:
            self._json_response({'error': 'Module not initialized'}, 503)
            return
        self._json_response(self.manager.get_status())

    def _handle_health(self) -> None:
        """GET /lora/health - Health check."""
        if not self.manager:
            self._json_response({'status': 'error', 'message': 'Module not initialized'}, 503)
            return
        
        status = self.manager.get_status()
        healthy = status.get('connected', False)
        
        self._json_response({
            'status': 'ok' if healthy else 'degraded',
            'connected': status.get('connected'),
            'simulation_mode': status.get('simulation_mode'),
            'timestamp': datetime.now(timezone.utc).isoformat(),
        })

    def _handle_messages(self) -> None:
        """GET /lora/messages - Get received messages."""
        if not self.manager:
            self._json_response({'error': 'Module not initialized'}, 503)
            return
        
        query = self.path.split('?')
        limit = 50
        if len(query) > 1:
            for param in query[1].split('&'):
                if param.startswith('limit='):
                    try:
                        limit = int(param.split('=')[1])
                    except ValueError:
                        pass
        
        messages = self.manager.get_received_messages(limit=limit)
        self._json_response({
            'messages': messages,
            'count': len(messages),
        })

    def _handle_send(self) -> None:
        """POST /lora/send - Manually send a message."""
        if not self.manager:
            self._json_response({'error': 'Module not initialized'}, 503)
            return
        
        body = self._read_body()
        
        # Check if custom message provided
        if 'payload' in body:
            # Custom payload (hex string)
            try:
                payload = bytes.fromhex(body['payload'])
                success = self.manager.radio.send(payload)
            except ValueError:
                self._json_response({'error': 'Invalid hex payload'}, 400)
                return
        else:
            # Send current position
            success = self.manager.transmit_position()
        
        if success:
            self._json_response({
                'status': 'ok',
                'message': 'Transmission successful',
                'timestamp': datetime.now(timezone.utc).isoformat(),
            })
        else:
            self._json_response({
                'status': 'error',
                'message': 'Transmission failed',
            }, 500)

    def _handle_gps_update(self) -> None:
        """POST /lora/gps - Update GPS position."""
        if not self.manager:
            self._json_response({'error': 'Module not initialized'}, 503)
            return
        
        body = self._read_body()
        lat = body.get('lat')
        lon = body.get('lon')
        
        if lat is None or lon is None:
            self._json_response({'error': 'Missing lat or lon'}, 400)
            return
        
        self.manager.update_gps(
            lat=float(lat),
            lon=float(lon),
            fix_quality=body.get('fix_quality', 0),
            speed=body.get('speed', 0.0),
        )
        
        self._json_response({
            'status': 'ok',
            'message': 'GPS position updated',
        })

    def _handle_get_config(self) -> None:
        """GET /lora/config - Get current configuration."""
        config = load_config()
        self._json_response({
            'config': config,
        })

    def _handle_update_config(self) -> None:
        """POST /lora/config - Update configuration."""
        body = self._read_body()
        
        # Filter allowed config keys
        allowed_keys = [
            'enabled', 'frequency_mhz', 'spreading_factor',
            'tx_power_dbm', 'bandwidth_khz', 'coding_rate',
            'tx_interval_sec', 'max_retries', 'gateway_id'
        ]
        
        updates = {k: v for k, v in body.items() if k in allowed_keys}
        
        if not updates:
            self._json_response({'error': 'No valid config keys provided'}, 400)
            return
        
        if save_lorawan_config(updates):
            self._json_response({
                'status': 'ok',
                'message': 'Configuration updated',
                'updates': updates,
            })
        else:
            self._json_response({
                'status': 'error',
                'message': 'Failed to save configuration',
            }, 500)

    def log_message(self, format: str, *args: Any) -> None:
        """Suppress default logging."""
        logger.debug(f"HTTP: {format % args}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    """Main entry point."""
    parser = argparse.ArgumentParser(description="LoRaWAN Backup Module")
    parser.add_argument(
        '--port', type=int, default=DEFAULT_PORT,
        help=f'HTTP API port (default: {DEFAULT_PORT})'
    )
    parser.add_argument(
        '--simulate', action='store_true',
        help='Run in simulation mode (no hardware required)'
    )
    parser.add_argument(
        '--config', type=str,
        help='Path to config file'
    )
    args = parser.parse_args()

    # Load configuration
    global CONFIG_PATH
    if args.config:
        CONFIG_PATH = Path(args.config)
    
    lora_config = load_config()
    
    # Create manager
    manager = LoRaWANManager(lora_config, simulate=args.simulate)
    
    # Initialize
    if not manager.init():
        logger.error("Failed to initialize LoRaWAN module")
        if not args.simulate:
            sys.exit(1)
        # In simulate mode, continue anyway
    
    # Set handler class attribute
    LoRaWANHandler.manager = manager
    
    # Start background operations
    manager.start()
    
    # Start HTTP server
    server = HTTPServer(('127.0.0.1', args.port), LoRaWANHandler)
    logger.info(f"LoRaWAN API server started on http://127.0.0.1:{args.port}/")
    
    # Handle shutdown
    def signal_handler(signum, frame):
        logger.info("Shutting down...")
        manager.stop()
        server.shutdown()
    
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)
    
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        manager.stop()
        server.server_close()


if __name__ == "__main__":
    main()
