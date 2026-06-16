#!/usr/bin/env python3
"""
Data Logger — Dog Agent
========================
Centralized data logging service for all dog-agent modules. Writes GPS tracks,
health logs, events, and alerts to daily-rotated files with thread-safe writes
via a queue + consumer thread pattern.

Log files are organized as:
    data/gps_tracks/gps_track_YYYY-MM-DD.csv
    data/health_logs/health_log_YYYY-MM-DD.csv
    data/events/events_YYYY-MM-DD.log       (JSON lines)
    data/alerts/alerts_YYYY-MM-DD.log       (JSON lines)

Features:
    • Automatic daily file rotation at midnight UTC
    • Thread-safe writes via queue + consumer thread
    • Configurable retention with auto-purge of old files
    • HTTP API on localhost:9110/logger for stats, purge, health
    • --test mode that writes sample data to all log types

Usage:
    python src/data_logger.py                         # Normal mode
    python src/data_logger.py --test                  # Test mode (sample data)
    python src/data_logger.py --config /path/to/config.yaml
    python src/data_logger.py --port 9110             # Custom HTTP port
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import queue
import signal
import sys
import threading
import time
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logger = logging.getLogger("data_logger")
_handler = logging.StreamHandler(sys.stdout)
_handler.setFormatter(logging.Formatter(
    "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
))
logger.addHandler(_handler)
logger.setLevel(logging.INFO)


# ---------------------------------------------------------------------------
# File writer queue item
# ---------------------------------------------------------------------------
class WriteJob:
    """A unit of work for the consumer thread.

    Attributes:
        category: One of 'gps', 'health', 'event', 'alert'.
        data: The row data (dict for CSV, dict for JSON line).
    """
    __slots__ = ("category", "data")

    def __init__(self, category: str, data: Dict[str, Any]) -> None:
        self.category = category
        self.data = data


# ---------------------------------------------------------------------------
# Writer worker — queue consumer
# ---------------------------------------------------------------------------
class LogWriter:
    """Manages file handles and writes for all log categories.

    Uses a queue + consumer thread pattern so all callers (regardless of
    which thread they come from) can log without blocking on I/O.

    File rotation happens automatically when the UTC date changes.
    """

    GPS_FIELDS = [
        "timestamp", "lat", "lon", "altitude", "speed_knots",
        "speed_mps", "heading", "fix_quality", "satellites", "valid",
    ]
    HEALTH_FIELDS = [
        "timestamp", "heart_rate_bpm", "temperature_c", "temperature_f",
        "accel_magnitude_g", "speed_mps", "lat", "lon",
        "sensors_valid", "gps_valid",
    ]

    def __init__(self, base_dir: str, retention_days: int = 90) -> None:
        self._base_dir = Path(base_dir)
        self._retention_days = retention_days

        # Ensure directories exist
        self._gps_dir = self._base_dir / "gps_tracks"
        self._health_dir = self._base_dir / "health_logs"
        self._events_dir = self._base_dir / "events"
        self._alerts_dir = self._base_dir / "alerts"
        for d in [self._gps_dir, self._health_dir, self._events_dir, self._alerts_dir]:
            d.mkdir(parents=True, exist_ok=True)

        # Current file handles (one per category)
        self._files: Dict[str, Any] = {}
        self._csv_writers: Dict[str, Any] = {}
        self._current_date: Optional[str] = None

        # Row counters (for get_today_counts)
        self._counts: Dict[str, int] = {
            "gps": 0, "health": 0, "event": 0, "alert": 0,
        }
        self._counts_lock = threading.Lock()

        # Queue + consumer thread
        self._queue: queue.Queue = queue.Queue()
        self._stop_event = threading.Event()
        self._consumer_thread = threading.Thread(
            target=self._consumer_loop, name="log-writer", daemon=True,
        )
        self._consumer_thread.start()

        # Last purge timestamp (avoid purging on every midnight roll)
        self._last_purge_date: Optional[str] = None

        logger.info(
            "LogWriter initialised — base_dir=%s retention_days=%d",
            self._base_dir, self._retention_days,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def enqueue(self, category: str, data: Dict[str, Any]) -> None:
        """Enqueue a write job.

        Thread-safe. Returns immediately — the consumer thread does I/O.
        """
        if self._stop_event.is_set():
            logger.warning("LogWriter is stopped; dropping %s write", category)
            return
        with self._counts_lock:
            if category in self._counts:
                self._counts[category] += 1
        self._queue.put_nowait(WriteJob(category, data))

    def get_counts(self) -> Dict[str, int]:
        """Return a copy of today's row counts per category."""
        with self._counts_lock:
            return dict(self._counts)

    def stop(self) -> None:
        """Signal the consumer to drain the queue and stop."""
        logger.info("LogWriter stopping...")
        self._stop_event.set()
        self._consumer_thread.join(timeout=5.0)
        self._close_all()

    # ------------------------------------------------------------------
    # Consumer loop
    # ------------------------------------------------------------------

    def _consumer_loop(self) -> None:
        """Main loop: pull jobs from the queue and write them."""
        while not self._stop_event.is_set():
            try:
                job = self._queue.get(timeout=1.0)
            except queue.Empty:
                # Check for midnight rotation even when queue is idle
                self._check_rotation()
                continue

            try:
                self._check_rotation()
                self._write(job)
            except Exception:
                logger.exception("Failed to write %s log entry", job.category)
            finally:
                self._queue.task_done()

        # Drain remaining jobs after stop
        while True:
            try:
                job = self._queue.get_nowait()
            except queue.Empty:
                break
            try:
                self._write(job)
            except Exception:
                logger.exception("Failed to write %s log entry during drain", job.category)
            finally:
                self._queue.task_done()

    # ------------------------------------------------------------------
    # Write logic
    # ------------------------------------------------------------------

    def _check_rotation(self) -> None:
        """Rotate files if the UTC date has changed."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if today != self._current_date:
            self._rotate(today)

    def _rotate(self, today: str) -> None:
        """Close old files and open new ones for *today*."""
        self._close_all()
        self._current_date = today
        self._csv_writers.clear()

        # GPS CSV
        gps_path = self._gps_dir / f"gps_track_{today}.csv"
        self._files["gps"] = open(gps_path, "a", newline="")  # noqa: SIM115
        writer = csv.DictWriter(
            self._files["gps"], fieldnames=self.GPS_FIELDS,
        )
        if gps_path.stat().st_size == 0:
            writer.writeheader()
        self._csv_writers["gps"] = writer

        # Health CSV
        health_path = self._health_dir / f"health_log_{today}.csv"
        self._files["health"] = open(health_path, "a", newline="")  # noqa: SIM115
        writer = csv.DictWriter(
            self._files["health"], fieldnames=self.HEALTH_FIELDS,
        )
        if health_path.stat().st_size == 0:
            writer.writeheader()
        self._csv_writers["health"] = writer

        # Events JSON lines
        events_path = self._events_dir / f"events_{today}.log"
        self._files["event"] = open(events_path, "a")  # noqa: SIM115

        # Alerts JSON lines
        alerts_path = self._alerts_dir / f"alerts_{today}.log"
        self._files["alert"] = open(alerts_path, "a")  # noqa: SIM115

        # Reset counters at midnight
        with self._counts_lock:
            for key in self._counts:
                self._counts[key] = 0

        # Auto-purge old files once per day
        if today != self._last_purge_date:
            self._last_purge_date = today
            self._purge_old_internal()

        logger.info("Rotated logs to date=%s", today)

    def _write(self, job: WriteJob) -> None:
        """Write a single job to the appropriate file."""
        cat = job.category
        data = job.data

        if cat in ("gps", "health"):
            writer = self._csv_writers.get(cat)
            if writer:
                writer.writerow(data)
                self._files[cat].flush()
        elif cat in ("event", "alert"):
            fh = self._files.get(cat)
            if fh:
                line = json.dumps(data, ensure_ascii=False, default=str)
                fh.write(line + "\n")
                fh.flush()

    def _close_all(self) -> None:
        """Close all open file handles."""
        for fh in self._files.values():
            with suppress(Exception):
                fh.close()
        self._files.clear()

    # ------------------------------------------------------------------
    # Retention / Purge
    # ------------------------------------------------------------------

    def _purge_old_internal(self) -> None:
        """Delete files older than retention_days across all categories."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=self._retention_days)
        cutoff_date = cutoff.strftime("%Y-%m-%d")
        logger.info(
            "Purging files older than %s (%d days)", cutoff_date, self._retention_days,
        )

        categories = {
            "gps_tracks": ("gps_track_", ".csv"),
            "health_logs": ("health_log_", ".csv"),
            "events": ("events_", ".log"),
            "alerts": ("alerts_", ".log"),
        }

        total_deleted = 0
        for subdir, (prefix, suffix) in categories.items():
            dir_path = self._base_dir / subdir
            if not dir_path.exists():
                continue
            for fpath in dir_path.iterdir():
                if not fpath.is_file():
                    continue
                fname = fpath.name
                if fname.startswith(prefix) and fname.endswith(suffix):
                    # Extract date: prefix_YYYY-MM-DD.suffix
                    date_str = fname[len(prefix):-len(suffix)]
                    if date_str < cutoff_date:
                        try:
                            fpath.unlink()
                            total_deleted += 1
                            logger.debug("Purged %s", fpath)
                        except OSError:
                            logger.warning("Could not delete %s", fpath)

        if total_deleted:
            logger.info("Purged %d old log files", total_deleted)

    def purge_old(self, force_days: Optional[int] = None) -> int:
        """Explicitly trigger purge.

        Args:
            force_days: Override retention_days for this purge. If None, uses
                        the configured retention_days.

        Returns:
            Number of files deleted.
        """
        days = force_days if force_days is not None else self._retention_days
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        cutoff_date = cutoff.strftime("%Y-%m-%d")

        categories = {
            "gps_tracks": ("gps_track_", ".csv"),
            "health_logs": ("health_log_", ".csv"),
            "events": ("events_", ".log"),
            "alerts": ("alerts_", ".log"),
        }

        deleted = 0
        for subdir, (prefix, suffix) in categories.items():
            dir_path = self._base_dir / subdir
            if not dir_path.exists():
                continue
            for fpath in dir_path.iterdir():
                if not fpath.is_file():
                    continue
                fname = fpath.name
                if fname.startswith(prefix) and fname.endswith(suffix):
                    date_str = fname[len(prefix):-len(suffix)]
                    if date_str < cutoff_date:
                        try:
                            fpath.unlink()
                            deleted += 1
                        except OSError:
                            logger.warning("Could not delete %s", fpath)

        logger.info("Explicit purge removed %d files (cutoff=%s)", deleted, cutoff_date)
        return deleted

    # ------------------------------------------------------------------
    # Storage summary
    # ------------------------------------------------------------------

    def get_storage_summary(self) -> Dict[str, Any]:
        """Return total files and disk usage per category."""
        categories = {
            "gps_tracks": ("gps_track_", ".csv"),
            "health_logs": ("health_log_", ".csv"),
            "events": ("events_", ".log"),
            "alerts": ("alerts_", ".log"),
        }

        summary: Dict[str, Any] = {}
        total_files = 0
        total_bytes = 0

        for cat_name, (prefix, suffix) in categories.items():
            dir_path = self._base_dir / cat_name
            files_list: List[Dict[str, Any]] = []
            cat_files = 0
            cat_bytes = 0

            if dir_path.exists():
                for fpath in dir_path.iterdir():
                    if not fpath.is_file():
                        continue
                    fname = fpath.name
                    if fname.startswith(prefix) and fname.endswith(suffix):
                        size = fpath.stat().st_size
                        cat_files += 1
                        cat_bytes += size
                        files_list.append({
                            "name": fname,
                            "size_bytes": size,
                            "last_modified": datetime.fromtimestamp(
                                fpath.stat().st_mtime, tz=timezone.utc,
                            ).isoformat(),
                        })

            # Sort by name (date descending)
            files_list.sort(key=lambda x: x["name"], reverse=True)

            summary[cat_name] = {
                "file_count": cat_files,
                "total_bytes": cat_bytes,
                "total_kb": round(cat_bytes / 1024, 1),
                "files": files_list[:20],  # limit to 20 most recent
            }
            total_files += cat_files
            total_bytes += cat_bytes

        summary["_meta"] = {
            "total_files": total_files,
            "total_bytes": total_bytes,
            "total_kb": round(total_bytes / 1024, 1),
            "total_mb": round(total_bytes / 1024 / 1024, 2),
            "retention_days": self._retention_days,
        }
        return summary


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------
class LoggerHTTPHandler(BaseHTTPRequestHandler):
    """Serves /logger/* endpoints on localhost."""

    writer: LogWriter  # Set by the server before serving

    def do_GET(self) -> None:
        if self.path == "/logger/stats":
            self._send_json({
                "storage": self.writer.get_storage_summary(),
                "today_counts": self.writer.get_counts(),
            })
        elif self.path == "/logger/health":
            self._send_json({
                "status": "ok",
                "service": "data_logger",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })
        else:
            self._send_json({"error": "not_found", "path": self.path}, 404)

    def do_POST(self) -> None:
        if self.path == "/logger/purge":
            deleted = self.writer.purge_old()
            self._send_json({
                "status": "ok",
                "files_deleted": deleted,
            })
        else:
            self._send_json({"error": "not_found", "path": self.path}, 404)

    def _send_json(self, data: Dict[str, Any], status: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: Any) -> None:
        """Route HTTP access logs to our logger."""
        logger.debug("HTTP %s", fmt % args)


