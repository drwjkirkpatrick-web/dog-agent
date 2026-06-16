#!/usr/bin/env python3
"""
Dog Agent — Panic Button
=======================
Physical button interface for owner emergency signaling.

Features:
  - Single press: Mark location for later
  - Double press: Send "all is well" check-in
  - Hold 3 seconds: Emergency alert with GPS

Usage:
    python src/panic_button.py
    python src/panic_button.py --simulate
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
from enum import Enum
from typing import Any, Dict, Optional

import yaml

# Logging
logger = logging.getLogger("panic_button")
handler = logging.StreamHandler(sys.stdout)
handler.setFormatter(logging.Formatter(
    "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
))
logger.addHandler(handler)
logger.setLevel(logging.INFO)

PROJECT_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_DIR / "config.yaml"


class ButtonAction(Enum):
    SINGLE_PRESS = "single"
    DOUBLE_PRESS = "double"
    HOLD = "hold"
    NONE = "none"


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
class ButtonEvent:
    action: ButtonAction
    timestamp: datetime
    location: Optional[Dict] = None


class PanicButtonController:
    """Monitors GPIO button and detects press patterns."""
    
    def __init__(self):
        self.enabled = get_cfg("panic_button.enabled", False)
        self.gpio_pin = get_cfg("panic_button.gpio_pin", 27)
        self.hold_threshold_sec = get_cfg("panic_button.hold_threshold_sec", 3)
        self.double_press_window_sec = get_cfg("panic_button.double_press_window_sec", 0.5)
        
        self._last_press_time: Optional[float] = None
        self._press_count = 0
        self._gpio = None
        self._running = False
        self._thread: Optional[threading.Thread] = None
        
        if self.enabled:
            self._init_gpio()
    
    def _init_gpio(self) -> None:
        try:
            import RPi.GPIO as GPIO
            GPIO.setmode(GPIO.BCM)
            GPIO.setup(self.gpio_pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)
            GPIO.add_event_detect(
                self.gpio_pin, GPIO.FALLING,
                callback=self._on_press,
                bouncetime=200
            )
            self._gpio = GPIO
            logger.info(f"Panic button initialized on GPIO {self.gpio_pin}")
        except ImportError:
            logger.warning("RPi.GPIO not available, using simulation")
        except Exception as e:
            logger.warning(f"Failed to initialize GPIO: {e}")
    
    def _on_press(self, channel):
        """Handle button press interrupt."""
        current_time = time.time()
        
        # Check for hold
        if self._last_press_time:
            time_since_last = current_time - self._last_press_time
            
            if time_since_last < self.double_press_window_sec:
                self._press_count += 1
            else:
                self._press_count = 1
        else:
            self._press_count = 1
        
        self._last_press_time = current_time
        
        # Schedule check for pattern detection
        threading.Timer(self.double_press_window_sec + 0.1, self._check_pattern).start()
    
    def _check_pattern(self):
        """Determine press pattern and trigger action."""
        if self._press_count == 0:
            return
        
        current_time = time.time()
        if self._last_press_time and (current_time - self._last_press_time) > self.double_press_window_sec:
            # Pattern complete
            if self._press_count == 1:
                # Check if it was a hold
                if self._was_hold():
                    self._trigger_action(ButtonAction.HOLD)
                else:
                    self._trigger_action(ButtonAction.SINGLE_PRESS)
            elif self._press_count == 2:
                self._trigger_action(ButtonAction.DOUBLE_PRESS)
            
            self._press_count = 0
    
    def _was_hold(self) -> bool:
        """Check if last press was a hold."""
        if not self._gpio:
            return False
        
        import RPi.GPIO as GPIO
        start = time.time()
        while time.time() - start < self.hold_threshold_sec + 0.5:
            if GPIO.input(self.gpio_pin) == GPIO.HIGH:
                return False
            time.sleep(0.05)
        return True
    
    def _trigger_action(self, action: ButtonAction) -> None:
        """Execute action based on button press."""
        logger.info(f"Panic button: {action.value}")
        
        event = ButtonEvent(
            action=action,
            timestamp=datetime.now(timezone.utc),
            location=self._get_location(),
        )
        
        # Perform action
        if action == ButtonAction.SINGLE_PRESS:
            self._mark_location(event)
        elif action == ButtonAction.DOUBLE_PRESS:
            self._send_check_in(event)
        elif action == ButtonAction.HOLD:
            self._send_emergency(event)
    
    def _get_location(self) -> Optional[Dict]:
        """Get current GPS location."""
        try:
            import requests
            resp = requests.get("http://localhost:9111/gps", timeout=5)
            if resp.status_code == 200:
                return resp.json()
        except:
            pass
        return None
    
    def _mark_location(self, event: ButtonEvent) -> None:
        """Mark current location for later review."""
        logger.info("Location marked for later review")
        # Store in file
        mark_file = PROJECT_DIR / "data" / "marked_locations.json"
        marks = []
        if mark_file.exists():
            with open(mark_file) as f:
                marks = json.load(f)
        marks.append({
            "timestamp": event.timestamp.isoformat(),
            "location": event.location,
            "note": "Owner marked location",
        })
        with open(mark_file, "w") as f:
            json.dump(marks, f, indent=2)
    
    def _send_check_in(self, event: ButtonEvent) -> None:
        """Send "all is well" check-in."""
        logger.info("Sending check-in")
        try:
            import requests
            requests.post(
                "http://localhost:9118/alerts/send",
                json={
                    "level": "info",
                    "message": "Check-in: All is well",
                    "location": event.location,
                },
                timeout=5
            )
        except Exception as e:
            logger.error(f"Failed to send check-in: {e}")
    
    def _send_emergency(self, event: ButtonEvent) -> None:
        """Send emergency alert."""
        logger.critical("EMERGENCY ALERT TRIGGERED")
        try:
            import requests
            requests.post(
                "http://localhost:9118/alerts/send",
                json={
                    "level": "emergency",
                    "message": "EMERGENCY: Owner panic button activated",
                    "location": event.location,
                },
                timeout=5
            )
        except Exception as e:
            logger.error(f"Failed to send emergency: {e}")
    
    def get_status(self) -> dict:
        return {
            "enabled": self.enabled,
            "gpio_pin": self.gpio_pin,
            "last_press": self._last_press_time,
        }


class PanicButtonHTTPHandler(BaseHTTPRequestHandler):
    """HTTP API for panic button."""
    
    controller: Optional[PanicButtonController] = None
    
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
        
        if path == "panic/health":
            self._send_json({
                "status": "ok",
                "service": "panic_button",
                "enabled": bool(self.controller and self.controller.enabled),
            })
        elif path == "panic/status":
            if self.controller:
                self._send_json(self.controller.get_status())
            else:
                self._send_json({"error": "Controller not initialized"})
        else:
            self._send_json({"error": f"Unknown endpoint: {path}"}, 404)
    
    def do_POST(self):
        path = self.path.strip("/")
        
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length).decode() if content_length > 0 else "{}"
        
        try:
            data = json.loads(body) if body else {}
        except json.JSONDecodeError:
            self._send_json({"error": "Invalid JSON"}, 400)
            return
        
        if path == "panic/test":
            action = data.get("action", "single")
            if action == "emergency":
                if self.controller:
                    self.controller._trigger_action(ButtonAction.HOLD)
                self._send_json({"status": "emergency_triggered"})
            elif action == "checkin":
                if self.controller:
                    self.controller._trigger_action(ButtonAction.DOUBLE_PRESS)
                self._send_json({"status": "checkin_sent"})
            else:
                if self.controller:
                    self.controller._trigger_action(ButtonAction.SINGLE_PRESS)
                self._send_json({"status": "location_marked"})
        else:
            self._send_json({"error": f"Unknown endpoint: {path}"}, 404)


def main():
    parser = argparse.ArgumentParser(description="Dog Agent — Panic Button")
    parser.add_argument("--port", type=int, default=9135, help="HTTP API port")
    parser.add_argument("--simulate", action="store_true", help="Run simulation")
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose logging")
    args = parser.parse_args()
    
    if args.verbose:
        logger.setLevel(logging.DEBUG)
    
    controller = PanicButtonController()
    
    if args.simulate:
        logger.info("=== Panic Button Simulation ===")
        logger.info("Single press: Mark location")
        logger.info("Double press: Send check-in")
        logger.info("Hold 3s: Emergency alert")
        return
    
    PanicButtonHTTPHandler.controller = controller
    
    server = HTTPServer(("127.0.0.1", args.port), PanicButtonHTTPHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    logger.info(f"Panic button API on http://127.0.0.1:{args.port}")
    
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
        logger.info("Panic button stopped")


if __name__ == "__main__":
    main()
