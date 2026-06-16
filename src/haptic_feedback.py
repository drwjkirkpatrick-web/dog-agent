#!/usr/bin/env python3
"""
Dog Agent — Haptic Feedback Module
==================================
Vibration motor control for silent owner notifications and dog calming.

Features:
  - PWM-controlled vibration motor
  - Multiple vibration patterns
  - Silent owner alerts
  - Calming vibration patterns for anxious dogs

Usage:
    python src/haptic_feedback.py
    python src/haptic_feedback.py --simulate
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
from enum import Enum

import yaml

# Logging
logger = logging.getLogger("haptic")
handler = logging.StreamHandler(sys.stdout)
handler.setFormatter(logging.Formatter(
    "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
))
logger.addHandler(handler)
logger.setLevel(logging.INFO)

PROJECT_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_DIR / "config.yaml"


class VibrationPattern(Enum):
    """Pre-defined vibration patterns."""
    SINGLE_PULSE = "single"      # One short vibration
    DOUBLE_PULSE = "double"      # Two short vibrations
    TRIPLE_PULSE = "triple"      # Three short vibrations
    SOS = "sos"                  # ... --- ... (emergency)
    HEARTBEAT = "heartbeat"      # Ba-bump, ba-bump
    WAVE = "wave"                # Gentle rise and fall
    CALMING = "calming"          # Slow, gentle pulses
    URGENT = "urgent"            # Rapid, strong pulses
    CONTINUOUS = "continuous"  # Constant vibration


PATTERNS = {
    VibrationPattern.SINGLE_PULSE: [(0.2, 0.0)],
    VibrationPattern.DOUBLE_PULSE: [(0.1, 0.1), (0.1, 0.0)],
    VibrationPattern.TRIPLE_PULSE: [(0.1, 0.1), (0.1, 0.1), (0.1, 0.0)],
    VibrationPattern.SOS: [
        (0.1, 0.1), (0.1, 0.1), (0.1, 0.3),  # ...
        (0.3, 0.1), (0.3, 0.1), (0.3, 0.3),  # ---
        (0.1, 0.1), (0.1, 0.1), (0.1, 0.0),  # ...
    ],
    VibrationPattern.HEARTBEAT: [(0.1, 0.1), (0.1, 0.5)],
    VibrationPattern.WAVE: [(0.3, 0.2), (0.5, 0.2), (0.3, 0.0)],
    VibrationPattern.CALMING: [(0.5, 1.0), (0.5, 1.0), (0.5, 2.0)],
    VibrationPattern.URGENT: [(0.05, 0.05)] * 10,
    VibrationPattern.CONTINUOUS: [(2.0, 0.0)],
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


class HapticController:
    """Controls vibration motor via GPIO."""
    
    def __init__(self):
        self.enabled = get_cfg("haptic.enabled", True)
        self.gpio_pin = get_cfg("haptic.gpio_pin", 26)
        self.default_intensity = get_cfg("haptic.default_intensity", 0.8)
        
        self._pwm = None
        self._active = False
        self._lock = threading.Lock()
        
        if self.enabled:
            self._init_gpio()
    
    def _init_gpio(self) -> None:
        try:
            import RPi.GPIO as GPIO
            GPIO.setmode(GPIO.BCM)
            GPIO.setup(self.gpio_pin, GPIO.OUT)
            self._pwm = GPIO.PWM(self.gpio_pin, 100)  # 100 Hz
            self._pwm.start(0)
            logger.info(f"Haptic motor initialized on GPIO {self.gpio_pin}")
        except ImportError:
            logger.warning("RPi.GPIO not available, using simulation")
        except Exception as e:
            logger.warning(f"Failed to initialize GPIO: {e}")
    
    def vibrate(self, duration_sec: float, intensity: float = None) -> None:
        """Single vibration pulse."""
        intensity = intensity or self.default_intensity
        
        with self._lock:
            if self._active:
                return  # Don't interrupt
            self._active = True
            
            try:
                if self._pwm:
                    self._pwm.ChangeDutyCycle(intensity * 100)
                time.sleep(duration_sec)
                if self._pwm:
                    self._pwm.ChangeDutyCycle(0)
            finally:
                self._active = False
    
    def play_pattern(self, pattern: VibrationPattern, intensity: float = None) -> None:
        """Play a vibration pattern."""
        intensity = intensity or self.default_intensity
        sequence = PATTERNS.get(pattern, [])
        
        with self._lock:
            if self._active:
                return
            self._active = True
            
            try:
                for on_time, off_time in sequence:
                    if self._pwm:
                        self._pwm.ChangeDutyCycle(intensity * 100)
                    time.sleep(on_time)
                    if self._pwm:
                        self._pwm.ChangeDutyCycle(0)
                    time.sleep(off_time)
            finally:
                self._active = False
    
    def owner_alert(self, urgency: str = "medium") -> None:
        """Alert owner via vibration."""
        patterns = {
            "low": VibrationPattern.SINGLE_PULSE,
            "medium": VibrationPattern.DOUBLE_PULSE,
            "high": VibrationPattern.TRIPLE_PULSE,
            "emergency": VibrationPattern.SOS,
        }
        pattern = patterns.get(urgency, VibrationPattern.SINGLE_PULSE)
        self.play_pattern(pattern)
    
    def calming_mode(self, duration_sec: float = 30) -> None:
        """Calming vibration for anxious dogs."""
        start = time.time()
        while time.time() - start < duration_sec:
            self.play_pattern(VibrationPattern.CALMING, intensity=0.5)
            time.sleep(0.5)
    
    def stop(self) -> None:
        """Stop any active vibration."""
        with self._lock:
            if self._pwm:
                self._pwm.ChangeDutyCycle(0)
            self._active = False


class HapticHTTPHandler(BaseHTTPRequestHandler):
    """HTTP API for haptic control."""
    
    controller: Optional[HapticController] = None
    
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
        
        if path == "haptic/health":
            self._send_json({
                "status": "ok",
                "service": "haptic_feedback",
                "enabled": bool(self.controller and self.controller.enabled),
            })
        elif path == "haptic/patterns":
            self._send_json({
                "patterns": [p.value for p in VibrationPattern],
                "descriptions": {
                    "single": "One short pulse",
                    "double": "Two short pulses",
                    "triple": "Three short pulses",
                    "sos": "Emergency SOS pattern",
                    "heartbeat": "Gentle heartbeat rhythm",
                    "wave": "Rising and falling",
                    "calming": "Slow, soothing pulses",
                    "urgent": "Rapid strong pulses",
                    "continuous": "Constant vibration",
                },
            })
        else:
            self._send_error(404, f"Unknown endpoint: {path}")
    
    def do_POST(self):
        path = self.path.strip("/")
        
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length).decode() if content_length > 0 else "{}"
        
        try:
            data = json.loads(body) if body else {}
        except json.JSONDecodeError:
            self._send_error(400, "Invalid JSON")
            return
        
        if path == "haptic/vibrate":
            if not self.controller:
                self._send_error(503, "Haptic controller not initialized")
                return
            duration = data.get("duration_sec", 0.5)
            intensity = data.get("intensity", 0.8)
            threading.Thread(
                target=self.controller.vibrate,
                args=(duration, intensity),
                daemon=True
            ).start()
            self._send_json({"status": "vibrating", "duration": duration})
        
        elif path == "haptic/pattern":
            if not self.controller:
                self._send_error(503, "Haptic controller not initialized")
                return
            pattern_name = data.get("pattern", "single")
            try:
                pattern = VibrationPattern(pattern_name)
            except ValueError:
                self._send_error(400, f"Unknown pattern: {pattern_name}")
                return
            intensity = data.get("intensity", 0.8)
            threading.Thread(
                target=self.controller.play_pattern,
                args=(pattern, intensity),
                daemon=True
            ).start()
            self._send_json({"status": "playing", "pattern": pattern_name})
        
        elif path == "haptic/alert":
            if not self.controller:
                self._send_error(503, "Haptic controller not initialized")
                return
            urgency = data.get("urgency", "medium")
            threading.Thread(
                target=self.controller.owner_alert,
                args=(urgency,),
                daemon=True
            ).start()
            self._send_json({"status": "alerting", "urgency": urgency})
        
        elif path == "haptic/calming":
            if not self.controller:
                self._send_error(503, "Haptic controller not initialized")
                return
            duration = data.get("duration_sec", 30)
            threading.Thread(
                target=self.controller.calming_mode,
                args=(duration,),
                daemon=True
            ).start()
            self._send_json({"status": "calming", "duration": duration})
        
        elif path == "haptic/stop":
            if self.controller:
                self.controller.stop()
            self._send_json({"status": "stopped"})
        
        else:
            self._send_error(404, f"Unknown endpoint: {path}")


def main():
    parser = argparse.ArgumentParser(description="Dog Agent — Haptic Feedback")
    parser.add_argument("--port", type=int, default=9133, help="HTTP API port")
    parser.add_argument("--simulate", action="store_true", help="Run simulation")
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose logging")
    args = parser.parse_args()
    
    if args.verbose:
        logger.setLevel(logging.DEBUG)
    
    controller = HapticController()
    
    if args.simulate:
        logger.info("=== Haptic Simulation ===")
        for pattern in VibrationPattern:
            logger.info(f"Pattern: {pattern.value}")
            time.sleep(0.5)
        return
    
    HapticHTTPHandler.controller = controller
    
    server = HTTPServer(("127.0.0.1", args.port), HapticHTTPHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    logger.info(f"Haptic API on http://127.0.0.1:{args.port}")
    
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
        controller.stop()
        logger.info("Haptic module stopped")


if __name__ == "__main__":
    main()
