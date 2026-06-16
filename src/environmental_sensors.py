#!/usr/bin/env python3
"""
Environmental Sensors Module — Dog Agent
=========================================
Reads environmental and motion sensors over I2C and serves via HTTP API.

Supported Sensors:
  - BME280: Temperature, Humidity, Pressure, Altitude (0x76 or 0x77)
  - VEML6070: UV Index (0x38)
  - LTR-329: Ambient Light (0x29)
  - BNO055: 9-DOF IMU - Orientation, Acceleration, Gyroscope, Magnetometer (0x28)

I2C Protocol Notes:
  - BME280: Calibrated temp/humidity/pressure sensor with compensation registers
  - VEML6070: Simple UV sensor, read 16-bit value, convert to UV index
  - LTR-329: Dual-channel light sensor (visible + IR), calculate lux
  - BNO055: Absolute orientation sensor with fusion algorithm on-chip

Usage:
    python src/environmental_sensors.py              # Normal mode (I2C hardware)
    python src/environmental_sensors.py --simulate # Simulation mode
    python src/environmental_sensors.py --config /path/to/config.yaml
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
from collections import deque
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logger = logging.getLogger("environmental_sensors")
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
I2C_ADDRESSES = {
    "bme280": [0x76, 0x77],
    "veml6070": [0x38],
    "ltr329": [0x29],
    "bno055": [0x28],
}

# BME280 Registers
BME280_REG_TEMP_MSB = 0xFA
BME280_REG_HUM_MSB = 0xFD
BME280_REG_PRESS_MSB = 0xF7
BME280_REG_CTRL_MEAS = 0xF4
BME280_REG_CTRL_HUM = 0xF2
BME280_REG_CONFIG = 0xF5
BME280_REG_CALIB = 0x88

# BNO055 Registers
BNO055_REG_CHIP_ID = 0x00
BNO055_REG_OPR_MODE = 0x3D
BNO055_REG_PWR_MODE = 0x3E
BNO055_REG_SYS_TRIGGER = 0x3F
BNO055_REG_QUAT_DATA = 0x20
BNO055_REG_ACCEL_DATA = 0x08
BNO055_REG_GYRO_DATA = 0x14
BNO055_REG_MAG_DATA = 0x0E
BNO055_REG_CALIB_STAT = 0x35

# Operating modes
BNO055_MODE_CONFIG = 0x00
BNO055_MODE_NDOF = 0x0C

# VEML6070
VEML6070_REG_UV = 0x00
VEML6070_CMD = 0x02

# LTR-329
LTR329_REG_CTRL = 0x80
LTR329_REG_DATA_CH1 = 0x88
LTR329_REG_DATA_CH0 = 0x8A


# ---------------------------------------------------------------------------
# Data Classes
# ---------------------------------------------------------------------------
@dataclass
class BME280Data:
    """BME280 sensor readings."""
    temperature_c: float = 0.0
    humidity_percent: float = 0.0
    pressure_hpa: float = 0.0
    altitude_m: float = 0.0
    available: bool = False


@dataclass
class VEML6070Data:
    """VEML6070 UV sensor readings."""
    uv_raw: int = 0
    uv_index: float = 0.0
    available: bool = False


@dataclass
class LTR329Data:
    """LTR-329 ambient light sensor readings."""
    ch0_visible: int = 0
    ch1_infrared: int = 0
    lux: float = 0.0
    available: bool = False


@dataclass
class BNO055Data:
    """BNO055 IMU readings."""
    quaternion: Tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)
    acceleration: Tuple[float, float, float] = (0.0, 0.0, 0.0)
    gyroscope: Tuple[float, float, float] = (0.0, 0.0, 0.0)
    magnetometer: Tuple[float, float, float] = (0.0, 0.0, 0.0)
    linear_acceleration: Tuple[float, float, float] = (0.0, 0.0, 0.0)
    gravity: Tuple[float, float, float] = (0.0, 0.0, 0.0)
    calibration_status: int = 0
    available: bool = False


@dataclass
class EnvironmentalReadings:
    """Thread-safe container for all environmental sensor readings."""
    _lock: threading.Lock = field(default_factory=threading.Lock)
    
    # BME280
    bme280: BME280Data = field(default_factory=BME280Data)
    
    # VEML6070
    veml6070: VEML6070Data = field(default_factory=VEML6070Data)
    
    # LTR-329
    ltr329: LTR329Data = field(default_factory=LTR329Data)
    
    # BNO055
    bno055: BNO055Data = field(default_factory=BNO055Data)
    
    # Health insights
    heat_stress_index: float = 0.0  # 0-100 scale
    uv_exposure_minutes: float = 0.0  # Cumulative UV exposure
    fall_detected: bool = False
    gait_score: float = 0.0  # 0-100, activity level
    
    timestamp: Optional[datetime] = None
    valid: bool = False
    
    def update(self, bme280: Optional[BME280Data] = None,
               veml6070: Optional[VEML6070Data] = None,
               ltr329: Optional[LTR329Data] = None,
               bno055: Optional[BNO055Data] = None,
               health_insights: Optional[Dict[str, Any]] = None) -> None:
        """Atomically update sensor readings."""
        with self._lock:
            if bme280:
                self.bme280 = bme280
            if veml6070:
                self.veml6070 = veml6070
            if ltr329:
                self.ltr329 = ltr329
            if bno055:
                self.bno055 = bno055
            if health_insights:
                self.heat_stress_index = health_insights.get("heat_stress_index", self.heat_stress_index)
                self.uv_exposure_minutes = health_insights.get("uv_exposure_minutes", self.uv_exposure_minutes)
                self.fall_detected = health_insights.get("fall_detected", self.fall_detected)
                self.gait_score = health_insights.get("gait_score", self.gait_score)
            self.timestamp = datetime.now(timezone.utc)
            self.valid = any([
                self.bme280.available,
                self.veml6070.available,
                self.ltr329.available,
                self.bno055.available,
            ])

    def snapshot(self) -> Dict[str, Any]:
        """Return a thread-safe copy of current readings."""
        with self._lock:
            return {
                "bme280": {
                    "temperature_c": round(self.bme280.temperature_c, 2),
                    "humidity_percent": round(self.bme280.humidity_percent, 1),
                    "pressure_hpa": round(self.bme280.pressure_hpa, 2),
                    "altitude_m": round(self.bme280.altitude_m, 1),
                    "available": self.bme280.available,
                } if self.bme280.available else None,
                "uv": {
                    "uv_index": round(self.veml6070.uv_index, 1),
                    "uv_raw": self.veml6070.uv_raw,
                    "available": self.veml6070.available,
                } if self.veml6070.available else None,
                "light": {
                    "lux": round(self.ltr329.lux, 1),
                    "ch0_visible": self.ltr329.ch0_visible,
                    "ch1_infrared": self.ltr329.ch1_infrared,
                    "available": self.ltr329.available,
                } if self.ltr329.available else None,
                "imu": {
                    "quaternion": list(self.bno055.quaternion),
                    "acceleration": {
                        "x": round(self.bno055.acceleration[0], 3),
                        "y": round(self.bno055.acceleration[1], 3),
                        "z": round(self.bno055.acceleration[2], 3),
                    },
                    "gyroscope": {
                        "x": round(self.bno055.gyroscope[0], 3),
                        "y": round(self.bno055.gyroscope[1], 3),
                        "z": round(self.bno055.gyroscope[2], 3),
                    },
                    "magnetometer": {
                        "x": round(self.bno055.magnetometer[0], 1),
                        "y": round(self.bno055.magnetometer[1], 1),
                        "z": round(self.bno055.magnetometer[2], 1),
                    },
                    "linear_acceleration": {
                        "x": round(self.bno055.linear_acceleration[0], 3),
                        "y": round(self.bno055.linear_acceleration[1], 3),
                        "z": round(self.bno055.linear_acceleration[2], 3),
                    },
                    "gravity": {
                        "x": round(self.bno055.gravity[0], 3),
                        "y": round(self.bno055.gravity[1], 3),
                        "z": round(self.bno055.gravity[2], 3),
                    },
                    "calibration_status": self.bno055.calibration_status,
                    "available": self.bno055.available,
                } if self.bno055.available else None,
                "health_index": {
                    "heat_stress_index": round(self.heat_stress_index, 1),
                    "heat_stress_level": self._get_heat_stress_level(),
                    "uv_exposure_minutes": round(self.uv_exposure_minutes, 1),
                    "fall_detected": self.fall_detected,
                    "gait_score": round(self.gait_score, 1),
                    "gait_status": self._get_gait_status(),
                },
                "valid": self.valid,
                "timestamp": self.timestamp.isoformat() if self.timestamp else None,
            }
    
    def _get_heat_stress_level(self) -> str:
        """Convert heat stress index to human-readable level."""
        if self.heat_stress_index < 30:
            return "normal"
        elif self.heat_stress_index < 50:
            return "elevated"
        elif self.heat_stress_index < 70:
            return "high"
        elif self.heat_stress_index < 85:
            return "danger"
        else:
            return "critical"
    
    def _get_gait_status(self) -> str:
        """Convert gait score to activity status."""
        if self.gait_score < 10:
            return "resting"
        elif self.gait_score < 30:
            return "slow_walk"
        elif self.gait_score < 60:
            return "walking"
        elif self.gait_score < 85:
            return "running"
        else:
            return "high_activity"


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
def load_config(path: str) -> dict:
    """Load YAML config with environmental sensor defaults."""
    with open(path) as f:
        cfg = yaml.safe_load(f) or {}

    env_cfg = cfg.get("environmental_sensors", {})
    hermes_cfg = cfg.get("hermes", {})

    return {
        "enabled": env_cfg.get("enabled", False),
        "i2c_bus": env_cfg.get("i2c_bus", 1),
        "auto_detect": env_cfg.get("auto_detect", True),
        "poll_interval_sec": env_cfg.get("poll_interval_sec", 30),
        "api_port": env_cfg.get("api_port", 9122),
        "heat_stress_threshold": env_cfg.get("heat_stress_threshold", 70),
        "uv_alert_threshold": env_cfg.get("uv_alert_threshold", 6),
    }


# ---------------------------------------------------------------------------
# I2C Scanner
# ---------------------------------------------------------------------------
class I2CScanner:
    """Scans I2C bus to detect connected sensors."""
    
    def __init__(self, bus_num: int):
        try:
            import smbus2
            self._bus = smbus2.SMBus(bus_num)
            self._smbus_available = True
        except Exception:
            self._bus = None
            self._smbus_available = False
            logger.warning("smbus2 not available, cannot scan I2C bus")
    
    def scan(self) -> Dict[str, List[int]]:
        """Scan I2C bus and return detected sensors."""
        detected = {
            "bme280": [],
            "veml6070": [],
            "ltr329": [],
            "bno055": [],
        }
        
        if not self._smbus_available:
            return detected
        
        logger.info("Scanning I2C bus for environmental sensors...")
        
        # Check each known address
        for sensor, addresses in I2C_ADDRESSES.items():
            for addr in addresses:
                if self._probe_address(addr):
                    detected[sensor].append(addr)
                    logger.info(f"Detected {sensor} at address 0x{addr:02X}")
        
        return detected
    
    def _probe_address(self, addr: int) -> bool:
        """Probe an I2C address to check if device is present."""
        try:
            # Try to read a byte from the device
            self._bus.read_byte(addr)
            return True
        except Exception:
            return False
    
    def close(self):
        """Close I2C bus."""
        if self._bus:
            with suppress(Exception):
                self._bus.close()


# ---------------------------------------------------------------------------
# Sensor Drivers
# ---------------------------------------------------------------------------
class BME280Driver:
    """Driver for BME280 temperature/humidity/pressure sensor."""
    
    def __init__(self, bus: Any, address: int):
        self._bus = bus
        self._address = address
        self._calibration = {}
        self._init_sensor()
    
    def _init_sensor(self) -> None:
        """Initialize BME280 with default settings."""
        # Read calibration data
        calib = self._bus.read_i2c_block_data(self._address, BME280_REG_CALIB, 26)
        
        # Parse calibration data
        self._calibration = {
            "dig_T1": self._uint16(calib[0], calib[1]),
            "dig_T2": self._int16(calib[2], calib[3]),
            "dig_T3": self._int16(calib[4], calib[5]),
            "dig_P1": self._uint16(calib[6], calib[7]),
            "dig_P2": self._int16(calib[8], calib[9]),
            "dig_P3": self._int16(calib[10], calib[11]),
            "dig_P4": self._int16(calib[12], calib[13]),
            "dig_P5": self._int16(calib[14], calib[15]),
            "dig_P6": self._int16(calib[16], calib[17]),
            "dig_P7": self._int16(calib[18], calib[19]),
            "dig_P8": self._int16(calib[20], calib[21]),
            "dig_P9": self._int16(calib[22], calib[23]),
        }
        
        # Read humidity calibration
        calib_hum = self._bus.read_i2c_block_data(self._address, 0xE1, 7)
        self._calibration["dig_H1"] = self._bus.read_byte_data(self._address, 0xA1)
        self._calibration["dig_H2"] = self._int16(calib_hum[0], calib_hum[1])
        self._calibration["dig_H3"] = calib_hum[2]
        self._calibration["dig_H4"] = (calib_hum[3] << 4) | (calib_hum[4] & 0x0F)
        self._calibration["dig_H5"] = (calib_hum[5] << 4) | (calib_hum[4] >> 4)
        self._calibration["dig_H6"] = self._int8(calib_hum[6])
        
        # Set configuration: oversampling x1 for all, normal mode
        self._bus.write_byte_data(self._address, BME280_REG_CTRL_HUM, 0x01)
        self._bus.write_byte_data(self._address, BME280_REG_CTRL_MEAS, 0x27)
        self._bus.write_byte_data(self._address, BME280_REG_CONFIG, 0x00)
    
    @staticmethod
    def _uint16(msb: int, lsb: int) -> int:
        return (msb << 8) | lsb
    
    @staticmethod
    def _int16(msb: int, lsb: int) -> int:
        val = (msb << 8) | lsb
        return val - 65536 if val > 32767 else val
    
    @staticmethod
    def _int8(val: int) -> int:
        return val - 256 if val > 127 else val
    
    def read(self) -> BME280Data:
        """Read and compensate BME280 data."""
        try:
            # Read raw data
            data = self._bus.read_i2c_block_data(self._address, 0xF7, 8)
            
            # Parse raw values
            raw_press = (data[0] << 12) | (data[1] << 4) | (data[2] >> 4)
            raw_temp = (data[3] << 12) | (data[4] << 4) | (data[5] >> 4)
            raw_hum = (data[6] << 8) | data[7]
            
            # Compensate temperature
            var1 = ((raw_temp / 16384.0) - (self._calibration["dig_T1"] / 1024.0))
            var1 = var1 * self._calibration["dig_T2"]
            var2 = ((raw_temp / 131072.0) - (self._calibration["dig_T1"] / 8192.0))
            var2 = (var2 ** 2) * self._calibration["dig_T3"]
            t_fine = var1 + var2
            temp_c = t_fine / 5120.0
            
            # Compensate pressure
            var1 = (t_fine / 2.0) - 64000.0
            var2 = var1 ** 2 * self._calibration["dig_P6"] / 32768.0
            var2 = var2 + var1 * self._calibration["dig_P5"] * 2.0
            var2 = (var2 / 4.0) + (self._calibration["dig_P4"] * 65536.0)
            var1 = (self._calibration["dig_P3"] * var1 * var1 / 524288.0 +
                   self._calibration["dig_P2"] * var1) / 524288.0
            var1 = (1.0 + var1 / 32768.0) * self._calibration["dig_P1"]
            
            if var1 == 0:
                pressure_hpa = 0
            else:
                pressure = 1048576.0 - raw_press
                pressure = ((pressure - var2 / 4096.0) * 6250.0) / var1
                var1 = self._calibration["dig_P9"] * pressure * pressure / 2147483648.0
                var2 = pressure * self._calibration["dig_P8"] / 32768.0
                pressure = pressure + (var1 + var2 + self._calibration["dig_P7"]) / 256.0
                pressure_hpa = pressure / 100.0
            
            # Compensate humidity
            var_h = t_fine - 76800.0
            var_h = (raw_hum - (self._calibration["dig_H4"] * 64.0 +
                              self._calibration["dig_H5"] / 16384.0 * var_h)) * \
                   (self._calibration["dig_H2"] / 65536.0 * 
                    (1.0 + self._calibration["dig_H6"] / 67108864.0 * var_h *
                     (1.0 + self._calibration["dig_H3"] / 67108864.0 * var_h)))
            var_h = var_h * (1.0 - self._calibration["dig_H1"] * var_h / 524288.0)
            humidity_percent = max(0.0, min(100.0, var_h))
            
            # Calculate altitude (using standard atmosphere)
            altitude_m = 44330.0 * (1.0 - (pressure_hpa / 1013.25) ** 0.1903)
            
            return BME280Data(
                temperature_c=temp_c,
                humidity_percent=humidity_percent,
                pressure_hpa=pressure_hpa,
                altitude_m=altitude_m,
                available=True
            )
        except Exception as e:
            logger.error(f"BME280 read error: {e}")
            return BME280Data(available=False)


class VEML6070Driver:
    """Driver for VEML6070 UV sensor."""
    
    def __init__(self, bus: Any, address: int = 0x38):
        self._bus = bus
        self._address = address
        self._init_sensor()
    
    def _init_sensor(self) -> None:
        """Initialize VEML6070 with 1x integration time."""
        # Command: SD=0 (active), Reserved=0, IT=0 (1x), ACK=0, ACK_THD=0
        self._bus.write_byte(self._address, 0x02)
    
    def read(self) -> VEML6070Data:
        """Read UV index from VEML6070."""
        try:
            # Read 16-bit UV value (MSB first)
            msb = self._bus.read_byte(self._address)
            lsb = self._bus.read_byte(self._address)
            uv_raw = (msb << 8) | lsb
            
            # Convert to UV index (approximate conversion)
            # VEML6070 output is not exactly UV index, needs calibration
            # Rough approximation: UV index = raw / ~100-150 depending on sensor
            uv_index = uv_raw / 100.0
            
            return VEML6070Data(
                uv_raw=uv_raw,
                uv_index=uv_index,
                available=True
            )
        except Exception as e:
            logger.error(f"VEML6070 read error: {e}")
            return VEML6070Data(available=False)


class LTR329Driver:
    """Driver for LTR-329 ambient light sensor."""
    
    def __init__(self, bus: Any, address: int = 0x29):
        self._bus = bus
        self._address = address
        self._init_sensor()
    
    def _init_sensor(self) -> None:
        """Initialize LTR-329."""
        # Set gain and integration time
        # Gain=1x, Integration time=100ms
        self._bus.write_byte_data(self._address, LTR329_REG_CTRL, 0x01)
    
    def read(self) -> LTR329Data:
        """Read light level from LTR-329."""
        try:
            # Read both channels
            ch1_data = self._bus.read_i2c_block_data(self._address, LTR329_REG_DATA_CH1, 2)
            ch0_data = self._bus.read_i2c_block_data(self._address, LTR329_REG_DATA_CH0, 2)
            
            ch1 = (ch1_data[1] << 8) | ch1_data[0]  # IR + visible
            ch0 = (ch0_data[1] << 8) | ch0_data[0]  # Visible only
            
            # Calculate lux (simplified formula)
            # ratio = ch1 / (ch1 + ch0) if ch0 > 0 else 0
            # Use piecewise approximation for lux calculation
            if ch0 > 0:
                ratio = ch1 / (ch1 + ch0)
                if ratio < 0.45:
                    lux = (1.774 * ch0 + 1.105 * ch1) / 1.0  # Gain=1x
                elif ratio < 0.64:
                    lux = (4.278 * ch0 - 1.954 * ch1) / 1.0
                elif ratio < 0.85:
                    lux = (0.592 * ch0 + 0.118 * ch1) / 1.0
                else:
                    lux = 0.0
            else:
                lux = 0.0
            
            return LTR329Data(
                ch0_visible=ch0,
                ch1_infrared=ch1,
                lux=lux,
                available=True
            )
        except Exception as e:
            logger.error(f"LTR-329 read error: {e}")
            return LTR329Data(available=False)


class BNO055Driver:
    """Driver for BNO055 9-DOF IMU."""
    
    def __init__(self, bus: Any, address: int = 0x28):
        self._bus = bus
        self._address = address
        self._init_sensor()
    
    def _init_sensor(self) -> None:
        """Initialize BNO055 in NDOF mode."""
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
        
        # Set to NDOF mode (fusion mode)
        self._bus.write_byte_data(self._address, BNO055_REG_OPR_MODE, BNO055_MODE_NDOF)
        time.sleep(0.025)
    
    def read(self) -> BNO055Data:
        """Read all BNO055 data."""
        try:
            # Read calibration status
            cal_stat = self._bus.read_byte_data(self._address, BNO055_REG_CALIB_STAT)
            
            # Read quaternion (w, x, y, z) - each is 16-bit signed / 2^14
            quat_data = self._bus.read_i2c_block_data(self._address, BNO055_REG_QUAT_DATA, 8)
            quat = tuple(
                self._int16(quat_data[i], quat_data[i+1]) / 16384.0
                for i in range(0, 8, 2)
            )
            
            # Read acceleration (x, y, z) - each is 16-bit signed / 100 m/s^2
            accel_data = self._bus.read_i2c_block_data(self._address, BNO055_REG_ACCEL_DATA, 6)
            accel = tuple(
                self._int16(accel_data[i], accel_data[i+1]) / 100.0
                for i in range(0, 6, 2)
            )
            
            # Read gyroscope (x, y, z) - each is 16-bit signed / 16 deg/s
            gyro_data = self._bus.read_i2c_block_data(self._address, BNO055_REG_GYRO_DATA, 6)
            gyro = tuple(
                self._int16(gyro_data[i], gyro_data[i+1]) / 16.0
                for i in range(0, 6, 2)
            )
            
            # Read magnetometer (x, y, z) - each is 16-bit signed / 16 uT
            mag_data = self._bus.read_i2c_block_data(self._address, BNO055_REG_MAG_DATA, 6)
            mag = tuple(
                self._int16(mag_data[i], mag_data[i+1]) / 16.0
                for i in range(0, 6, 2)
            )
            
            # Linear acceleration (from register 0x28)
            lin_accel_data = self._bus.read_i2c_block_data(self._address, 0x28, 6)
            lin_accel = tuple(
                self._int16(lin_accel_data[i], lin_accel_data[i+1]) / 100.0
                for i in range(0, 6, 2)
            )
            
            # Gravity vector (from register 0x2E)
            grav_data = self._bus.read_i2c_block_data(self._address, 0x2E, 6)
            gravity = tuple(
                self._int16(grav_data[i], grav_data[i+1]) / 100.0
                for i in range(0, 6, 2)
            )
            
            return BNO055Data(
                quaternion=quat,
                acceleration=accel,
                gyroscope=gyro,
                magnetometer=mag,
                linear_acceleration=lin_accel,
                gravity=gravity,
                calibration_status=cal_stat,
                available=True
            )
        except Exception as e:
            logger.error(f"BNO055 read error: {e}")
            return BNO055Data(available=False)
    
    @staticmethod
    def _int16(msb: int, lsb: int) -> int:
        val = (msb << 8) | lsb
        return val - 65536 if val > 32767 else val


# ---------------------------------------------------------------------------
# Sensor Manager
# ---------------------------------------------------------------------------
class EnvironmentalSensorManager:
    """Manages all environmental sensors."""
    
    def __init__(self, bus_num: int, auto_detect: bool = True):
        self._bus_num = bus_num
        self._auto_detect = auto_detect
        self._bus = None
        self._drivers: Dict[str, Any] = {}
        
        # Health tracking
        self._uv_accumulator = 0.0
        self._uv_start_time = datetime.now(timezone.utc)
        self._accel_history: deque = deque(maxlen=100)  # For fall detection
        self._gait_history: deque = deque(maxlen=50)    # For gait analysis
        self._last_fall_time: Optional[datetime] = None
        
        self._init_bus()
        if auto_detect:
            self._detect_and_init()
    
    def _init_bus(self) -> None:
        """Initialize I2C bus."""
        try:
            import smbus2
            self._bus = smbus2.SMBus(self._bus_num)
            logger.info(f"Opened I2C bus {self._bus_num}")
        except ImportError:
            raise ImportError("smbus2 is required. Install with: pip install smbus2")
        except Exception as e:
            logger.error(f"Failed to open I2C bus: {e}")
            raise
    
    def _detect_and_init(self) -> None:
        """Auto-detect and initialize sensors."""
        scanner = I2CScanner(self._bus_num)
        detected = scanner.scan()
        scanner.close()
        
        # Initialize detected sensors
        if detected["bme280"]:
            try:
                addr = detected["bme280"][0]
                self._drivers["bme280"] = BME280Driver(self._bus, addr)
                logger.info(f"BME280 initialized at 0x{addr:02X}")
            except Exception as e:
                logger.error(f"Failed to initialize BME280: {e}")
        
        if detected["veml6070"]:
            try:
                addr = detected["veml6070"][0]
                self._drivers["veml6070"] = VEML6070Driver(self._bus, addr)
                logger.info(f"VEML6070 initialized at 0x{addr:02X}")
            except Exception as e:
                logger.error(f"Failed to initialize VEML6070: {e}")
        
        if detected["ltr329"]:
            try:
                addr = detected["ltr329"][0]
                self._drivers["ltr329"] = LTR329Driver(self._bus, addr)
                logger.info(f"LTR-329 initialized at 0x{addr:02X}")
            except Exception as e:
                logger.error(f"Failed to initialize LTR-329: {e}")
        
        if detected["bno055"]:
            try:
                addr = detected["bno055"][0]
                self._drivers["bno055"] = BNO055Driver(self._bus, addr)
                logger.info(f"BNO055 initialized at 0x{addr:02X}")
            except Exception as e:
                logger.error(f"Failed to initialize BNO055: {e}")
    
    def read_all(self) -> Tuple[BME280Data, VEML6070Data, LTR329Data, BNO055Data]:
        """Read all connected sensors."""
        bme280_data = BME280Data(available=False)
        veml6070_data = VEML6070Data(available=False)
        ltr329_data = LTR329Data(available=False)
        bno055_data = BNO055Data(available=False)
        
        if "bme280" in self._drivers:
            bme280_data = self._drivers["bme280"].read()
        
        if "veml6070" in self._drivers:
            veml6070_data = self._drivers["veml6070"].read()
        
        if "ltr329" in self._drivers:
            ltr329_data = self._drivers["ltr329"].read()
        
        if "bno055" in self._drivers:
            bno055_data = self._drivers["bno055"].read()
            
            # Store acceleration history for fall detection
            if bno055_data.available:
                self._accel_history.append(bno055_data.linear_acceleration)
                # Calculate activity level for gait analysis
                accel_mag = math.sqrt(sum(a**2 for a in bno055_data.linear_acceleration))
                self._gait_history.append(accel_mag)
        
        return bme280_data, veml6070_data, ltr329_data, bno055_data
    
    def calculate_health_insights(self, bme280: BME280Data, 
                                   bno055: BNO055Data) -> Dict[str, Any]:
        """Calculate health insights from sensor data."""
        insights = {
            "heat_stress_index": 0.0,
            "uv_exposure_minutes": 0.0,
            "fall_detected": False,
            "gait_score": 0.0,
        }
        
        # Heat stress index calculation
        if bme280.available:
            # Based on temperature-humidity index for dogs
            # Simplified: combine temp and humidity with activity
            temp_factor = max(0, (bme280.temperature_c - 25) * 2)
            humidity_factor = bme280.humidity_percent * 0.5
            
            # Add activity component if available
            activity_factor = 0
            if bno055.available and self._gait_history:
                avg_activity = sum(self._gait_history) / len(self._gait_history)
                activity_factor = avg_activity * 10
            
            insights["heat_stress_index"] = min(100, temp_factor + humidity_factor + activity_factor)
        
        # UV exposure tracking
        # Accumulate exposure when UV index is significant
        if hasattr(self, '_last_uv_update'):
            elapsed = (datetime.now(timezone.utc) - self._last_uv_update).total_seconds()
            # This is simplified - real implementation would integrate properly
        self._last_uv_update = datetime.now(timezone.utc)
        
        # Fall detection
        if bno055.available and len(self._accel_history) >= 10:
            recent_accels = list(self._accel_history)[-10:]
            
            # Check for high-G event followed by low activity
            for accel in recent_accels:
                mag = math.sqrt(sum(a**2 for a in accel))
                if mag > 4.0:  # Threshold for impact
                    # Check if followed by inactivity
                    recent_mags = [math.sqrt(sum(a**2 for a in a2)) for a2 in recent_accels[-5:]]
                    if max(recent_mags) < 0.5:  # Low activity after impact
                        now = datetime.now(timezone.utc)
                        if self._last_fall_time is None or (now - self._last_fall_time).seconds > 10:
                            insights["fall_detected"] = True
                            self._last_fall_time = now
                        break
        
        # Gait analysis
        if self._gait_history:
            avg_activity = sum(self._gait_history) / len(self._gait_history)
            # Normalize to 0-100 scale
            insights["gait_score"] = min(100, avg_activity * 20)
        
        return insights
    
    def get_available_sensors(self) -> List[str]:
        """Return list of available sensor names."""
        return list(self._drivers.keys())
    
    def close(self) -> None:
        """Close I2C bus."""
        if self._bus:
            with suppress(Exception):
                self._bus.close()
                logger.info("Environmental sensors I2C bus closed.")


# ---------------------------------------------------------------------------
# Simulation Mode
# ---------------------------------------------------------------------------
class SimulatedEnvironmentalSensors:
    """Generates realistic fake environmental sensor data."""
    
    def __init__(self, seed: int = 42) -> None:
        self._rng = random.Random(seed)
        self._t0 = time.monotonic()
        self._day_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0)
        
        # Baselines
        self._temp_baseline = 25.0
        self._humidity_baseline = 50.0
        self._pressure_baseline = 1013.0
        self._uv_baseline = 0.0
        self._light_baseline = 500.0
        
        # For fall detection simulation
        self._fall_cooldown = 0
        self._last_fall = 0
        
        # Gait simulation
        self._activity_state = "resting"  # resting, walking, running
        self._activity_timer = 0
    
    def read_all(self) -> Tuple[BME280Data, VEML6070Data, LTR329Data, BNO055Data]:
        """Generate simulated sensor data."""
        elapsed = time.monotonic() - self._t0
        now = datetime.now(timezone.utc)
        
        # Simulate day/night cycle for UV and light
        hour = now.hour + now.minute / 60.0
        is_daytime = 6 <= hour <= 18
        day_progress = (hour - 6) / 12 if is_daytime else 0
        
        # BME280: Temperature with daily cycle
        temp_daily = 5 * math.sin((hour - 6) * math.pi / 12) if is_daytime else -2
        temp_noise = self._rng.gauss(0, 0.5)
        temperature = self._temp_baseline + temp_daily + temp_noise
        
        # Humidity (inverse to temperature)
        humidity = self._humidity_baseline - temp_daily * 2 + self._rng.gauss(0, 3)
        humidity = max(10, min(95, humidity))
        
        # Pressure (slow drift)
        pressure = self._pressure_baseline + 5 * math.sin(elapsed * 0.0001) + self._rng.gauss(0, 1)
        
        # Altitude (derived from pressure)
        altitude = 44330.0 * (1.0 - (pressure / 1013.25) ** 0.1903)
        
        bme280 = BME280Data(
            temperature_c=round(temperature, 2),
            humidity_percent=round(humidity, 1),
            pressure_hpa=round(pressure, 2),
            altitude_m=round(altitude, 1),
            available=True
        )
        
        # VEML6070: UV index (high at midday)
        if is_daytime:
            uv_peak = 8 * math.sin(day_progress * math.pi)
            uv_noise = self._rng.gauss(0, 0.2)
            uv_index = max(0, uv_peak + uv_noise)
        else:
            uv_index = self._rng.gauss(0.1, 0.05)
        
        veml6070 = VEML6070Data(
            uv_raw=int(uv_index * 100),
            uv_index=round(max(0, uv_index), 1),
            available=True
        )
        
        # LTR-329: Light level (lux)
        if is_daytime:
            lux = 10000 * day_progress * (2 - day_progress) if day_progress <= 1 else 0
            lux = max(0, lux + self._rng.gauss(0, lux * 0.1))
        else:
            lux = self._rng.gauss(5, 2)  # Night time
        
        ltr329 = LTR329Data(
            ch0_visible=int(lux * 0.8),
            ch1_infrared=int(lux * 0.2),
            lux=round(max(0, lux), 1),
            available=True
        )
        
        # BNO055: Motion simulation
        # Simulate activity changes
        self._activity_timer += 1
        if self._activity_timer > 100:  # Change activity every ~100 cycles
            self._activity_timer = 0
            activities = ["resting", "slow_walk", "walking", "running"]
            weights = [0.4, 0.3, 0.2, 0.1]
            self._activity_state = self._rng.choices(activities, weights)[0]
        
        # Generate motion based on activity state
        if self._activity_state == "resting":
            accel_mag = self._rng.gauss(0.1, 0.05)
            gyro_noise = 0.1
        elif self._activity_state == "slow_walk":
            accel_mag = self._rng.gauss(0.3, 0.1)
            gyro_noise = 0.5
        elif self._activity_state == "walking":
            accel_mag = self._rng.gauss(0.6, 0.2)
            gyro_noise = 2.0
        else:  # running
            accel_mag = self._rng.gauss(1.5, 0.5)
            gyro_noise = 5.0
        
        # Generate quaternion (orientation)
        # Simulate slow rotation over time
        quat_w = math.cos(elapsed * 0.01)
        quat_x = math.sin(elapsed * 0.01) * 0.3
        quat_y = math.sin(elapsed * 0.02) * 0.2
        quat_z = math.sin(elapsed * 0.015) * 0.1
        # Normalize
        quat_norm = math.sqrt(quat_w**2 + quat_x**2 + quat_y**2 + quat_z**2)
        quaternion = (
            round(quat_w / quat_norm, 4),
            round(quat_x / quat_norm, 4),
            round(quat_y / quat_norm, 4),
            round(quat_z / quat_norm, 4)
        )
        
        # Acceleration with gravity
        accel_x = self._rng.gauss(0, accel_mag * 0.3)
        accel_y = self._rng.gauss(0, accel_mag * 0.3)
        accel_z = 9.8 + self._rng.gauss(0, accel_mag * 0.2)
        acceleration = (round(accel_x, 3), round(accel_y, 3), round(accel_z, 3))
        
        # Gyroscope
        gyro_x = self._rng.gauss(0, gyro_noise)
        gyro_y = self._rng.gauss(0, gyro_noise)
        gyro_z = self._rng.gauss(0, gyro_noise)
        gyroscope = (round(gyro_x, 3), round(gyro_y, 3), round(gyro_z, 3))
        
        # Magnetometer (relatively stable)
        mag_x = 20 + self._rng.gauss(0, 1)
        mag_y = 5 + self._rng.gauss(0, 1)
        mag_z = 40 + self._rng.gauss(0, 1)
        magnetometer = (round(mag_x, 1), round(mag_y, 1), round(mag_z, 1))
        
        # Linear acceleration (without gravity)
        lin_accel_x = accel_x
        lin_accel_y = accel_y
        lin_accel_z = accel_z - 9.8
        
        # Fall detection simulation (rare event)
        fall_detected = False
        if self._fall_cooldown > 0:
            self._fall_cooldown -= 1
        
        if self._rng.random() < 0.001 and self._fall_cooldown == 0:  # 0.1% chance
            # Simulate a fall: high G spike
            lin_accel_x = self._rng.uniform(-5, 5)
            lin_accel_y = self._rng.uniform(-5, 5)
            lin_accel_z = self._rng.uniform(-5, 5)
            fall_detected = True
            self._fall_cooldown = 500  # Cooldown period
        
        linear_acceleration = (round(lin_accel_x, 3), round(lin_accel_y, 3), round(lin_accel_z, 3))
        
        # Gravity vector
        gravity = (0.0, 0.0, 9.8)
        
        bno055 = BNO055Data(
            quaternion=quaternion,
            acceleration=acceleration,
            gyroscope=gyroscope,
            magnetometer=magnetometer,
            linear_acceleration=linear_acceleration,
            gravity=gravity,
            calibration_status=0xFF,  # Fully calibrated in simulation
            available=True
        )
        
        return bme280, veml6070, ltr329, bno055
    
    def calculate_health_insights(self, bme280: BME280Data,
                                   bno055: BNO055Data) -> Dict[str, Any]:
        """Calculate health insights for simulation."""
        # Heat stress based on temp/humidity
        temp_factor = max(0, (bme280.temperature_c - 25) * 2)
        humidity_factor = bme280.humidity_percent * 0.3
        
        # Activity-based addition
        activity_map = {
            "resting": 0,
            "slow_walk": 10,
            "walking": 30,
            "running": 60
        }
        activity_factor = activity_map.get(self._activity_state, 0)
        
        heat_stress = min(100, temp_factor + humidity_factor + activity_factor)
        
        # UV exposure (simulated daily accumulation)
        now = datetime.now(timezone.utc)
        minutes_since_midnight = (now - self._day_start).total_seconds() / 60
        uv_exposure = minutes_since_midnight * 0.1 if bno055.available else 0
        
        # Gait score
        gait_score = activity_factor + self._rng.gauss(0, 5)
        
        # Check for fall in recent acceleration
        fall_detected = False
        if bno055.available:
            lin_mag = math.sqrt(sum(a**2 for a in bno055.linear_acceleration))
            if lin_mag > 4.0:
                fall_detected = True
        
        return {
            "heat_stress_index": heat_stress,
            "uv_exposure_minutes": uv_exposure,
            "fall_detected": fall_detected,
            "gait_score": max(0, min(100, gait_score)),
        }
    
    def get_available_sensors(self) -> List[str]:
        """Return all simulated sensors."""
        return ["bme280", "veml6070", "ltr329", "bno055"]
    
    def close(self) -> None:
        """No-op for simulation."""
        pass


# ---------------------------------------------------------------------------
# Sensor Reader Thread
# ---------------------------------------------------------------------------
def environmental_sensor_reader(
    sensor_source: Any,
    readings: EnvironmentalReadings,
    stop_event: threading.Event,
    update_interval: float = 30.0,
) -> None:
    """Periodically read environmental sensors and update readings."""
    while not stop_event.is_set():
        try:
            # Read all sensors
            bme280_data, veml6070_data, ltr329_data, bno055_data = sensor_source.read_all()
            
            # Calculate health insights
            health_insights = sensor_source.calculate_health_insights(bme280_data, bno055_data)
            
            # Update readings
            readings.update(
                bme280=bme280_data,
                veml6070=veml6070_data,
                ltr329=ltr329_data,
                bno055=bno055_data,
                health_insights=health_insights
            )
            
            logger.debug(
                "Environmental sensors updated: Temp=%.1f°C, Humidity=%.1f%%, "
                "UV=%.1f, Light=%.1flux, Activity=%.1f",
                bme280_data.temperature_c if bme280_data.available else 0,
                bme280_data.humidity_percent if bme280_data.available else 0,
                veml6070_data.uv_index if veml6070_data.available else 0,
                ltr329_data.lux if ltr329_data.available else 0,
                health_insights.get("gait_score", 0),
            )
            
            # Log fall detection
            if health_insights.get("fall_detected"):
                logger.warning("Fall detected!")
                
        except Exception as e:
            logger.error(f"Error in environmental sensor reader: {e}")
            readings.valid = False
        
        # Wait for next interval
        for _ in range(int(update_interval * 10)):
            if stop_event.is_set():
                return
            time.sleep(0.1)


# ---------------------------------------------------------------------------
# HTTP API Handler
# ---------------------------------------------------------------------------
class EnvironmentalAPIHandler(BaseHTTPRequestHandler):
    """Serves environmental sensor data via HTTP API."""
    
    readings: EnvironmentalReadings = None  # type: ignore
    
    def do_GET(self) -> None:
        routes = {
            "/environmental": self._handle_all,
            "/environmental/bme280": self._handle_bme280,
            "/environmental/uv": self._handle_uv,
            "/environmental/light": self._handle_light,
            "/environmental/imu": self._handle_imu,
            "/environmental/health_index": self._handle_health_index,
            "/environmental/health": self._handle_health,
        }
        
        handler = routes.get(self.path, self._handle_404)
        handler()
    
    def _handle_all(self) -> None:
        """GET /environmental - All sensor readings."""
        self._json_response(self.readings.snapshot())
    
    def _handle_bme280(self) -> None:
        """GET /environmental/bme280 - Weather data only."""
        snapshot = self.readings.snapshot()
        response = {
            "bme280": snapshot.get("bme280"),
            "timestamp": snapshot.get("timestamp"),
        }
        self._json_response(response)
    
    def _handle_uv(self) -> None:
        """GET /environmental/uv - UV exposure summary."""
        snapshot = self.readings.snapshot()
        response = {
            "uv": snapshot.get("uv"),
            "health_index": {
                "uv_exposure_minutes": snapshot.get("health_index", {}).get("uv_exposure_minutes"),
            },
            "timestamp": snapshot.get("timestamp"),
        }
        self._json_response(response)
    
    def _handle_light(self) -> None:
        """GET /environmental/light - Light level."""
        snapshot = self.readings.snapshot()
        response = {
            "light": snapshot.get("light"),
            "timestamp": snapshot.get("timestamp"),
        }
        self._json_response(response)
    
    def _handle_imu(self) -> None:
        """GET /environmental/imu - Orientation/motion data."""
        snapshot = self.readings.snapshot()
        response = {
            "imu": snapshot.get("imu"),
            "timestamp": snapshot.get("timestamp"),
        }
        self._json_response(response)
    
    def _handle_health_index(self) -> None:
        """GET /environmental/health_index - Health insights."""
        snapshot = self.readings.snapshot()
        response = {
            "health_index": snapshot.get("health_index"),
            "bme280": {
                "temperature_c": snapshot.get("bme280", {}).get("temperature_c"),
                "humidity_percent": snapshot.get("bme280", {}).get("humidity_percent"),
            } if snapshot.get("bme280") else None,
            "timestamp": snapshot.get("timestamp"),
        }
        self._json_response(response)
    
    def _handle_health(self) -> None:
        """GET /environmental/health - Module health status."""
        snapshot = self.readings.snapshot()
        available_sensors = []
        if snapshot.get("bme280") and snapshot["bme280"].get("available"):
            available_sensors.append("bme280")
        if snapshot.get("uv") and snapshot["uv"].get("available"):
            available_sensors.append("veml6070")
        if snapshot.get("light") and snapshot["light"].get("available"):
            available_sensors.append("ltr329")
        if snapshot.get("imu") and snapshot["imu"].get("available"):
            available_sensors.append("bno055")
        
        response = {
            "status": "ok" if snapshot.get("valid") else "degraded",
            "service": "environmental_sensors",
            "available_sensors": available_sensors,
            "valid": snapshot.get("valid"),
            "timestamp": snapshot.get("timestamp"),
        }
        self._json_response(response)
    
    def _handle_404(self) -> None:
        """Handle unknown paths."""
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
        logger.debug(f"HTTP: {fmt % args}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(description="Dog Agent Environmental Sensors")
    parser.add_argument(
        "--config",
        default=None,
        help="Path to config.yaml",
    )
    parser.add_argument(
        "--simulate",
        action="store_true",
        help="Simulate mode - generate fake sensor data",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=30.0,
        help="Sensor read interval in seconds (default: 30.0)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=9122,
        help="HTTP API port (default: 9122)",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable debug logging",
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
        logger.info(f"Loaded config from {config_path}")
    else:
        logger.warning(f"No config found at {config_path}; using defaults")
        cfg = {
            "enabled": True,
            "i2c_bus": 1,
            "auto_detect": True,
            "poll_interval_sec": 30,
            "api_port": args.port,
        }
    
    if not cfg.get("enabled", True) and not args.simulate:
        logger.info("Environmental sensors module is disabled in config. Exiting.")
        return
    
    # Shared state
    readings = EnvironmentalReadings()
    stop_event = threading.Event()
    
    # Open sensor source
    if args.simulate:
        logger.info("SIMULATE MODE - Using fake environmental sensor data")
        sensor_source = SimulatedEnvironmentalSensors()
    else:
        logger.info("Initializing environmental sensors...")
        try:
            sensor_source = EnvironmentalSensorManager(
                bus_num=cfg["i2c_bus"],
                auto_detect=cfg.get("auto_detect", True),
            )
            available = sensor_source.get_available_sensors()
            logger.info(f"Available sensors: {available}")
            if not available:
                logger.warning("No environmental sensors detected!")
        except ImportError:
            logger.error(
                "smbus2 is not installed. Install with: pip install smbus2\n"
                "Use --simulate mode for development without hardware."
            )
            sys.exit(1)
        except Exception as e:
            logger.error(f"Failed to initialize environmental sensors: {e}")
            logger.error("Use --simulate mode for development without hardware.")
            sys.exit(1)
    
    # Start sensor reader thread
    reader_thread = threading.Thread(
        target=environmental_sensor_reader,
        args=(sensor_source, readings, stop_event, cfg.get("poll_interval_sec", 30)),
        name="env-sensor-reader",
        daemon=True,
    )
    reader_thread.start()
    logger.info(f"Environmental sensor reader started (interval: {cfg.get('poll_interval_sec', 30)} sec)")
    
    # Start HTTP API server
    EnvironmentalAPIHandler.readings = readings
    api_port = cfg.get("api_port", args.port)
    server = HTTPServer(("127.0.0.1", api_port), EnvironmentalAPIHandler)
    
    try:
        server_thread = threading.Thread(
            target=server.serve_forever,
            name="env-api",
            daemon=True,
        )
        server_thread.start()
        logger.info(f"Environmental API server listening on http://127.0.0.1:{api_port}")
        logger.info("Available endpoints:")
        logger.info(f"  - http://127.0.0.1:{api_port}/environmental")
        logger.info(f"  - http://127.0.0.1:{api_port}/environmental/bme280")
        logger.info(f"  - http://127.0.0.1:{api_port}/environmental/uv")
        logger.info(f"  - http://127.0.0.1:{api_port}/environmental/light")
        logger.info(f"  - http://127.0.0.1:{api_port}/environmental/imu")
        logger.info(f"  - http://127.0.0.1:{api_port}/environmental/health_index")
        logger.info(f"  - http://127.0.0.1:{api_port}/environmental/health")
    except OSError as e:
        logger.error(f"Failed to start HTTP server on port {api_port}: {e}")
        stop_event.set()
        sensor_source.close()
        sys.exit(1)
    
    # Graceful shutdown
    def shutdown(signum: int, frame: Any) -> None:
        logger.info(f"Received signal {signum} - shutting down...")
        stop_event.set()
        server.shutdown()
        sensor_source.close()
        logger.info("Environmental sensors stopped.")
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
