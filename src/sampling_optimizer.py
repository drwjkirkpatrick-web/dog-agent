#!/usr/bin/env python3
"""
Dog Agent — Adaptive Multi-Sensor Sampling Optimizer
=====================================================
Dynamically adjusts sensor sampling rates based on activity, battery,
location confidence, and accuracy preferences.

Features:
  - Coordinates GPS, IMU, environmental, and display rates
  - Power/accuracy trade-off control
  - Battery-aware throttling
  - Activity-driven adaptive sampling

Usage:
    python src/sampling_optimizer.py
    python src/sampling_optimizer.py --simulate
"""

from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

# Logging
logger = logging.getLogger("sampling_optimizer")
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


class PowerMode(Enum):
    ECO = "eco"           # Maximum battery savings
    BALANCED = "balanced" # Default
    HIGH_ACCURACY = "high_accuracy"  # Maximum tracking precision


@dataclass
class SamplingRates:
    gps_hz: float
    imu_hz: float
    env_poll_sec: float
    lora_tx_sec: float
    display_refresh_sec: float
    health_check_sec: float


class SamplingOptimizer:
    """Dynamic sampling rate optimizer."""
    
    def __init__(self):
        self.enabled = get_cfg("sampling_optimizer.enabled", False)
        self.default_mode = PowerMode(get_cfg("sampling_optimizer.default_mode", "balanced"))
        self.poll_interval_sec = get_cfg("sampling_optimizer.poll_interval_sec", 5.0)
        self.battery_threshold_low = get_cfg("sampling_optimizer.battery_threshold_low", 20)
        self.battery_threshold_critical = get_cfg("sampling_optimizer.battery_threshold_critical", 10)
        
        self._current_mode = self.default_mode
        self._activity_level = 0.0  # 0-1
        self._rates = SamplingRates(
            gps_hz=1.0,
            imu_hz=10.0,
            env_poll_sec=60.0,
            lora_tx_sec=300.0,
            display_refresh_sec=60.0,
            health_check_sec=60.0,
        )
        self._rate_history: deque[Dict] = deque(maxlen=100)
        self._last_change: Optional[datetime] = None
        self._lock = threading.Lock()
        
        self._update_rates()
    
    def _mode_to_rates(self, mode: PowerMode, activity: float) -> SamplingRates:
        """Compute rates from mode and activity."""
        if mode == PowerMode.ECO:
            return SamplingRates(
                gps_hz=0.05 + activity * 0.05,       # 1 fix per 20s, max 1/10s
                imu_hz=1.0 + activity * 4.0,
                env_poll_sec=300.0,
                lora_tx_sec=600.0,
                display_refresh_sec=300.0,
                health_check_sec=300.0,
            )
        elif mode == PowerMode.HIGH_ACCURACY:
            return SamplingRates(
                gps_hz=10.0,
                imu_hz=100.0,
                env_poll_sec=10.0,
                lora_tx_sec=60.0,
                display_refresh_sec=5.0,
                health_check_sec=10.0,
            )
        else:  # balanced
            return SamplingRates(
                gps_hz=0.2 + activity * 4.8,  # 1/5 Hz to 5 Hz
                imu_hz=10.0 + activity * 40.0,
                env_poll_sec=60.0 - activity * 40.0,
                lora_tx_sec=300.0 - activity * 180.0,
                display_refresh_sec=60.0 - activity * 40.0,
                health_check_sec=60.0 - activity * 30.0,
            )
    
    def _get_battery_pct(self) -> Optional[float]:
        try:
            import requests
            resp = requests.get("http://localhost:9132/battery/status", timeout=5)
            if resp.status_code == 200:
                return resp.json().get("percentage")
        except:
            pass
        return None
    
    def _get_activity_level(self) -> float:
        """Query activity level from behavior or IMU."""
        try:
            import requests
            resp = requests.get("http://localhost:9115/behavior/activity", timeout=5)
            if resp.status_code == 200:
                return resp.json().get("level", 0.0)
        except:
            pass
        
        # Fallback to IMU motion
        try:
            import requests
            resp = requests.get("http://localhost:9122/environmental/imu", timeout=5)
            if resp.status_code == 200:
                data = resp.json()
                mag = (data.get("accel_x", 0)**2 + data.get("accel_y", 0)**2 + data.get("accel_z", 0)**2) ** 0.5
                return min(1.0, abs(mag - 1.0) / 2.0)
        except:
            pass
        return 0.0
    
    def _select_mode(self, battery: Optional[float]) -> PowerMode:
        """Select mode based on battery."""
        if battery is None:
            return self.default_mode
        if battery <= self.battery_threshold_critical:
            return PowerMode.ECO
        if battery <= self.battery_threshold_low:
            return PowerMode.ECO
        return self.default_mode
    
    def _update_rates(self):
        """Recompute rates."""
        battery = self._get_battery_pct()
        self._current_mode = self._select_mode(battery)
        self._activity_level = self._get_activity_level()
        self._rates = self._mode_to_rates(self._current_mode, self._activity_level)
        
        with self._lock:
            self._last_change = datetime.now(timezone.utc)
            self._rate_history.append({
                "timestamp": self._last_change.isoformat(),
                "mode": self._current_mode.value,
                "battery_pct": battery,
                "activity": round(self._activity_level, 2),
                "rates": self._rates_to_dict(),
            })
    
    def _apply_rates(self):
        """Push recommended rates to other modules."""
        try:
            import requests
            # Update GPS adaptive rate
            requests.post(
                "http://localhost:9124/adaptive/config",
                json={"target_rate_hz": self._rates.gps_hz},
                timeout=5,
            )
        except:
            pass
    
    def _rates_to_dict(self) -> dict:
        return {
            "gps_hz": round(self._rates.gps_hz, 2),
            "imu_hz": round(self._rates.imu_hz, 1),
            "env_poll_sec": round(self._rates.env_poll_sec, 1),
            "lora_tx_sec": round(self._rates.lora_tx_sec, 1),
            "display_refresh_sec": round(self._rates.display_refresh_sec, 1),
            "health_check_sec": round(self._rates.health_check_sec, 1),
        }
    
    def update(self):
        """Run one optimization cycle."""
        if not self.enabled:
            return
        
        self._update_rates()
        self._apply_rates()
        logger.info(f"Sampling mode={self._current_mode.value}, activity={self._activity_level:.2f}, GPS={self._rates.gps_hz:.2f}Hz")
    
    def get_status(self) -> dict:
        with self._lock:
            return {
                "enabled": self.enabled,
                "current_mode": self._current_mode.value,
                "default_mode": self.default_mode.value,
                "activity_level": round(self._activity_level, 2),
                "recommended_rates": self._rates_to_dict(),
                "last_change": self._last_change.isoformat() if self._last_change else None,
                "recent_history": list(self._rate_history)[-10:],
            }
    
    def set_mode(self, mode: str) -> bool:
        try:
            self.default_mode = PowerMode(mode)
            self._update_rates()
            return True
        except ValueError:
            return False