# ---------------------------------------------------------------------------
# Test mode
# ---------------------------------------------------------------------------
def run_test_mode(writer: LogWriter, count: int = 20) -> None:
    """Write sample data to all log types for testing."""
    import random

    logger.info("Test mode: writing %d sample entries to each log type...", count)

    base_time = datetime.now(timezone.utc)
    rng = random.Random(42)

    for i in range(count):
        t = base_time + timedelta(seconds=i * 5)

        # GPS track
        lat = 45.5152 + rng.gauss(0, 0.0002)
        lon = -122.6784 + rng.gauss(0, 0.0002)
        speed_knots = rng.uniform(0, 3.0)
        speed_mps = speed_knots * 0.514444
        writer.enqueue("gps", {
            "timestamp": t.isoformat(),
            "lat": round(lat, 8),
            "lon": round(lon, 8),
            "altitude": round(rng.uniform(80, 120), 1),
            "speed_knots": round(speed_knots, 2),
            "speed_mps": round(speed_mps, 2),
            "heading": round(rng.uniform(0, 360), 1),
            "fix_quality": rng.choice([1, 1, 1, 2, 0]),
            "satellites": rng.randint(4, 12),
            "valid": True,
        })

        # Health log
        hr = 70 + rng.gauss(0, 10) + (10 if rng.random() < 0.3 else 0)
        temp = 38.5 + rng.gauss(0, 0.2)
        writer.enqueue("health", {
            "timestamp": t.isoformat(),
            "heart_rate_bpm": round(hr, 1),
            "temperature_c": round(temp, 2),
            "temperature_f": round(temp * 9.0 / 5.0 + 32.0, 1),
            "accel_magnitude_g": round(0.98 + rng.random() * 0.5, 3),
            "speed_mps": round(speed_mps, 2),
            "lat": round(lat, 8),
            "lon": round(lon, 8),
            "sensors_valid": True,
            "gps_valid": True,
        })

        # Event (occasionally)
        if i % 5 == 0:
            writer.enqueue("event", {
                "timestamp": t.isoformat(),
                "event_type": rng.choice(["zone_enter", "zone_exit", "movement"]),
                "message": f"Sample event #{i}",
                "data": {
                    "zone": rng.choice(["home", "yard", "perimeter"]),
                    "distance_m": round(rng.uniform(5, 100), 1),
                },
            })

        # Alert (occasionally)
        if i % 7 == 0:
            writer.enqueue("alert", {
                "timestamp": t.isoformat(),
                "severity": rng.choice(["info", "warning", "critical"]),
                "message": f"Sample alert #{i}: {rng.choice(['high HR', 'low temp', 'escape detected', 'battery low'])}",
                "alert_type": rng.choice(["hr_high", "fever", "escape", "battery"]),
            })

    # Wait for the consumer to drain the queue
    writer._queue.join()  # noqa: SLF001
    logger.info("Test mode complete — wrote sample data to all log types.")


