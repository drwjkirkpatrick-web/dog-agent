#!/usr/bin/env python3
"""
Dog Agent — Health Trends (Long-Term Analysis)
==============================================
Long-term health data analysis and trend detection.

Features:
  - Aggregate data over weeks/months
  - Detect gradual changes in vitals
  - Spot aging patterns
  - Compare to breed baselines

Usage:
    python src/health_trends.py
    python src/health_trends.py --simulate
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import signal
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Dict, List, Optional
from collections import defaultdict
import statistics

import yaml

# Logging
logger = logging.getLogger("health_trends")
handler = logging.StreamHandler(sys.stdout)
handler.setFormatter(logging.Formatter(
    "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
))
logger.addHandler(handler)
logger.setLevel(logging.INFO)

PROJECT_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_DIR / "config.yaml"
DB_PATH = PROJECT_DIR / "data" / "health_trends.db"


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
class VitalTrends:
    metric: str
    daily_avg: List[float]
    weekly_avg: List[float]
    monthly_avg: List[float]
    trend_direction: str  # "improving", "stable", "declining"
    percent_change: float
    alerts: List[str]


class HealthTrendAnalyzer:
    """Analyzes long-term health trends."""
    
    def __init__(self):
        self.enabled = get_cfg("health_trends.enabled", True)
        self.analysis_interval_hours = get_cfg("health_trends.analysis_interval_hours", 24)
        
        self._db: Optional[sqlite3.Connection] = None
        self._lock = threading.Lock()
        
        self._init_db()
    
    def _init_db(self) -> None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS daily_aggregates (
                date TEXT PRIMARY KEY,
                avg_heart_rate REAL,
                min_heart_rate REAL,
                max_heart_rate REAL,
                avg_temperature REAL,
                avg_activity_minutes REAL,
                sleep_hours REAL
            );
            CREATE TABLE IF NOT EXISTS trend_alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                metric TEXT NOT NULL,
                direction TEXT NOT NULL,
                severity TEXT NOT NULL,
                message TEXT NOT NULL,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        self._db.commit()
    
    def aggregate_daily(self, date: datetime) -> None:
        """Aggregate health data for a specific day."""
        date_str = date.strftime("%Y-%m-%d")
        
        # Query raw health data from health_monitor database
        health_db = PROJECT_DIR / "data" / "health.db"
        
        if not health_db.exists():
            logger.warning(f"Health database not found: {health_db}")
            return
        
        try:
            conn = sqlite3.connect(str(health_db))
            conn.row_factory = sqlite3.Row
            
            # Get heart rate stats
            cursor = conn.execute(
                """SELECT AVG(heart_rate_bpm) as avg_hr,
                          MIN(heart_rate_bpm) as min_hr,
                          MAX(heart_rate_bpm) as max_hr
                   FROM health_readings
                   WHERE DATE(timestamp) = ? AND heart_rate_bpm > 0""",
                (date_str,)
            )
            hr_stats = cursor.fetchone()
            
            # Get temperature stats
            cursor = conn.execute(
                """SELECT AVG(temperature_c) as avg_temp
                   FROM health_readings
                   WHERE DATE(timestamp) = ? AND temperature_c > 0""",
                (date_str,)
            )
            temp_stats = cursor.fetchone()
            
            conn.close()
            
            # Insert aggregate
            self._db.execute(
                """INSERT OR REPLACE INTO daily_aggregates
                   (date, avg_heart_rate, min_heart_rate, max_heart_rate, avg_temperature)
                   VALUES (?, ?, ?, ?, ?)""",
                (date_str,
                 hr_stats["avg_hr"] if hr_stats else None,
                 hr_stats["min_hr"] if hr_stats else None,
                 hr_stats["max_hr"] if hr_stats else None,
                 temp_stats["avg_temp"] if temp_stats else None)
            )
            self._db.commit()
            
            logger.info(f"Aggregated health data for {date_str}")
            
        except Exception as e:
            logger.error(f"Failed to aggregate daily data: {e}")
    
    def calculate_trends(self, metric: str, days: int = 30) -> Optional[Dict]:
        """Calculate trend for a metric over time."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
        
        cursor = self._db.execute(
            f"""SELECT date, {metric} FROM daily_aggregates
               WHERE date > ? AND {metric} IS NOT NULL
               ORDER BY date""",
            (cutoff,)
        )
        
        values = [row[metric] for row in cursor if row[metric] is not None]
        
        if len(values) < 7:
            return None
        
        # Simple linear regression
        n = len(values)
        x_mean = (n - 1) / 2
        y_mean = statistics.mean(values)
        
        numerator = sum((i - x_mean) * (v - y_mean) for i, v in enumerate(values))
        denominator = sum((i - x_mean) ** 2 for i in range(n))
        
        if denominator == 0:
            slope = 0
        else:
            slope = numerator / denominator
        
        # Determine trend direction
        if abs(slope) < y_mean * 0.01:  # Less than 1% change per day
            direction = "stable"
        elif slope > 0:
            direction = "increasing"
        else:
            direction = "decreasing"
        
        # Calculate percent change
        if values[0] != 0:
            pct_change = ((values[-1] - values[0]) / values[0]) * 100
        else:
            pct_change = 0
        
        return {
            "metric": metric,
            "current_avg": round(y_mean, 2),
            "direction": direction,
            "slope_per_day": round(slope, 4),
            "percent_change": round(pct_change, 2),
            "days_analyzed": n,
        }
    
    def check_health_alerts(self) -> List[Dict]:
        """Check for concerning trends."""
        alerts = []
        
        # Check heart rate trends
        hr_trend = self.calculate_trends("avg_heart_rate", days=14)
        if hr_trend:
            if hr_trend["direction"] == "increasing" and hr_trend["percent_change"] > 10:
                alerts.append({
                    "metric": "heart_rate",
                    "severity": "warning",
                    "message": f"Heart rate trending up {hr_trend['percent_change']:.1f}% over 2 weeks",
                })
            elif hr_trend["direction"] == "decreasing" and hr_trend["percent_change"] < -15:
                alerts.append({
                    "metric": "heart_rate",
                    "severity": "warning",
                    "message": f"Heart rate trending down {abs(hr_trend['percent_change']):.1f}% over 2 weeks",
                })
        
        # Check temperature trends
        temp_trend = self.calculate_trends("avg_temperature", days=14)
        if temp_trend and abs(temp_trend["percent_change"]) > 5:
            alerts.append({
                "metric": "temperature",
                "severity": "info",
                "message": f"Body temperature changed {temp_trend['percent_change']:.1f}% over 2 weeks",
            })
        
        return alerts
    
    def get_summary(self) -> Dict:
        """Get comprehensive health trends summary."""
        return {
            "heart_rate": self.calculate_trends("avg_heart_rate"),
            "temperature": self.calculate_trends("avg_temperature"),
            "alerts": self.check_health_alerts(),
            "last_updated": datetime.now(timezone.utc).isoformat(),
        }