class OptimizerHTTPHandler(BaseHTTPRequestHandler):
    """HTTP API for sampling optimizer."""
    
    optimizer: Optional[SamplingOptimizer] = None
    
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
        
        if path == "optimizer/health":
            self._send_json({
                "status": "ok",
                "service": "sampling_optimizer",
                "enabled": bool(self.optimizer and self.optimizer.enabled),
            })
        elif path == "optimizer/status":
            if not self.optimizer:
                self._send_json({"error": "Optimizer not initialized"}, 503)
                return
            self._send_json(self.optimizer.get_status())
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
        
        if path == "optimizer/mode":
            if not self.optimizer:
                self._send_json({"error": "Optimizer not initialized"}, 503)
                return
            success = self.optimizer.set_mode(data.get("mode", "balanced"))
            self._send_json({"success": success, "mode": self.optimizer.default_mode.value})
        else:
            self._send_json({"error": f"Unknown endpoint: {path}"}, 404)


def main():
    parser = argparse.ArgumentParser(description="Dog Agent — Sampling Optimizer")
    parser.add_argument("--port", type=int, default=9157, help="HTTP API port")
    parser.add_argument("--simulate", action="store_true", help="Run simulation")
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose logging")
    args = parser.parse_args()
    
    if args.verbose:
        logger.setLevel(logging.DEBUG)
    
    optimizer = SamplingOptimizer()
    
    if args.simulate:
        logger.info("=== Sampling Optimizer Simulation ===")
        optimizer.enabled = True
        for activity in [0.0, 0.3, 0.8, 1.0]:
            optimizer._activity_level = activity
            optimizer._current_mode = PowerMode.BALANCED
            rates = optimizer._mode_to_rates(PowerMode.BALANCED, activity)
            logger.info(f"Activity={activity:.1f} -> GPS={rates.gps_hz:.2f}Hz, IMU={rates.imu_hz:.1f}Hz")
        return
    
    OptimizerHTTPHandler.optimizer = optimizer
    
    server = HTTPServer(("127.0.0.1", args.port), OptimizerHTTPHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    logger.info(f"Sampling optimizer API on http://127.0.0.1:{args.port}")
    
    def update_loop():
        while True:
            if optimizer.enabled:
                optimizer.update()
            time.sleep(optimizer.poll_interval_sec)
    
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
        logger.info("Sampling optimizer stopped")


if __name__ == "__main__":
    main()