# ---------------------------------------------------------------------------
# Config loader
# ---------------------------------------------------------------------------
def load_config(config_path: str) -> Dict[str, Any]:
    """Load YAML configuration, falling back to sensible defaults."""
    defaults: Dict[str, Any] = {
        "logging": {
            "log_dir": "data",
            "gps_track_dir": "data/gps_tracks",
            "health_log_dir": "data/health_logs",
            "retention_days": 90,
        },
    }

    path = Path(config_path)
    if not path.exists():
        logger.warning("Config not found at %s; using defaults", config_path)
        return defaults

    try:
        with open(path) as f:
            cfg = yaml.safe_load(f) or {}
    except Exception:
        logger.warning("Failed to parse %s; using defaults", config_path)
        return defaults

    # Merge: fill missing keys from defaults
    merged = dict(defaults)
    if "logging" in cfg:
        merged["logging"].update(cfg["logging"])
    return merged


# ---------------------------------------------------------------------------
# Signal handler for graceful shutdown
# ---------------------------------------------------------------------------
_shutdown_event = threading.Event()


def _signal_handler(signum: int, frame: Any) -> None:
    logger.info("Received signal %d; shutting down...", signum)
    _shutdown_event.set()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(
        description="Dog Agent — Data Logger",
    )
    parser.add_argument(
        "--config", default="config.yaml",
        help="Path to YAML config file (default: config.yaml)",
    )
    parser.add_argument(
        "--port", type=int, default=9110,
        help="HTTP API port (default: 9110)",
    )
    parser.add_argument(
        "--test", action="store_true",
        help="Run test mode: write sample data to all logs, then exit",
    )
    parser.add_argument(
        "--data-dir", default=None,
        help="Override data directory (default: from config or 'data')",
    )
    args = parser.parse_args()

    # Load config
    config = load_config(args.config)
    logging_cfg = config.get("logging", {})
    retention_days = logging_cfg.get("retention_days", 90)
    data_dir = args.data_dir or logging_cfg.get("log_dir", "data")

    # Resolve data dir relative to project root if relative
    data_path = Path(data_dir)
    if not data_path.is_absolute():
        # Assume running from project root
        data_path = Path.cwd() / data_dir

    # Create writer
    writer = LogWriter(str(data_path), retention_days=retention_days)

    # Test mode: write sample data and exit
    if args.test:
        run_test_mode(writer, count=20)
        writer.stop()
        logger.info("Test mode finished. Check %s for output files.", data_path)
        return

    # Start HTTP server
    server = HTTPServer(("127.0.0.1", args.port), LoggerHTTPHandler)
    LoggerHTTPHandler.writer = writer

    server_thread = threading.Thread(
        target=server.serve_forever, name="http-server", daemon=True,
    )
    server_thread.start()
    logger.info(
        "Data Logger HTTP API listening on http://127.0.0.1:%d/logger",
        args.port,
    )

    # Register signal handlers
    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    # Block until shutdown signal
    _shutdown_event.wait()

    logger.info("Shutting down...")
    server.shutdown()
    writer.stop()
    logger.info("Shutdown complete.")


if __name__ == "__main__":
    main()