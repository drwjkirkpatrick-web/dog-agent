#!/usr/bin/env python3
"""
Status LED Controller Module for Dog Agent

Controls a WS2812B/NeoPixel RGB LED for visual status indication.
Designed to be sewn into the dog sweater collar.

Features:
- Multiple color states (BLUE, GREEN, YELLOW, RED, PURPLE, ORANGE, WHITE, OFF)
- Pattern support (solid, pulse, blink, rainbow fade)
- HTTP API for remote control
- Thread-safe state updates
- Simulation mode for development
"""

import asyncio
import threading
import time
import math
import socket
import json
import logging
import argparse
from dataclasses import dataclass, asdict
from enum import Enum, auto
from typing import Optional, Dict, Any, Callable
from http.server import HTTPServer, BaseHTTPRequestHandler
import yaml

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class LEDColor:
    """Color constants for WS2812B (GRB order)"""
    OFF = (0, 0, 0)
    BLUE = (0, 0, 255)
    GREEN = (0, 255, 0)
    YELLOW = (255, 255, 0)
    RED = (255, 0, 0)
    PURPLE = (255, 0, 255)
    ORANGE = (255, 128, 0)
    WHITE = (255, 255, 255)

    # Named lookup
    NAMES = {
        'off': OFF,
        'blue': BLUE,
        'green': GREEN,
        'yellow': YELLOW,
        'red': RED,
        'purple': PURPLE,
        'orange': ORANGE,
        'white': WHITE,
    }

    @classmethod
    def from_name(cls, name: str) -> tuple:
        """Get color tuple from name"""
        return cls.NAMES.get(name.lower(), cls.OFF)

    @classmethod
    def to_name(cls, color: tuple) -> str:
        """Get name from color tuple"""
        for name, value in cls.NAMES.items():
            if value == color:
                return name
        return f"rgb({color[0]},{color[1]},{color[2]})"


class LEDPattern(Enum):
    """LED animation patterns"""
    SOLID = "solid"
    PULSE = "pulse"
    BLINK = "blink"
    RAINBOW_FADE = "rainbow_fade"


@dataclass
class LEDState:
    """Current LED state"""
    color: tuple
    pattern: LEDPattern
    meaning: str
    brightness: float = 1.0
    speed_hz: float = 1.0
    duration_sec: Optional[float] = None
    start_time: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            'color': LEDColor.to_name(self.color),
            'pattern': self.pattern.value,
            'meaning': self.meaning,
            'brightness': self.brightness,
            'speed_hz': self.speed_hz,
            'duration_sec': self.duration_sec,
            'active_since': self.start_time
        }


