#!/usr/bin/env python3
"""
Dog Agent — Sleep Posture & Rest Quality Analyzer
=================================================
Analyzes BNO055 orientation during rest periods.

Features:
  - Sleeping position detection (curled, stretched, on side)
  - Restlessness scoring
  - Tremor detection
  - Rest quality for recovery assessment

Usage:
    python src/sleep_posture.py
    python src/sleep_posture.py --simulate
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import signal
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

# Logging
logger = logging.getLogger("sleep_posture")
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


@dataclass
class RestSession:
    """Container for rest quality metrics."""
    start: datetime
    end: Optional[datetime]
    posture_counts: Dict[str, int]
    position_changes: int
    tremor_seconds: float
    restlessness_score: float
    quality_score: float  # 0-100
    
    def to_dict(self) -> dict:
        return {
            "start": self.start.isoformat(),
            "end": self.end.isoformat() if self.end else None,
            "posture_counts": self.posture_counts,
            "position_changes": self.position_changes,
            "tremor_seconds": round(self.tremor_seconds, 1),
            "restlessness_score": round(self.restlessness_score, 1),
            "quality_score": round(self.quality_score, 1),
        }


class SleepPostureAnalyzer:
    """Analyzes BNO055 orientation during rest."""
    
    POSTURES = {
        "curled": {"roll": (0, 45), "pitch": (-30, 30)},
        "stretched": {"roll": (135, 180), "pitch": (-20, 20)},
        "on_side": {"roll": (45, 135), "pitch": (-45, 45)},
        "upside_down": {"roll": (135, 180), "pitch": (150, 180)},
    }
    
    def __init__(self):
        self.enabled = get_cfg("sleep_posture.enabled", False)
        self.poll_interval_sec = get_cfg("sleep_posture.poll_interval_sec", 1.0)
        self.min_rest_sec = get_cfg("sleep_posture.min_rest_duration_sec", 300)
        self.tremor_threshold_g = get_cfg("sleep_posture.tremor_threshold_g", 0.05)
        
        self._current_session: Optional[RestSession] = None
        self._sessions: deque[RestSession] = deque(maxlen=100)
        self._motion_history: deque[float] = deque(maxlen=60)  # 60 seconds
        self._last_posture: Optional[str] = None
        self._last_position_change: float = 0
        self._lock = threading.Lock()
        
        self._imu = None
        if self.enabled:
            self._init_imu()
    
    def _init_imu(self):
        try:
            from bno055 import BNO055
            self._imu = BNO055(get_cfg("sleep_posture.imu_address", 0x28))
            logger.info("BNO055 IMU initialized for sleep posture analysis")
        except Exception as e:
            logger.warning(f"Failed to init IMU: {e}")
            self._imu = None
    
    def _euler_to_posture(self, roll: float, pitch: float) -> str:
        """Classify orientation into posture."""
        # Normalize to 0-180
        roll = abs(roll)
        pitch = abs(pitch)
        
        if pitch > 150:
            return "upside_down"
        if roll > 135:
            return "stretched"
        if 45 <= roll <= 135:
            return "on_side"
        return "curled"
    
    def _read_imu(self) -> Optional[Dict[str, float]]:
        """Read orientation and motion from IMU."""
        if not self._imu:
            return None
        try:
            euler = self._imu.get_euler()
            accel = self._imu.get_acceleration()
            motion = math.sqrt(accel[0]**2 + accel[1]**2 + accel[2]**2) - 1.0
            return {
                "roll": euler[0],
                "pitch": euler[1],
                "yaw": euler[2],
                "motion": abs(motion),
            }
        except Exception as e:
            logger.error(f"IMU read error: {e}")
            return None
    
    def update(self):
        """Process one IMU reading."""
        if not self.enabled:
            return
        
        reading = self._read_imu()
        if not reading:
            return
        
        posture = self._euler_to_posture(reading["roll"], reading["pitch"])
        motion = reading["motion"]
        now = time.time()
        
        with self._lock:
            self._motion_history.append(motion)
            
            # Detect rest start/end
            is_resting = motion < 0.1  # g threshold
            
            if is_resting and self._current_session is None:
                self._current_session = RestSession(
                    start=datetime.now(timezone.utc),
                    end=None,
                    posture_counts={k: 0 for k in self.POSTURES.keys()},
                    position_changes=0,
                    tremor_seconds=0,
                    restlessness_score=0,
                    quality_score=0,
                )
            
            if self._current_session:
                self._current_session.posture_counts[posture] += 1
                
                # Position change detection
                if self._last_posture and self._last_posture != posture:
                    if now - self._last_position_change > 5:
                        self._current_session.position_changes += 1
                        self._last_position_change = now
                
                self._last_posture = posture
                
                # Tremor detection
                if motion > self.tremor_threshold_g and motion < 0.5:
                    self._current_session.tremor_seconds += self.poll_interval_sec
                
                # Restlessness score
                avg_motion = sum(self._motion_history) / len(self._motion_history) if self._motion_history else 0
                self._current_session.restlessness_score = min(100, avg_motion * 1000)
                
                if not is_resting:
                    # End session if active for > 30 sec
                    self._current_session.end = datetime.now(timezone.utc)
                    duration = (self._current_session.end - self._current_session.start).total_seconds()
                    
                    if duration >= self.min_rest_sec:
                        self._current_session.quality_score = max(0, 100 - self._current_session.restlessness_score - 
                            self._current_session.position_changes * 2 - self._current_session.tremor_seconds * 5)
                        self._sessions.append(self._current_session)
                    
                    self._current_session = None
    
    def get_current(self) -> Optional[dict]:
        with self._lock:
            if not self._current_session:
                return None
            return self._current_session.to_dict()
    
    def get_last_nights(self, days: int = 7) -> List[dict]:
        with self._lock:
            return [s.to_dict() for s in list(self._sessions)[-days:]]
    
    def get_stats(self) -> dict:
        with self._lock:
            sessions = list(self._sessions)
            avg_quality = sum(s.quality_score for s in sessions) / len(sessions) if sessions else 0
        
        return {
            "enabled": self.enabled,
            "current_session": self.get_current(),
            "session_count": len(sessions),
            "average_quality": round(avg_quality, 1),
            "recent_sessions": self.get_last_nights(3),
        }


class SleepHTTPHandler(BaseHTTPRequestHandler):
    """HTTP API for sleep posture analyzer."""
    
    analyzer: Optional[SleepPostureAnalyzer] = None
    
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
        
        if path == "sleep/health":
            self._send_json({
                "status": "ok",
                "service": "sleep_posture",
                "enabled": bool(self.analyzer and self.analyzer.enabled),
            })
        elif path == "sleep/current":
            if not self.analyzer:
                self._send_json({"error": "Analyzer not initialized"}, 503)
                return
            current = self.analyzer.get_current()
            self._send_json({"current_session": current})
        elif path == "sleep/stats":
            if not self.analyzer:
                self._send_json({"error": "Analyzer not initialized"}, 503)
                return
            self._send_json(self.analyzer.get_stats())
        else:
            self._send_json({"error": f"Unknown endpoint: {path}"}, 404)


def main():
    parser = argparse.ArgumentParser(description="Dog Agent — Sleep Posture Analyzer")
    parser.add_argument("--port", type=int, default=9154, help="HTTP API port")
    parser.add_argument("--simulate", action="store_true", help="Run simulation")
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose logging")
    args = parser.parse_args()
    
    if args.verbose:
        logger.setLevel(logging.DEBUG)
    
    analyzer = SleepPostureAnalyzer()
    
    if args.simulate:
        logger.info("=== Sleep Posture Simulation ===")
        # Simulate readings
        analyzer.enabled = True
        for i in range(10):
            reading = {
                "roll": 10 + i * 2,
                "pitch": 5,
                "yaw": 0,
                "motion": 0.02,
            }
            posture = analyzer._euler_to_posture(reading["roll"], reading["pitch"])
            logger.info(f"Posture: {posture}, motion: {reading['motion']}")
            time.sleep(0.1)
        return
    
    SleepHTTPHandler.analyzer = analyzer
    
    server = HTTPServer(("127.0.0.1", args.port), SleepHTTPHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    logger.info(f"Sleep posture API on http://127.0.0.1:{args.port}")
    
    def update_loop():
        while True:
            if analyzer.enabled:
                analyzer.update()
            time.sleep(analyzer.poll_interval_sec)
    
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
        logger.info("Sleep posture module stopped")


if __name__ == "__main__":
    main()
