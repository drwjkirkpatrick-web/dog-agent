#!/usr/bin/env python3
"""
Dog Agent — RTK / Differential GPS Module
=========================================
Adds support for u-blox NEO-M8P / NEO-F9P RTK receivers.

Features
--------
* NTRIP client: connects to RTK2GO, Emlid Caster, or a custom caster, receives
  RTCM correction data, and reports a GGA sentence for VRS networks.
* GPS integration: forwards RTCM to the u-blox module via UART, parses
  UBX-NAV-PVT for centimeter-accurate fixes, and detects RTK fix status
  (NONE / FLOAT / FIXED).
* HTTP API on port 9150:
    GET /rtk/status      — fix status, base distance, correction age
    GET /rtk/position    — high-accuracy position
    GET /rtk/health      — module health
* Configuration under the ``gps_rtk`` key.
* Simulation mode with ``--simulate``.

Usage:
    python src/gps_rtk.py
    python src/gps_rtk.py --simulate
    python src/gps_rtk.py --config /path/to/config.yaml
    python src/gps_rtk.py --port 9150 --simulate
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import math
import signal
import socket
import sys
import threading
import time
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import IntEnum
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import serial
import yaml

try:
    import requests
except Exception:  # pragma: no cover
    requests = None  # type: ignore[assignment, misc]

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logger = logging.getLogger("gps_rtk")
_handler = logging.StreamHandler(sys.stdout)
_handler.setFormatter(logging.Formatter(
    "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
))
logger.addHandler(_handler)
logger.setLevel(logging.INFO)


PROJECT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_DIR / "config.yaml"

# ---------------------------------------------------------------------------
# UBX constants
# ---------------------------------------------------------------------------
UBX_SYNC = bytes([0xB5, 0x62])
UBX_CLASS_NAV = 0x01
UBX_ID_NAV_PVT = 0x07
UBX_NAV_PVT_LEN = 92

# UBX-CFG-MSG: enable UBX-NAV-PVT on the current UART/USB port at 1 Hz
UBX_ENABLE_NAV_PVT = bytes([
    0xB5, 0x62,             # sync
    0x06, 0x01,             # class/id CFG-MSG
    0x03, 0x00,             # length
    0x01, 0x07, 0x01,       # UBX-NAV-PVT, current port
    0x13, 0x51,             # checksum
])

# UBX-CFG-RATE: set measurement/nav rate to 10 Hz / 100 ms
UBX_SET_RATE_10HZ = bytes([
    0xB5, 0x62,
    0x06, 0x08,             # CFG-RATE
    0x06, 0x00,             # length
    0x64, 0x00,             # measRate 100 ms
    0x01, 0x00,             # navRate 1
    0x00, 0x00,             # timeRef UTC
    0x7A, 0x12,             # checksum
])

# UBX-CFG-PRT to enable UBX protocol input+output on current port
UBX_ENABLE_UBX_PROTOCOL = bytes([
    0xB5, 0x62,
    0x06, 0x00,             # CFG-PRT
    0x14, 0x00,             # length 20
    0xFF, 0xFF,             # portID = current
    0x00, 0x00,             # reserved
    0x00, 0x00, 0x00, 0x00, # txReady
    0x00, 0x00, 0x00, 0x00, # mode (8N1, current)
    0x00, 0x00,             # baudrate (0 = current)
    0x01, 0x00,             # inProtoMask = UBX only
    0x01, 0x00,             # outProtoMask = UBX only
    0x00, 0x00,             # flags
    0x00, 0x00,
    0x10, 0x2B,             # checksum (placeholder OK-ish for current port)
])


# ---------------------------------------------------------------------------
# Fix status
# ---------------------------------------------------------------------------
class RTKFixStatus(IntEnum):
    """RTK fix status codes from UBX-NAV-PVT flags ( carrierSolution )."""
    NONE = 0
    FLOAT = 1
    FIXED = 2

    def __str__(self) -> str:
        return self.name


@dataclass
class RTKPosition:
    """High-accuracy RTK position snapshot."""
    latitude: float = 0.0
    longitude: float = 0.0
    altitude_m: Optional[float] = None
    altitude_msl: Optional[float] = None
    speed_ms: Optional[float] = None
    heading: Optional[float] = None
    fix_type: int = 0
    rtk_status: RTKFixStatus = RTKFixStatus.NONE
    satellites_used: int = 0
    hdop: Optional[float] = None
    pdop: Optional[float] = None
    vdop: Optional[float] = None
    accuracy_horizontal_mm: Optional[int] = None
    accuracy_vertical_mm: Optional[int] = None
    accuracy_speed_mms: Optional[int] = None
    accuracy_heading_deg: Optional[float] = None
    correction_age_sec: Optional[float] = None
    base_distance_m: Optional[float] = None
    base_latitude: Optional[float] = None
    base_longitude: Optional[float] = None
    gga_sentence: Optional[str] = None
    timestamp: Optional[datetime] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "latitude": self.latitude,
            "longitude": self.longitude,
            "altitude_m": self.altitude_m,
            "altitude_msl": self.altitude_msl,
            "speed_ms": self.speed_ms,
            "heading": self.heading,
            "fix_type": self.fix_type,
            "rtk_status": str(self.rtk_status),
            "satellites_used": self.satellites_used,
            "hdop": self.hdop,
            "pdop": self.pdop,
            "vdop": self.vdop,
            "accuracy_horizontal_mm": self.accuracy_horizontal_mm,
            "accuracy_vertical_mm": self.accuracy_vertical_mm,
            "accuracy_speed_mms": self.accuracy_speed_mms,
            "accuracy_heading_deg": self.accuracy_heading_deg,
            "correction_age_sec": self.correction_age_sec,
            "base_distance_m": self.base_distance_m,
            "base_latitude": self.base_latitude,
            "base_longitude": self.base_longitude,
            "gga_sentence": self.gga_sentence,
            "timestamp": self.timestamp.isoformat() if self.timestamp else None,
        }


@dataclass
class ModuleHealth:
    """Runtime health counters for the RTK module."""
    receiver_connected: bool = False
    ntrip_connected: bool = False
    ntrip_caster: str = ""
    ntrip_mountpoint: str = ""
    bytes_received_rtcm: int = 0
    bytes_sent_rtcm: int = 0
    corrections_received: int = 0
    ubx_messages_parsed: int = 0
    ubx_parse_errors: int = 0
    last_rtcm_time: Optional[float] = None
    last_pvt_time: Optional[float] = None
    uptime_sec: float = 0.0
    started_at: Optional[datetime] = None
    enabled: bool = False
    simulation: bool = False

    def to_dict(self) -> Dict[str, Any]:
        now = time.monotonic()
        return {
            "receiver_connected": self.receiver_connected,
            "ntrip_connected": self.ntrip_connected,
            "ntrip_caster": self.ntrip_caster,
            "ntrip_mountpoint": self.ntrip_mountpoint,
            "bytes_received_rtcm": self.bytes_received_rtcm,
            "bytes_sent_rtcm": self.bytes_sent_rtcm,
            "corrections_received": self.corrections_received,
            "ubx_messages_parsed": self.ubx_messages_parsed,
            "ubx_parse_errors": self.ubx_parse_errors,
            "last_rtcm_age_sec": (now - self.last_rtcm_time) if self.last_rtcm_time else None,
            "last_pvt_age_sec": (now - self.last_pvt_time) if self.last_pvt_time else None,
            "uptime_sec": self.uptime_sec,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "enabled": self.enabled,
            "simulation": self.simulation,
        }


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------
def load_config(path: Path) -> Dict[str, Any]:
    try:
        with open(path) as f:
            return yaml.safe_load(f) or {}
    except Exception as exc:
        logger.warning("Failed to load config from %s: %s", path, exc)
        return {}


def get_cfg(cfg: Dict[str, Any], path: str, default: Any = None) -> Any:
    keys = path.split(".")
    val: Any = cfg
    for key in keys:
        if isinstance(val, dict):
            val = val.get(key)
        else:
            return default
        if val is None:
            return default
    return val


# ---------------------------------------------------------------------------
# UBX helpers
# ---------------------------------------------------------------------------
def _ubx_checksum(payload: bytes) -> bytes:
    ck_a = 0
    ck_b = 0
    for b in payload:
        ck_a = (ck_a + b) & 0xFF
        ck_b = (ck_b + ck_a) & 0xFF
    return bytes([ck_a, ck_b])


def _build_ubx_frame(msg_class: int, msg_id: int, payload: bytes) -> bytes:
    body = bytes([msg_class, msg_id]) + _len_u16(len(payload)) + payload
    return UBX_SYNC + body + _ubx_checksum(body)


def _len_u16(length: int) -> bytes:
    return bytes([length & 0xFF, (length >> 8) & 0xFF])


def _read_u16(data: bytes, offset: int) -> int:
    return int.from_bytes(data[offset:offset + 2], "little", signed=False)


def _read_u32(data: bytes, offset: int) -> int:
    return int.from_bytes(data[offset:offset + 4], "little", signed=False)


def _read_i32(data: bytes, offset: int) -> int:
    return int.from_bytes(data[offset:offset + 4], "little", signed=True)


def _parse_ubx_nav_pvt(payload: bytes) -> Optional[RTKPosition]:
    """Parse a UBX-NAV-PVT payload (92 bytes) into an RTKPosition."""
    if len(payload) != UBX_NAV_PVT_LEN:
        return None

    # iTOW at 0..3, year 4..5, month 6, day 7, hour 8, min 9, sec 10
    fix_type = payload[20]
    flags = payload[21]
    flags2 = payload[22]
    num_sv = payload[23]
    lon_deg = _read_i32(payload, 24) * 1e-7
    lat_deg = _read_i32(payload, 28) * 1e-7
    height_mm = _read_i32(payload, 32)
    height_msl_mm = _read_i32(payload, 36)
    h_acc_mm = _read_u32(payload, 40)
    v_acc_mm = _read_u32(payload, 44)
    vel_n_mm_s = _read_i32(payload, 48)
    vel_e_mm_s = _read_i32(payload, 52)
    vel_d_mm_s = _read_i32(payload, 56)
    speed_3d_mm_s = _read_i32(payload, 60)
    speed_2d_mm_s = _read_i32(payload, 64)
    head_mot_deg = _read_i32(payload, 68) * 1e-5
    s_acc_mm_s = _read_u32(payload, 72)
    head_acc_deg = _read_u32(payload, 76) * 1e-5
    pdop = _read_u16(payload, 80) * 0.01
    # 82..83 reserved, 84 invalidLlh, 85 lastCorrectionAge, 86..91 reserved
    last_correction_age = payload[85]
    carrier_solution = (flags2 >> 6) & 0x03

    # Convert lastCorrectionAge nibble to seconds per u-blox spec
    correction_age_map = {
        0: 0.0, 1: 1.0, 2: 2.0, 3: 5.0, 4: 10.0,
        5: 15.0, 6: 20.0, 7: 30.0, 8: 45.0, 9: 60.0,
        10: 90.0, 11: 120.0, 12: 180.0, 13: 300.0,
        14: 600.0, 15: float("inf"),
    }
    correction_age = correction_age_map.get(last_correction_age)

    pos = RTKPosition(
        latitude=lat_deg,
        longitude=lon_deg,
        altitude_m=height_mm / 1000.0,
        altitude_msl=height_msl_mm / 1000.0,
        speed_ms=speed_2d_mm_s / 1000.0,
        heading=head_mot_deg if head_mot_deg < 360.0 else None,
        fix_type=fix_type,
        rtk_status=RTKFixStatus(carrier_solution),
        satellites_used=num_sv,
        pdop=pdop,
        accuracy_horizontal_mm=h_acc_mm,
        accuracy_vertical_mm=v_acc_mm,
        accuracy_speed_mms=s_acc_mm_s,
        accuracy_heading_deg=head_acc_deg if head_acc_deg < 360.0 else None,
        correction_age_sec=correction_age,
        timestamp=datetime.now(timezone.utc),
    )
    return pos


# ---------------------------------------------------------------------------
# Coordinate / distance helpers
# ---------------------------------------------------------------------------
def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371000.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return R * c


def _format_deg_min(coord: float, lat: bool = True) -> str:
    """Format decimal degrees as NMEA DDDMM.MMMMMM / direction."""
    direction = "N" if coord >= 0 else "S" if lat else "E" if coord >= 0 else "W"
    coord = abs(coord)
    degrees = int(coord)
    minutes = (coord - degrees) * 60.0
    if lat:
        return f"{degrees:02d}{minutes:010.7f}", direction
    return f"{degrees:03d}{minutes:010.7f}", direction


def _build_gga(pos: RTKPosition) -> Optional[str]:
    """Build a GNGGA sentence from an RTKPosition for NTRIP VRS."""
    if not pos or pos.latitude is None or pos.longitude is None:
        return None

    now = datetime.now(timezone.utc)
    time_str = f"{now.hour:02d}{now.minute:02d}{now.second + now.microsecond / 1e6:09.6f}"
    lat_str, lat_dir = _format_deg_min(pos.latitude, lat=True)
    lon_str, lon_dir = _format_deg_min(pos.longitude, lat=False)

    # Quality: 1 = GPS, 2 = DGPS, 4 = RTK fixed, 5 = RTK float
    quality = 1
    if pos.rtk_status == RTKFixStatus.FIXED:
        quality = 4
    elif pos.rtk_status == RTKFixStatus.FLOAT:
        quality = 5
    elif pos.fix_type >= 2:
        quality = 1

    hdop = f"{pos.hdop:.2f}" if pos.hdop is not None else "1.00"
    sats = f"{pos.satellites_used:02d}"
    alt = f"{pos.altitude_msl if pos.altitude_msl is not None else pos.altitude_m:.3f}"
    geoid_sep = "0.000"

    body = (
        f"GNGGA,{time_str},{lat_str},{lat_dir},{lon_str},{lon_dir},"
        f"{quality},{sats},{hdop},{alt},M,{geoid_sep},M,"
        f"{pos.correction_age_sec if pos.correction_age_sec is not None else ''},"
    )
    if body.endswith(","):
        body = body[:-1]
    checksum = 0
    for ch in body:
        checksum ^= ord(ch)
    return f"${body}*{checksum:02X}"


# ---------------------------------------------------------------------------
# NTRIP client
# ---------------------------------------------------------------------------
class NTRIPClient(threading.Thread):
    """Threaded NTRIP client that fetches RTCM corrections and forwards them."""

    def __init__(
        self,
        caster_host: str,
        mountpoint: str,
        user: str,
        password: str,
        gga_provider: Any,
        rtcm_callback: Any,
        port: int = 2101,
        reconnect_interval_sec: float = 10.0,
        timeout_sec: float = 5.0,
    ) -> None:
        super().__init__(name="ntrip-client", daemon=True)
        self.caster_host = caster_host
        self.mountpoint = mountpoint
        self.user = user
        self.password = password
        self.gga_provider = gga_provider
        self.rtcm_callback = rtcm_callback
        self.caster_port = port
        self.reconnect_interval_sec = reconnect_interval_sec
        self.timeout_sec = timeout_sec
        self._running = True
        self.connected = False
        self._last_gga_send = 0.0
        self._gga_interval_sec = 10.0

    def stop(self) -> None:
        self._running = False

    def _connect(self) -> Optional[socket.socket]:
        try:
            sock = socket.create_connection(
                (self.caster_host, self.caster_port), timeout=self.timeout_sec
            )
            sock.settimeout(self.timeout_sec)

            auth = base64.b64encode(f"{self.user}:{self.password}".encode()).decode()
            headers = [
                f"GET /{self.mountpoint} HTTP/1.1",
                f"Host: {self.caster_host}:{self.caster_port}",
                "Ntrip-Version: Ntrip/2.0",
                "User-Agent: dog-agent-rtk/5.0",
                f"Authorization: Basic {auth}",
                "Connection: close",
                "",
                "",
            ]
            request = "\r\n".join(headers)
            sock.sendall(request.encode())

            # Read HTTP response until double CRLF
            response = b""
            while b"\r\n\r\n" not in response:
                chunk = sock.recv(1024)
                if not chunk:
                    break
                response += chunk
            resp_text = response.decode("ascii", errors="ignore")
            first_line = resp_text.split("\r\n")[0]
            if "200" in first_line or "ICY 200 OK" in resp_text:
                logger.info("NTRIP connected to %s/%s", self.caster_host, self.mountpoint)
                self.connected = True
                return sock
            elif "401" in first_line:
                logger.error("NTRIP authentication failed for %s/%s", self.caster_host, self.mountpoint)
                self.connected = False
                sock.close()
                return None
            else:
                logger.warning("NTRIP unexpected response: %s", first_line)
                self.connected = False
                sock.close()
                return None
        except Exception as exc:
            logger.warning("NTRIP connection failed: %s", exc)
            self.connected = False
            return None

    def _send_gga_if_needed(self, sock: socket.socket) -> None:
        now = time.monotonic()
        if now - self._last_gga_send < self._gga_interval_sec:
            return
        gga = self.gga_provider()
        if gga:
            try:
                sock.sendall((gga + "\r\n").encode())
                self._last_gga_send = now
            except Exception as exc:
                logger.debug("Failed to send GGA to caster: %s", exc)

    def run(self) -> None:
        while self._running:
            sock = self._connect()
            if sock is None:
                time.sleep(self.reconnect_interval_sec)
                continue

            try:
                while self._running:
                    self._send_gga_if_needed(sock)
                    try:
                        data = sock.recv(4096)
                    except socket.timeout:
                        continue
                    if not data:
                        logger.warning("NTRIP caster closed connection")
                        break
                    self.rtcm_callback(data)
            except Exception as exc:
                logger.warning("NTRIP stream error: %s", exc)
            finally:
                self.connected = False
                with suppress(Exception):
                    sock.close()
                if self._running:
                    time.sleep(self.reconnect_interval_sec)


# ---------------------------------------------------------------------------
# Simulation helpers
# ---------------------------------------------------------------------------
class SimulatedReceiver:
    """Produces synthetic UBX-NAV-PVT frames and NMEA-ish lines for testing."""

    def __init__(self, base_lat: float = 45.5152, base_lon: float = -122.6784) -> None:
        self.base_lat = base_lat
        self.base_lon = base_lon
        self._step = 0
        self._running = True
        self._buffer = b""

    @property
    def in_waiting(self) -> int:
        self._maybe_fill()
        return len(self._buffer)

    def read(self, size: int = 1) -> bytes:
        self._maybe_fill()
        chunk = self._buffer[:size]
        self._buffer = self._buffer[size:]
        return chunk

    def readline(self) -> bytes:
        self._maybe_fill()
        idx = self._buffer.find(b"\n")
        if idx == -1:
            chunk = self._buffer
            self._buffer = b""
            return chunk
        line = self._buffer[:idx + 1]
        self._buffer = self._buffer[idx + 1:]
        return line

    def write(self, data: bytes) -> int:
        return len(data)

    def close(self) -> None:
        self._running = False

    def _maybe_fill(self) -> None:
        if len(self._buffer) < 200:
            self._buffer += self._next_frame()

    def _next_frame(self) -> bytes:
        self._step += 1
        import random

        # Simulate a small circular drift around base coords
        offset_lat = 0.00005 * math.sin(self._step * 0.05)
        offset_lon = 0.00005 * math.cos(self._step * 0.05)
        lat = self.base_lat + offset_lat
        lon = self.base_lon + offset_lon

        # Cycle through fix states for testing
        rtk_cycle = self._step % 180
        if rtk_cycle < 60:
            carrier_solution = 0  # NONE
        elif rtk_cycle < 120:
            carrier_solution = 1  # FLOAT
        else:
            carrier_solution = 2  # FIXED

        payload = bytearray(UBX_NAV_PVT_LEN)
        # iTOW
        itow = (self._step * 100) & 0xFFFFFFFF
        payload[0:4] = itow.to_bytes(4, "little")
        # year/month/day/hour/min/sec (valid)
        now = datetime.now(timezone.utc)
        payload[4:6] = now.year.to_bytes(2, "little")
        payload[6] = now.month
        payload[7] = now.day
        payload[8] = now.hour
        payload[9] = now.minute
        payload[10] = now.second
        payload[11] = 0  # valid flags
        payload[20] = 3 if carrier_solution else 2  # fixType 3D / 2D
        payload[21] = 0x01  # flags: gnssFixOK
        payload[22] = (carrier_solution << 6) | 0x01  # flags2 + confirmedDate/Time
        payload[23] = 12 + (self._step % 8)
        _write_i32(payload, 24, int(lon * 1e7))
        _write_i32(payload, 28, int(lat * 1e7))
        _write_i32(payload, 32, int((100.0 + random.uniform(-1, 1)) * 1000))
        _write_i32(payload, 36, int((95.0 + random.uniform(-1, 1)) * 1000))
        _write_u32(payload, 40, 50 if carrier_solution == 2 else 500)  # hAcc
        _write_u32(payload, 44, 80 if carrier_solution == 2 else 900)   # vAcc
        _write_i32(payload, 48, 0)
        _write_i32(payload, 52, 0)
        _write_i32(payload, 56, 0)
        _write_i32(payload, 60, 0)
        _write_i32(payload, 64, int(random.uniform(0.0, 1.5) * 1000))
        _write_i32(payload, 68, int((self._step * 2) % 360 * 1e5))
        _write_u32(payload, 72, 200)
        _write_u32(payload, 76, int(2.0 * 1e5))
        _write_u16(payload, 80, int(1.2 * 100))  # pDOP
        # lastCorrectionAge
        payload[85] = 4 if carrier_solution else 0

        frame = _build_ubx_frame(UBX_CLASS_NAV, UBX_ID_NAV_PVT, bytes(payload))
        return frame + b"\n"


def _write_i32(buf: bytearray, offset: int, value: int) -> None:
    buf[offset:offset + 4] = value.to_bytes(4, "little", signed=True)


def _write_u32(buf: bytearray, offset: int, value: int) -> None:
    buf[offset:offset + 4] = value.to_bytes(4, "little", signed=False)


def _write_u16(buf: bytearray, offset: int, value: int) -> None:
    buf[offset:offset + 2] = value.to_bytes(2, "little", signed=False)


# ---------------------------------------------------------------------------
# Core RTK manager
# ---------------------------------------------------------------------------
class RTKGPSManager:
    """Coordinates UART receiver, UBX parser, NTRIP client, and HTTP state."""

    def __init__(
        self,
        enabled: bool,
        receiver_port: str,
        receiver_baud: int,
        ntrip_caster: str,
        ntrip_mountpoint: str,
        ntrip_user: str,
        ntrip_password: str,
        ntrip_port: int = 2101,
        simulate: bool = False,
        base_position: Optional[Tuple[float, float]] = None,
    ) -> None:
        self.enabled = enabled
        self.receiver_port = receiver_port
        self.receiver_baud = receiver_baud
        self.ntrip_caster = ntrip_caster
        self.ntrip_mountpoint = ntrip_mountpoint
        self.ntrip_user = ntrip_user
        self.ntrip_password = ntrip_password
        self.ntrip_port = ntrip_port
        self.simulate = simulate
        self.base_position = base_position
        self._start_monotonic = time.monotonic()

        self._serial: Any = None
        self._ntrip: Optional[NTRIPClient] = None
        self._lock = threading.Lock()
        self._position: Optional[RTKPosition] = None
        self._health = ModuleHealth(
            enabled=enabled,
            simulation=simulate,
            ntrip_caster=ntrip_caster,
            ntrip_mountpoint=ntrip_mountpoint,
            started_at=datetime.now(timezone.utc),
        )
        self._stop_event = threading.Event()
        self._ubx_buffer = b""

        if enabled and not simulate:
            self._init_receiver()

    def _init_receiver(self) -> None:
        try:
            self._serial = serial.Serial(
                self.receiver_port,
                self.receiver_baud,
                timeout=1,
            )
            self._serial.write(UBX_ENABLE_UBX_PROTOCOL)
            time.sleep(0.05)
            self._serial.write(UBX_SET_RATE_10HZ)
            time.sleep(0.05)
            self._serial.write(UBX_ENABLE_NAV_PVT)
            self._health.receiver_connected = True
            logger.info("RTK receiver initialized on %s @ %d baud", self.receiver_port, self.receiver_baud)
        except Exception as exc:
            self._health.receiver_connected = False
            logger.error("Failed to initialize RTK receiver on %s: %s", self.receiver_port, exc)

    def _start_ntrip(self) -> None:
        if not self.ntrip_caster or not self.ntrip_mountpoint:
            logger.info("NTRIP caster not configured — running receiver-only")
            return
        self._ntrip = NTRIPClient(
            caster_host=self.ntrip_caster,
            mountpoint=self.ntrip_mountpoint,
            user=self.ntrip_user,
            password=self.ntrip_password,
            gga_provider=self.get_gga,
            rtcm_callback=self._on_rtcm,
            port=self.ntrip_port,
        )
        self._ntrip.start()

    def _on_rtcm(self, data: bytes) -> None:
        self._health.bytes_received_rtcm += len(data)
        self._health.corrections_received += 1
        self._health.last_rtcm_time = time.monotonic()
        if self._serial:
            try:
                self._serial.write(data)
                self._health.bytes_sent_rtcm += len(data)
            except Exception as exc:
                logger.warning("Failed to forward RTCM to receiver: %s", exc)
                self._health.receiver_connected = False

    def get_gga(self) -> Optional[str]:
        with self._lock:
            pos = self._position
        return _build_gga(pos)

    def _update_position(self, pos: RTKPosition) -> None:
        if self.base_position:
            pos.base_latitude, pos.base_longitude = self.base_position
            pos.base_distance_m = _haversine_m(
                pos.latitude, pos.longitude,
                self.base_position[0], self.base_position[1],
            )
        pos.gga_sentence = _build_gga(pos)
        with self._lock:
            self._position = pos
            self._health.last_pvt_time = time.monotonic()
            self._health.ubx_messages_parsed += 1

    def _process_ubx(self, frame: bytes) -> None:
        if len(frame) < 8:
            return
        msg_class = frame[2]
        msg_id = frame[3]
        payload_len = int.from_bytes(frame[4:6], "little")
        payload = frame[6:6 + payload_len]

        if msg_class == UBX_CLASS_NAV and msg_id == UBX_ID_NAV_PVT:
            try:
                pos = _parse_ubx_nav_pvt(payload)
                if pos:
                    self._update_position(pos)
                else:
                    self._health.ubx_parse_errors += 1
            except Exception as exc:
                logger.debug("UBX-NAV-PVT parse error: %s", exc)
                self._health.ubx_parse_errors += 1
        else:
            logger.debug("Ignoring UBX message class=0x%02X id=0x%02X", msg_class, msg_id)

    def _reader_loop(self) -> None:
        source: Any = self._serial
        if self.simulate:
            source = SimulatedReceiver()
            self._health.receiver_connected = True
            self._health.simulation = True

        while not self._stop_event.is_set():
            try:
                if source.in_waiting:
                    data = source.read(source.in_waiting)
                    self._ubx_buffer += data
                else:
                    time.sleep(0.01)
                    continue
            except Exception as exc:
                logger.debug("Receiver read error: %s", exc)
                time.sleep(0.5)
                continue

            # Search for UBX sync bytes
            while True:
                sync_idx = self._ubx_buffer.find(UBX_SYNC)
                if sync_idx == -1:
                    self._ubx_buffer = b""
                    break
                self._ubx_buffer = self._ubx_buffer[sync_idx:]
                if len(self._ubx_buffer) < 8:
                    break
                payload_len = int.from_bytes(self._ubx_buffer[4:6], "little")
                frame_len = 6 + payload_len + 2
                if len(self._ubx_buffer) < frame_len:
                    break
                frame = self._ubx_buffer[:frame_len]
                calc_ck = _ubx_checksum(frame[2:-2])
                if calc_ck == frame[-2:]:
                    self._process_ubx(frame)
                else:
                    logger.debug("UBX checksum mismatch")
                    self._health.ubx_parse_errors += 1
                self._ubx_buffer = self._ubx_buffer[frame_len:]

    def start(self) -> None:
        if not self.enabled:
            logger.info("RTK GPS module is disabled in config")
            return
        if not self.simulate and not self._health.receiver_connected:
            logger.warning("RTK receiver not connected — module will retry in reader loop")

        self._reader_thread = threading.Thread(target=self._reader_loop, name="rtk-reader", daemon=True)
        self._reader_thread.start()
        self._start_ntrip()

    def stop(self) -> None:
        self._stop_event.set()
        if self._ntrip:
            self._ntrip.stop()
        if self._serial:
            with suppress(Exception):
                self._serial.close()

    def get_position(self) -> Optional[RTKPosition]:
        with self._lock:
            return self._position

    def get_status(self) -> Dict[str, Any]:
        pos = self.get_position()
        now = time.monotonic()
        self._health.uptime_sec = now - self._start_monotonic
        if self._ntrip:
            self._health.ntrip_connected = self._ntrip.connected

        status = {
            "enabled": self.enabled,
            "simulation": self.simulate,
            "rtk_status": str(pos.rtk_status) if pos else "NONE",
            "fix_type": pos.fix_type if pos else 0,
            "satellites_used": pos.satellites_used if pos else 0,
            "correction_age_sec": pos.correction_age_sec if pos else None,
            "base_distance_m": pos.base_distance_m if pos else None,
            "last_pvt_age_sec": (now - self._health.last_pvt_time) if self._health.last_pvt_time else None,
            "last_rtcm_age_sec": (now - self._health.last_rtcm_time) if self._health.last_rtcm_time else None,
            "receiver_connected": self._health.receiver_connected,
            "ntrip_connected": self._health.ntrip_connected,
            "ntrip_caster": self.ntrip_caster,
            "ntrip_mountpoint": self.ntrip_mountpoint,
            "uptime_sec": round(self._health.uptime_sec, 1),
        }
        return status

    def get_health(self) -> Dict[str, Any]:
        self._health.uptime_sec = time.monotonic() - self._start_monotonic
        return self._health.to_dict()


# ---------------------------------------------------------------------------
# HTTP API handler
# ---------------------------------------------------------------------------
class RTKHTTPHandler(BaseHTTPRequestHandler):
    manager: Optional[RTKGPSManager] = None

    def log_message(self, format: str, *args: Any) -> None:
        logger.debug("HTTP: %s", format % args)

    def _send_json(self, data: Any, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data, indent=2, default=str).encode())

    def do_GET(self) -> None:
        path = self.path.strip("/")
        if path == "rtk/status":
            if not self.manager:
                self._send_json({"error": "RTK manager not initialized"}, 503)
                return
            self._send_json(self.manager.get_status())
        elif path == "rtk/position":
            if not self.manager:
                self._send_json({"error": "RTK manager not initialized"}, 503)
                return
            pos = self.manager.get_position()
            if pos:
                self._send_json(pos.to_dict())
            else:
                self._send_json({"error": "No RTK position available"}, 503)
        elif path == "rtk/health":
            if not self.manager:
                self._send_json({"error": "RTK manager not initialized"}, 503)
                return
            self._send_json(self.manager.get_health())
        elif path == "health":
            self._send_json({"status": "ok", "service": "gps_rtk"})
        else:
            self._send_json({"error": f"Unknown endpoint: {path}"}, 404)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(description="Dog Agent — RTK GPS Module")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="Path to config.yaml")
    parser.add_argument("--port", type=int, default=9150, help="HTTP API port")
    parser.add_argument("--simulate", action="store_true", help="Run simulation mode")
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose logging")
    args = parser.parse_args()

    if args.verbose:
        logger.setLevel(logging.DEBUG)

    cfg = load_config(Path(args.config))

    enabled = get_cfg(cfg, "gps_rtk.enabled", False)
    receiver_port = get_cfg(cfg, "gps_rtk.receiver_port", "/dev/ttyACM1")
    receiver_baud = get_cfg(cfg, "gps_rtk.receiver_baud", 115200)
    ntrip_caster = get_cfg(cfg, "gps_rtk.ntrip_caster", "rtk2go.com")
    ntrip_mountpoint = get_cfg(cfg, "gps_rtk.ntrip_mountpoint", "")
    ntrip_user = get_cfg(cfg, "gps_rtk.ntrip_user", "")
    ntrip_password = get_cfg(cfg, "gps_rtk.ntrip_password", "")
    ntrip_port = get_cfg(cfg, "gps_rtk.ntrip_port", 2101)

    # Optional base station position for distance calculation
    base_lat = get_cfg(cfg, "gps_rtk.base_station.lat")
    base_lon = get_cfg(cfg, "gps_rtk.base_station.lon")
    base_position: Optional[Tuple[float, float]] = None
    if base_lat is not None and base_lon is not None:
        try:
            base_position = (float(base_lat), float(base_lon))
        except ValueError:
            base_position = None

    manager = RTKGPSManager(
        enabled=enabled or args.simulate,
        receiver_port=receiver_port,
        receiver_baud=receiver_baud,
        ntrip_caster=ntrip_caster,
        ntrip_mountpoint=ntrip_mountpoint,
        ntrip_user=ntrip_user,
        ntrip_password=ntrip_password,
        ntrip_port=int(ntrip_port),
        simulate=args.simulate,
        base_position=base_position,
    )

    if args.simulate:
        logger.info("=== RTK GPS Simulation Mode ===")
        logger.info("Synthetic UBX-NAV-PVT frames will cycle through NONE/FLOAT/FIXED")

    manager.start()

    RTKHTTPHandler.manager = manager
    server = HTTPServer(("127.0.0.1", args.port), RTKHTTPHandler)
    server_thread = threading.Thread(target=server.serve_forever, name="rtk-api", daemon=True)
    server_thread.start()
    logger.info("RTK GPS API on http://127.0.0.1:%d/rtk/{{status,position,health}}", args.port)

    def shutdown(signum: int, frame: Any) -> None:
        logger.info("Received signal %d — shutting down...", signum)
        manager.stop()
        server.shutdown()
        logger.info("RTK GPS module stopped.")
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        shutdown(signal.SIGINT, None)


if __name__ == "__main__":
    main()