class StatusLEDController:
    """
    Status LED Controller for WS2812B/NeoPixel LED

    Manages LED colors, patterns, and HTTP API for remote control.
    Thread-safe for use with multiple modules.
    """

    # Status meanings
    MEANINGS = {
        'booting': 'Booting/initializing',
        'nominal': 'All systems nominal, GPS fix',
        'gps_searching': 'GPS searching / weak signal',
        'alert_escape': 'Escape alert active',
        'alert_health': 'Health anomaly alert',
        'recording': 'Recording data / voice active',
        'low_battery': 'Low battery (<20%)',
        'hermes_interaction': 'Hermes interaction in progress',
        'deep_sleep': 'Deep sleep mode',
        'manual': 'Manual control',
    }

    def __init__(self, config: Dict[str, Any], simulate: bool = False):
        """
        Initialize the LED controller

        Args:
            config: Configuration dict with status_led settings
            simulate: If True, print LED state instead of controlling hardware
        """
        self.config = config.get('status_led', {})
        self.simulate = simulate
        self.enabled = self.config.get('enabled', True)
        self.gpio_pin = self.config.get('gpio_pin', 18)
        self.brightness = self.config.get('brightness', 0.5)
        self.default_pattern = self.config.get('pattern', 'solid')

        # Thread safety
        self._lock = threading.Lock()
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._strip = None

        # Current state
        self._current_state = LEDState(
            color=LEDColor.OFF,
            pattern=LEDPattern.SOLID,
            meaning='Off',
            brightness=self.brightness
        )

        # Pattern generators
        self._pattern_generators = {
            LEDPattern.SOLID: self._pattern_solid,
            LEDPattern.PULSE: self._pattern_pulse,
            LEDPattern.BLINK: self._pattern_blink,
            LEDPattern.RAINBOW_FADE: self._pattern_rainbow_fade,
        }

        # Initialize hardware or simulation
        if self.enabled and not self.simulate:
            self._init_hardware()

        logger.info(f"Status LED Controller initialized (enabled={self.enabled}, simulate={self.simulate})")

    def _init_hardware(self):
        """Initialize WS2812B hardware interface"""
        try:
            # Try rpi_ws281x library first (best performance on Raspberry Pi)
            import board
            import neopixel

            self._strip = neopixel.NeoPixel(
                board.D18,  # GPIO 18 (PWM)
                1,  # 1 LED
                brightness=self.brightness,
                auto_write=False
            )
            logger.info(f"Initialized NeoPixel on GPIO {self.gpio_pin}")
            self._hardware_mode = 'neopixel'

        except ImportError:
            try:
                # Fallback to rpi_ws281x
                import rpi_ws281x

                self._strip = rpi_ws281x.PixelStrip(
                    num=1,
                    pin=self.gpio_pin,
                    freq_hz=800000,  # 800kHz for WS2812B
                    dma=10,
                    invert=False,
                    brightness=int(self.brightness * 255),
                    channel=0
                )
                self._strip.begin()
                logger.info(f"Initialized rpi_ws281x on GPIO {self.gpio_pin}")
                self._hardware_mode = 'rpi_ws281x'

            except ImportError:
                logger.warning("No WS2812 library found. Using bit-bang fallback or simulation.")
                self._hardware_mode = 'simulation'
                self.simulate = True

    def _set_pixel_color(self, color: tuple):
        """Set pixel color on hardware or simulation"""
        if self.simulate:
            return

        if self._strip is None:
            return

        try:
            # Scale brightness
            r = int(color[0] * self.brightness)
            g = int(color[1] * self.brightness)
            b = int(color[2] * self.brightness)

            if self._hardware_mode == 'neopixel':
                self._strip[0] = (g, r, b)  # NeoPixel uses GRB order
                self._strip.show()
            elif self._hardware_mode == 'rpi_ws281x':
                self._strip.setPixelColor(0, (g << 16) | (r << 8) | b)
                self._strip.show()

        except Exception as e:
            logger.error(f"Failed to set pixel color: {e}")

    def _pattern_solid(self, state: LEDState, elapsed: float) -> tuple:
        """Solid color pattern"""
        return state.color

    def _pattern_pulse(self, state: LEDState, elapsed: float) -> tuple:
        """Pulsing pattern (slow fade in/out)"""
        # Sine wave from 0 to 1
        intensity = (math.sin(elapsed * state.speed_hz * 2 * math.pi) + 1) / 2
        intensity = 0.3 + (intensity * 0.7)  # Minimum 30% brightness

        r = int(state.color[0] * intensity)
        g = int(state.color[1] * intensity)
        b = int(state.color[2] * intensity)
        return (r, g, b)

    def _pattern_blink(self, state: LEDState, elapsed: float) -> tuple:
        """Fast blink pattern"""
        # Square wave
        phase = (elapsed * state.speed_hz) % 1.0
        if phase < 0.5:
            return state.color
        return LEDColor.OFF

    def _pattern_rainbow_fade(self, state: LEDState, elapsed: float) -> tuple:
        """Rainbow fade pattern"""
        hue = (elapsed * state.speed_hz) % 1.0
        return self._hsv_to_rgb(hue, 1.0, 1.0)

    @staticmethod
    def _hsv_to_rgb(h: float, s: float, v: float) -> tuple:
        """Convert HSV to RGB tuple"""
        if s == 0.0:
            return (int(v * 255), int(v * 255), int(v * 255))

        i = int(h * 6)
        f = (h * 6) - i
        p = v * (1 - s)
        q = v * (1 - s * f)
        t = v * (1 - s * (1 - f))

        i %= 6
        if i == 0:
            rgb = (v, t, p)
        elif i == 1:
            rgb = (q, v, p)
        elif i == 2:
            rgb = (p, v, t)
        elif i == 3:
            rgb = (p, q, v)
        elif i == 4:
            rgb = (t, p, v)
        else:
            rgb = (v, p, q)

        return (int(rgb[0] * 255), int(rgb[1] * 255), int(rgb[2] * 255))

    def _update_loop(self):
        """Main LED update loop - runs in separate thread"""
        while self._running:
            try:
                with self._lock:
                    state = self._current_state
                    elapsed = time.time() - state.start_time

                    # Check duration timeout
                    if state.duration_sec and elapsed > state.duration_sec:
                        # Revert to nominal state
                        self._set_state_internal(
                            LEDColor.GREEN,
                            LEDPattern.SOLID,
                            'nominal'
                        )
                        continue

                    # Generate pattern
                    pattern_func = self._pattern_generators.get(state.pattern, self._pattern_solid)
                    color = pattern_func(state, elapsed)

                # Apply color
                self._set_pixel_color(color)

                if self.simulate:
                    status = f"LED: {LEDColor.to_name(color)} ({state.pattern.value}) - {state.meaning}"
                    print(f"\r{status:60s}", end='', flush=True)

                # Frame rate ~30fps
                time.sleep(0.033)

            except Exception as e:
                logger.error(f"LED update error: {e}")
                time.sleep(0.1)

    def _set_state_internal(self, color: tuple, pattern: LEDPattern, meaning_key: str,
                            brightness: Optional[float] = None, speed_hz: float = 1.0,
                            duration_sec: Optional[float] = None):
        """Internal state update (assumes lock is held or called from within lock)"""
        self._current_state = LEDState(
            color=color,
            pattern=pattern,
            meaning=self.MEANINGS.get(meaning_key, meaning_key),
            brightness=brightness if brightness is not None else self.brightness,
            speed_hz=speed_hz,
            duration_sec=duration_sec,
            start_time=time.time()
        )

        if self.simulate:
            logger.info(f"State: {self._current_state.meaning} - {LEDColor.to_name(color)} {pattern.value}")

    def set_state(self, color_name: str, pattern_name: str = 'solid',
                  meaning_key: str = 'manual', duration_sec: Optional[float] = None,
                  brightness: Optional[float] = None, speed_hz: float = 1.0):
        """
        Set LED state (thread-safe)

        Args:
            color_name: Color name (blue, green, yellow, red, purple, orange, white, off)
            pattern_name: Pattern name (solid, pulse, blink, rainbow_fade)
            meaning_key: Meaning key for status description
            duration_sec: Auto-revert duration (None for permanent)
            brightness: Override brightness (None for default)
            speed_hz: Pattern speed in Hz
        """
        if not self.enabled:
            return

        color = LEDColor.from_name(color_name)
        pattern = LEDPattern(pattern_name) if isinstance(pattern_name, str) else pattern_name

        with self._lock:
            self._set_state_internal(color, pattern, meaning_key, brightness, speed_hz, duration_sec)

    def get_state(self) -> Dict[str, Any]:
        """Get current LED state"""
        with self._lock:
            return self._current_state.to_dict()

    def boot_sequence(self):
        """Run boot sequence (rainbow fade)"""
        if not self.enabled:
            return

        self.set_state('blue', 'rainbow_fade', 'booting', speed_hz=0.5, duration_sec=3.0)
        logger.info("Boot sequence started")

    def set_nominal(self):
        """Set nominal state (green solid)"""
        self.set_state('green', 'solid', 'nominal')

    def set_gps_searching(self):
        """Set GPS searching state (yellow pulse)"""
        self.set_state('yellow', 'pulse', 'gps_searching', speed_hz=1.0)

    def set_alert_escape(self):
        """Set escape alert state (red blink)"""
        self.set_state('red', 'blink', 'alert_escape', speed_hz=3.0)

    def set_alert_health(self):
        """Set health alert state (red pulse)"""
        self.set_state('red', 'pulse', 'alert_health', speed_hz=2.0)

    def set_recording(self, active: bool = True):
        """Set recording state (purple)"""
        if active:
            self.set_state('purple', 'pulse', 'recording', speed_hz=1.5)
        else:
            self.set_nominal()

    def set_low_battery(self):
        """Set low battery state (orange pulse)"""
        self.set_state('orange', 'pulse', 'low_battery', speed_hz=0.5)

    def set_hermes_interaction(self, active: bool = True):
        """Set Hermes interaction state (white)"""
        if active:
            self.set_state('white', 'solid', 'hermes_interaction')
        else:
            self.set_nominal()

    def set_deep_sleep(self):
        """Set deep sleep state (off)"""
        self.set_state('off', 'solid', 'deep_sleep')

    def start(self):
        """Start the LED controller thread"""
        if not self.enabled or self._running:
            return

        self._running = True
        self._thread = threading.Thread(target=self._update_loop, daemon=True)
        self._thread.start()
        logger.info("LED controller thread started")

        # Run boot sequence
        self.boot_sequence()

    def stop(self):
        """Stop the LED controller"""
        self._running = False
        if self._thread:
            self._thread.join(timeout=1.0)

        # Turn off LED
        self._set_pixel_color(LEDColor.OFF)
        logger.info("LED controller stopped")

    def health_check(self) -> Dict[str, Any]:
        """Health check for monitoring"""
        return {
            'status': 'healthy' if self.enabled else 'disabled',
            'enabled': self.enabled,
            'simulate': self.simulate,
            'hardware_mode': getattr(self, '_hardware_mode', 'none'),
            'gpio_pin': self.gpio_pin,
            'brightness': self.brightness,
            'thread_running': self._running and self._thread is not None and self._thread.is_alive()
        }


