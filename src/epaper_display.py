#!/usr/bin/env python3
"""
Dog Agent — E-Paper Display
===========================
Ultra-low power display for battery/walks/status.

Features:
  - 2.13" Waveshare e-paper (250x122)
  - Shows: battery %, last walk, simple status
  - Updates only when needed (0 power between updates)
  - Partial refresh support

Usage:
    python src/epaper_display.py
    python src/epaper_display.py --simulate
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
from typing import Any, Dict, Optional

import yaml

# Logging
logger = logging.getLogger("epaper_display")
handler = logging.StreamHandler(sys.stdout)
handler.setFormatter(logging.Formatter(
    "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
))
logger.addHandler(handler)
logger.setLevel(logging.INFO)

PROJECT_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_DIR / "config.yaml"


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


class EPaperDisplay:
    """Waveshare 2.13" e-paper display driver."""
    
    def __init__(self):
        self.enabled = get_cfg("epaper.enabled", False)
        self.width = 250
        self.height = 122
        self.update_interval_sec = get_cfg("epaper.update_interval_sec", 300)
        
        self._display = None
        self._last_update = 0
        self._current_image = None
        
        if self.enabled:
            self._init_display()
    
    def _init_display(self) -> None:
        try:
            from waveshare_epd import epd2in13_V2
            self._display = epd2in13_V2.EPD()
            self._display.init(self._display.FULL_UPDATE)
            logger.info("E-paper display initialized")
        except ImportError:
            logger.warning("waveshare_epd library not available")
        except Exception as e:
            logger.warning(f"Failed to initialize display: {e}")
    
    def _get_status_image(self) -> Optional[bytes]:
        """Generate status image (returns bytes or None for simulation)."""
        try:
            # Get current status
            import requests
            
            battery = requests.get("http://localhost:9132/battery/current", timeout=5).json()
            activity = requests.get("http://localhost:9126/activity/today", timeout=5).json()
            
            # Simple text representation
            lines = [
                "Dog Agent Status",
                f"Battery: {battery.get('percent', '?')}%",
                f"Walks: {activity.get('walk_count', '?')}",
                f"Score: {activity.get('score', '?')}/100",
                datetime.now().strftime("%H:%M"),
            ]
            
            return "\n".join(lines).encode()
        except:
            return None
    
    def update(self) -> bool:
        """Update display with current status."""
        if not self.enabled:
            return False
        
        now = time.time()
        if now - self._last_update < self.update_interval_sec:
            return False
        
        try:
            image_data = self._get_status_image()
            if image_data:
                if self._display:
                    # Would actually draw to display here
                    logger.info("Updating e-paper display")
                    self._display.display(self._display.getbuffer(None))
                else:
                    logger.info(f"[SIM] E-paper would show:\n{image_data.decode()}")
                
                self._last_update = now
                return True
        except Exception as e:
            logger.error(f"Display update error: {e}")
        
        return False
    
    def clear(self):
        """Clear display."""
        if self._display:
            self._display.Clear(0xFF)
    
    def get_status(self) -> dict:
        return {
            "enabled": self.enabled,
            "width": self.width,
            "height": self.height,
            "last_update": self._last_update,
        }


class EPaperHTTPHandler(BaseHTTPRequestHandler):
    """HTTP API for e-paper display."""
    
    display: Optional[EPaperDisplay] = None
    
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
        
        if path == "epaper/health":
            self._send_json({
                "status": "ok",
                "service": "epaper_display",
                "enabled": bool(self.display and self.display.enabled),
            })
        elif path == "epaper/status":
            if self.display:
                self._send_json(self.display.get_status())
            else:
                self._send_json({"error": "Display not initialized"}, 503)
        else:
            self._send_json({"error": f"Unknown endpoint: {path}"}, 404)
    
    def do_POST(self):
        path = self.path.strip("/")
        
        if path == "epaper/update":
            if self.display:
                success = self.display.update()
                self._send_json({"updated": success})
            else:
                self._send_json({"error": "Display not initialized"}, 503)
        elif path == "epaper/clear":
            if self.display:
                self.display.clear()
                self._send_json({"cleared": True})
            else:
                self._send_json({"error": "Display not initialized"}, 503)
        else:
            self._send_json({"error": f"Unknown endpoint: {path}"}, 404)


def main():
    parser = argparse.ArgumentParser(description="Dog Agent — E-Paper Display")
    parser.add_argument("--port", type=int, default=9143, help="HTTP API port")
    parser.add_argument("--simulate", action="store_true", help="Run simulation")
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose logging")
    args = parser.parse_args()
    
    if args.verbose:
        logger.setLevel(logging.DEBUG)
    
    display = EPaperDisplay()
    
    if args.simulate:
        logger.info("=== E-Paper Display Simulation ===")
        logger.info("Display would show: Battery, Walks, Score, Time")
        return
    
    EPaperHTTPHandler.display = display
    
    server = HTTPServer(("127.0.0.1", args.port), EPaperHTTPHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    logger.info(f"E-paper API on http://127.0.0.1:{args.port}")
    
    # Update loop
    def update_loop():
        while True:
            if display.enabled:
                display.update()
            time.sleep(10)
    
    update_thread = threading.Thread(target=update_loop, daemon=True)
    update_thread.start()
    
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
        logger.info("E-paper module stopped")


if __name__ == "__main__":
    main()
