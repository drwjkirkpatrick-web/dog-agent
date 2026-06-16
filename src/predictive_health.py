#!/usr/bin/env python3
"""
Dog Agent — Predictive Health Anomalies
======================================
Machine learning-based health anomaly detection.

Features:
  - Detect patterns before they become critical
  - Baseline deviation alerts
  - Trend prediction

Usage:
    python src/predictive_health.py
    python src/predictive_health.py --simulate
"""

from __future__ import annotations

import argparse
import json
import logging
import numpy as np
import signal
import sqlite3
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Dict, List, Optional
from collections import deque
import statistics

import yaml

# Logging
logger = logging.getLogger("predictive_health")
handler = logging.StreamHandler(sys.stdout)
handler.setFormatter(logging.Formatter(
    "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
))
logger.addHandler(handler)
logger.setLevel(logging.INFO)

PROJECT_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_DIR / "config.yaml"
DB_PATH = PROJECT_DIR / "data" / "predictive_health.db"


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


class PredictiveHealthAnalyzer:
    """Analyzes health data to predict anomalies."""
    
    def __init__(self):
        self.enabled = get_cfg("predictive_health.enabled", True)
        self.baseline_days = get_cfg("predictive_health.baseline_days", 7)
        self.anomaly_threshold = get_cfg("predictive_health.anomaly_threshold", 2.0)
        self.prediction_window_hours = get_cfg("predictive_health.prediction_window_hours", 24)
        
        self._db: Optional[sqlite3.Connection] = None
        self._baselines: Dict[str, Dict] = {}
        self._history: Dict[str, deque] = {}
        self._lock = threading.Lock()
        
        self._init_db()
    
    def _init_db(self) -> None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS anomaly_predictions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                metric TEXT NOT NULL,
                predicted_value REAL,
                confidence REAL,
                severity TEXT,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS detected_anomalies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                metric TEXT NOT NULL,
                actual_value REAL,
                expected_range TEXT,
                severity TEXT,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        self._db.commit()
    
    def calculate_baseline(self, metric: str, days: int = 7) -> Optional[Dict]:
        """Calculate baseline statistics for a metric."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        
        health_db = PROJECT_DIR / "data" / "health.db"
        if not health_db.exists():
            return None
        
        try:
            conn = sqlite3.connect(str(health_db))
            column_map = {
                "heart_rate": "heart_rate_bpm",
                "temperature": "temperature_c",
            }
            column = column_map.get(metric, metric)
            
            cursor = conn.execute(
                f"SELECT {column} FROM health_readings WHERE timestamp > ? AND {column} > 0",
                (cutoff,)
            )
            values = [row[0] for row in cursor if row[0] is not None]
            conn.close()
            
            if len(values) < 10:
                return None
            
            mean = statistics.mean(values)
            std = statistics.stdev(values) if len(values) > 1 else 0
            
            return {
                "metric": metric,
                "mean": mean,
                "std": std,
                "min": min(values),
                "max": max(values),
                "samples": len(values),
            }
        except Exception as e:
            logger.error(f"Failed to calculate baseline: {e}")
            return None
    
    def detect_anomaly(self, metric: str, value: float) -> Optional[Dict]:
        """Detect if a value is anomalous."""
        baseline = self._baselines.get(metric)
        if not baseline:
            baseline = self.calculate_baseline(metric)
            if baseline:
                self._baselines[metric] = baseline
        
        if not baseline:
            return None
        
        z_score = (value - baseline["mean"]) / baseline["std"] if baseline["std"] > 0 else 0
        
        if abs(z_score) < self.anomaly_threshold:
            return None
        
        severity = "warning" if abs(z_score) < self.anomaly_threshold * 1.5 else "critical"
        
        return {
            "metric": metric,
            "value": value,
            "expected_mean": baseline["mean"],
            "expected_std": baseline["std"],
            "z_score": z_score,
            "severity": severity,
        }
    
    def predict_next_value(self, metric: str) -> Optional[Dict]:
        """Predict next value using simple linear regression."""
        history = self._history.get(metric, deque(maxlen=100))
        
        if len(history) < 10:
            return None
        
        # Simple prediction using last N values
        recent = list(history)[-20:]
        if len(recent) < 5:
            return None
        
        # Linear trend
        n = len(recent)
        x_mean = (n - 1) / 2
        y_mean = statistics.mean(recent)
        
        numerator = sum((i - x_mean) * (v - y_mean) for i, v in enumerate(recent))
        denominator = sum((i - x_mean) ** 2 for i in range(n))
        slope = numerator / denominator if denominator > 0 else 0
        
        predicted = y_mean + slope * n
        
        return {
            "metric": metric,
            "predicted": predicted,
            "trend": "increasing" if slope > 0 else "decreasing",
            "confidence": min(len(recent) / 50, 0.95),
        }
    
    def analyze_current_readings(self) -> List[Dict]:
        """Analyze current health readings."""
        anomalies = []
        
        # Query latest readings
        health_db = PROJECT_DIR / "data" / "health.db"
        if not health_db.exists():
            return anomalies
        
        try:
            conn = sqlite3.connect(str(health_db))
            cursor = conn.execute(
                """SELECT heart_rate_bpm, temperature_c, timestamp
                   FROM health_readings
                   ORDER BY timestamp DESC LIMIT 1"""
            )
            row = cursor.fetchone()
            conn.close()
            
            if row:
                hr, temp, ts = row
                
                # Update history
                with self._lock:
                    if "heart_rate" not in self._history:
                        self._history["heart_rate"] = deque(maxlen=100)
                    if "temperature" not in self._history:
                        self._history["temperature"] = deque(maxlen=100)
                    
                    self._history["heart_rate"].append(hr)
                    self._history["temperature"].append(temp)
                
                # Check for anomalies
                for metric, value in [("heart_rate", hr), ("temperature", temp)]:
                    if value > 0:
                        anomaly = self.detect_anomaly(metric, value)
                        if anomaly:
                            anomalies.append(anomaly)
        
        except Exception as e:
            logger.error(f"Failed to analyze readings: {e}")
        
        return anomalies
    
    def get_summary(self) -> dict:
        """Get predictive health summary."""
        predictions = []
        for metric in ["heart_rate", "temperature"]:
            pred = self.predict_next_value(metric)
            if pred:
                predictions.append(pred)
        
        return {
            "predictions": predictions,
            "baselines": self._baselines,
            "last_analyzed": datetime.now(timezone.utc).isoformat(),
        }


class PredictiveHealthHTTPHandler(BaseHTTPRequestHandler):
    """HTTP API for predictive health."""
    
    analyzer: Optional[PredictiveHealthAnalyzer] = None
    
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
        
        if path == "predictive/health":
            self._send_json({
                "status": "ok",
                "service": "predictive_health",
                "enabled": bool(self.analyzer and self.analyzer.enabled),
            })
        elif path == "predictive/summary":
            if not self.analyzer:
                self._send_json({"error": "Analyzer not initialized"}, 503)
                return
            self._send_json(self.analyzer.get_summary())
        elif path == "predictive/anomalies":
            if not self.analyzer:
                self._send_json({"error": "Analyzer not initialized"}, 503)
                return
            anomalies = self.analyzer.analyze_current_readings()
            self._send_json({"anomalies": anomalies})
        else:
            self._send_json({"error": f"Unknown endpoint: {path}"}, 404)


def main():
    parser = argparse.ArgumentParser(description="Dog Agent — Predictive Health")
    parser.add_argument("--port", type=int, default=9147, help="HTTP API port")
    parser.add_argument("--simulate", action="store_true", help="Run simulation")
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose logging")
    args = parser.parse_args()
    
    if args.verbose:
        logger.setLevel(logging.DEBUG)
    
    analyzer = PredictiveHealthAnalyzer()
    
    if args.simulate:
        logger.info("=== Predictive Health Simulation ===")
        # Simulate readings
        for i in range(10):
            analyzer._history.setdefault("heart_rate", deque(maxlen=100)).append(80 + i * 2)
        pred = analyzer.predict_next_value("heart_rate")
        logger.info(f"Prediction: {pred}")
        return
    
    PredictiveHealthHTTPHandler.analyzer = analyzer
    
    server = HTTPServer(("127.0.0.1", args.port), PredictiveHealthHTTPHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    logger.info(f"Predictive health API on http://127.0.0.1:{args.port}")
    
    # Analysis loop
    def analysis_loop():
        while True:
            if analyzer.enabled:
                anomalies = analyzer.analyze_current_readings()
                for a in anomalies:
                    logger.warning(f"ANOMALY: {a['metric']} = {a['value']:.1f} (z={a['z_score']:.2f})")
            time.sleep(60)
    
    analysis_thread = threading.Thread(target=analysis_loop, daemon=True)
    analysis_thread.start()
    
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
        logger.info("Predictive health module stopped")


if __name__ == "__main__":
    main()