class LEDHTTPRequestHandler(BaseHTTPRequestHandler):
    """HTTP request handler for LED API"""

    controller: Optional[StatusLEDController] = None

    def log_message(self, format, *args):
        """Suppress default logging"""
        logger.debug(format % args)

    def _send_json(self, data: Dict[str, Any], status: int = 200):
        """Send JSON response"""
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps(data, indent=2).encode())

    def _send_error(self, message: str, status: int = 400):
        """Send error response"""
        self._send_json({'error': message}, status)

    def do_GET(self):
        """Handle GET requests"""
        if not self.controller:
            self._send_error('Controller not initialized', 500)
            return

        if self.path == '/led/status':
            self._send_json(self.controller.get_state())

        elif self.path == '/led/health':
            self._send_json(self.controller.health_check())

        else:
            self._send_error('Not found', 404)

    def do_POST(self):
        """Handle POST requests"""
        if not self.controller:
            self._send_error('Controller not initialized', 500)
            return

        try:
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length).decode()
            data = json.loads(body) if body else {}

            if self.path == '/led/color':
                color = data.get('color', 'white')
                pattern = data.get('pattern', 'solid')
                duration = data.get('duration_sec')

                self.controller.set_state(
                    color_name=color,
                    pattern_name=pattern,
                    meaning_key='manual',
                    duration_sec=duration
                )
                self._send_json({
                    'success': True,
                    'color': color,
                    'pattern': pattern,
                    'duration_sec': duration
                })

            elif self.path == '/led/pattern':
                pattern = data.get('pattern', 'solid')
                speed = data.get('speed_hz', 1.0)

                current = self.controller.get_state()
                self.controller.set_state(
                    color_name=current['color'],
                    pattern_name=pattern,
                    speed_hz=speed
                )
                self._send_json({
                    'success': True,
                    'pattern': pattern,
                    'speed_hz': speed
                })

            else:
                self._send_error('Not found', 404)

        except json.JSONDecodeError:
            self._send_error('Invalid JSON', 400)
        except Exception as e:
            self._send_error(str(e), 500)