class HealthTrendsHTTPHandler(BaseHTTPRequestHandler):
    """HTTP API for health trends."""
    
    analyzer: Optional[HealthTrendAnalyzer] = None
    
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
        
        if path == "trends/health":
            self._send_json({
                "status": "ok",
                "service": "health_trends",
                "enabled": bool(self.analyzer and self.analyzer.enabled),
            })
        elif path == "trends/summary":
            if not self.analyzer:
                self._send_json({"error": "Analyzer not initialized"}, 503)
                return
            self._send_json(self.analyzer.get_summary())
        elif path == "trends/alerts":
            if not self.analyzer:
                self._send_json({"error": "Analyzer not initialized"}, 503)
                return
            self._send_json({"alerts": self.analyzer.check_health_alerts()})
        else:
            self._send_json({"error": f"Unknown endpoint: {path}"}, 404)


def main():
    parser = argparse.ArgumentParser(description="Dog Agent — Health Trends")
    parser.add_argument("--port", type=int, default=9144, help="HTTP API port")
    parser.add_argument("--simulate", action="store_true", help="Run simulation")
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose logging")
    args = parser.parse_args()
    
    if args.verbose:
        logger.setLevel(logging.DEBUG)
    
    analyzer = HealthTrendAnalyzer()
    
    if args.simulate:
        logger.info("=== Health Trends Simulation ===")
        trends = analyzer.calculate_trends("avg_heart_rate", days=30)
        if trends:
            logger.info(f"Heart rate trend: {trends['direction']} ({trends['percent_change']}%)")
        return
    
    HealthTrendsHTTPHandler.analyzer = analyzer
    
    server = HTTPServer(("127.0.0.1", args.port), HealthTrendsHTTPHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    logger.info(f"Health trends API on http://127.0.0.1:{args.port}")
    
    # Analysis loop
    def analysis_loop():
        while True:
            if analyzer.enabled:
                # Aggregate yesterday's data
                yesterday = datetime.now(timezone.utc) - timedelta(days=1)
                analyzer.aggregate_daily(yesterday)
            time.sleep(analyzer.analysis_interval_hours * 3600)
    
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
        logger.info("Health trends module stopped")


if __name__ == "__main__":
    main()