class LEDAPIServer:
    """HTTP API server for LED control"""

    def __init__(self, controller: StatusLEDController, port: int = 9121):
        self.controller = controller
        self.port = port
        self._server: Optional[HTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    def start(self):
        """Start HTTP server"""
        LEDHTTPRequestHandler.controller = self.controller

        self._server = HTTPServer(('0.0.0.0', self.port), LEDHTTPRequestHandler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        logger.info(f"LED API server started on port {self.port}")

    def stop(self):
        """Stop HTTP server"""
        if self._server:
            self._server.shutdown()
            self._server.server_close()
        logger.info("LED API server stopped")


class StatusLEDManager:
    """
    High-level manager that combines controller and API server
    """

    def __init__(self, config_path: str = 'config.yaml', simulate: bool = False):
        # Load config
        with open(config_path, 'r') as f:
            self.config = yaml.safe_load(f)

        self.simulate = simulate
        self.controller = StatusLEDController(self.config, simulate=simulate)

        # API server on port 9121
        self.api = LEDAPIServer(self.controller, port=9121)

    def start(self):
        """Start LED controller and API server"""
        self.controller.start()
        self.api.start()

    def stop(self):
        """Stop LED controller and API server"""
        self.api.stop()
        self.controller.stop()

    def set_state(self, color=None, pattern=None, meaning_key=None, **kwargs):
        """Proxy to controller.set_state"""
        if color is None:
            # Called with keyword arguments
            self.controller.set_state(**kwargs)
        else:
            # Called with positional arguments
            self.controller.set_state(
                color_name=color,
                pattern_name=pattern,
                meaning_key=meaning_key,
                **kwargs
            )

    def get_state(self):
        """Proxy to controller.get_state"""
        return self.controller.get_state()


def main():
    """Main entry point for standalone operation"""
    parser = argparse.ArgumentParser(description='Status LED Controller')
    parser.add_argument('--config', default='config.yaml', help='Config file path')
    parser.add_argument('--simulate', action='store_true', help='Simulation mode (no hardware)')
    parser.add_argument('--test', action='store_true', help='Run test sequence')
    args = parser.parse_args()

    # Initialize manager
    manager = StatusLEDManager(config_path=args.config, simulate=args.simulate)

    try:
        manager.start()

        if args.test:
            # Run test sequence
            logger.info("Running test sequence...")

            patterns = [
                ('blue', 'solid', 'booting'),
                ('green', 'pulse', 'nominal'),
                ('yellow', 'pulse', 'gps_searching'),
                ('red', 'blink', 'alert_escape'),
                ('red', 'pulse', 'alert_health'),
                ('purple', 'solid', 'recording'),
                ('orange', 'pulse', 'low_battery'),
                ('white', 'solid', 'hermes_interaction'),
                ('green', 'rainbow_fade', 'booting'),
            ]

            for color, pattern, meaning in patterns:
                logger.info(f"Testing: {color} {pattern} ({meaning})")
                manager.set_state(color, pattern, meaning, duration_sec=2.0)
                time.sleep(2.5)

            manager.set_state('green', 'solid', 'nominal')
            logger.info("Test sequence complete")

        # Keep running
        while True:
            time.sleep(1)

    except KeyboardInterrupt:
        logger.info("Shutting down...")
    finally:
        manager.stop()


if __name__ == '__main__':
    main()
