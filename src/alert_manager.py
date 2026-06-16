#!/usr/bin/env python3
"""
Alert Manager — Dog Agent
=========================
Receives alerts from other modules (health_monitor, geofence, behavior) and
routes them to configured destinations (Telegram, local log file, SMS stub,
stdout). Manages deduplication with configurable cooldown per alert type.

Port assignment: 9118 (reserved for alert_manager)

Usage:
    python src/alert_manager.py                    # Normal mode
    python src/alert_manager.py --test             # Send sample alerts to verify routing
    python src/alert_manager.py --config /path/to/config.yaml
    python src/alert_manager.py --port 9118

Config (config.yaml → alerts section):
    alerts:
      telegram:
        enabled: true
        bot_token: "YOUR_TELEGRAM_BOT_TOKEN"
        chat_id: "YOUR_CHAT_ID"
      local_log: true
      sms_enabled: false
      cooldown_minutes: 15        # dedup cooldown (default: 15)

API endpoints (http://127.0.0.1:<port>):
    GET /alerts          — return recent alerts (default: last 10)
    GET /alerts/health   — health check
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import threading
import time
from contextlib import suppress
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

try:
    import requests
except ImportError:
    requests = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logger = logging.getLogger("alert_manager")
_handler = logging.StreamHandler(sys.stdout)
_handler.setFormatter(logging.Formatter(
    "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
))
logger.addHandler(_handler)
logger.setLevel(logging.INFO)


# ---------------------------------------------------------------------------
# Paths & constants
# ---------------------------------------------------------------------------
PROJECT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_DIR / "config.yaml"
DEFAULT_ALERTS_LOG_DIR = PROJECT_DIR / "data" / "alerts"
DEFAULT_ALERTS_LOG_FILE = DEFAULT_ALERTS_LOG_DIR / "alerts_%(date)s.log"
DEFAULT_PORT = 9118

VALID_SEVERITIES = ("info", "low", "medium", "high", "critical")
DEFAULT_COOLDOWN_MINUTES = 15


# ---------------------------------------------------------------------------
# Alert data structure
# ---------------------------------------------------------------------------
class Alert:
    """A single alert event with metadata."""

    __slots__ = (
        "alert_type", "severity", "message", "data",
        "timestamp", "destinations_sent",
    )

    def __init__(
        self,
        alert_type: str,
        severity: str,
        message: str,
        data: Optional[Dict[str, Any]] = None,
        timestamp: Optional[datetime] = None,
    ) -> None:
        if severity not in VALID_SEVERITIES:
            raise ValueError(
                f"Invalid severity '{severity}'. Must be one of {VALID_SEVERITIES}"
            )
        self.alert_type = alert_type
        self.severity = severity
        self.message = message
        self.data = data or {}
        self.timestamp = timestamp or datetime.now(timezone.utc)
        self.destinations_sent: List[str] = []

    def to_dict(self) -> Dict[str, Any]:
        return {
            "alert_type": self.alert_type,
            "severity": self.severity,
            "message": self.message,
            "data": self.data,
            "timestamp": self.timestamp.isoformat(),
            "destinations_sent": self.destinations_sent,
        }

    def to_json_line(self) -> str:
        """Return a single JSON line for log file storage."""
        return json.dumps({
            "alert_type": self.alert_type,
            "severity": self.severity,
            "message": self.message,
            "data": self.data,
            "timestamp": self.timestamp.isoformat(),
        }, default=str)


# ---------------------------------------------------------------------------
# Alert store — ring buffer with dedup tracking
# ---------------------------------------------------------------------------
class AlertStore:
    """Thread-safe store that maintains recent alerts and dedup state.

    Deduplication: when ``send_alert`` is called for the same ``alert_type``
    within the cooldown window, the alert is NOT re-sent to destinations
    (though it is still stored for history).
    """

    def __init__(self, max_history: int = 1000, cooldown_minutes: int = DEFAULT_COOLDOWN_MINUTES) -> None:
        self._lock = threading.Lock()
        self._alerts: List[Alert] = []
        self._max_history = max_history
        self._cooldown_seconds = cooldown_minutes * 60.0
        # Tracks last-sent timestamp per alert_type for dedup
        self._last_sent: Dict[str, float] = {}

    def add(self, alert: Alert) -> bool:
        """Add an alert to the store.

        Returns True if the alert is *new* (should be sent to destinations),
        False if it's a duplicate (within cooldown, already sent).
        """
        with self._lock:
            # Dedup check
            last_ts = self._last_sent.get(alert.alert_type)
            now = alert.timestamp.timestamp()
            if last_ts is not None and (now - last_ts) < self._cooldown_seconds:
                # Still within cooldown — store but mark as duplicate
                alert.destinations_sent.append("_duplicate")
                self._alerts.append(alert)
                self._trim()
                return False

            # Mark as new send
            self._last_sent[alert.alert_type] = now
            self._alerts.append(alert)
            self._trim()
            return True

    def _trim(self) -> None:
        while len(self._alerts) > self._max_history:
            self._alerts.pop(0)

    def get_recent(self, count: int = 10) -> List[Alert]:
        """Return the most recent *count* alerts."""
        with self._lock:
            return list(self._alerts[-count:])

    def get_recent_since(self, since_ts: float) -> List[Alert]:
        """Return alerts with timestamp >= *since_ts* (unix epoch)."""
        with self._lock:
            return [
                a for a in self._alerts
                if a.timestamp.timestamp() >= since_ts
            ]

    def count(self) -> int:
        with self._lock:
            return len(self._alerts)

    def dedup_stats(self) -> Dict[str, Any]:
        """Return dedup state for debugging."""
        with self._lock:
            return {
                "total_alerts": len(self._alerts),
                "tracked_types": {
                    at: {"last_sent_epoch": ts}
                    for at, ts in self._last_sent.items()
                },
                "cooldown_seconds": self._cooldown_seconds,
            }


# ---------------------------------------------------------------------------
# Destination: local log file
# ---------------------------------------------------------------------------
class LocalLogWriter:
    """Writes alerts to a daily JSON-lines log file under data/alerts/."""

    def __init__(self, directory: Optional[str] = None) -> None:
        self._directory = Path(directory or DEFAULT_ALERTS_LOG_DIR)
        self._directory.mkdir(parents=True, exist_ok=True)
        self._file: Optional[Any] = None
        self._current_date: Optional[str] = None
        self._lock = threading.Lock()

    def write(self, alert: Alert) -> None:
        today = alert.timestamp.strftime("%Y-%m-%d")
        with self._lock:
            if today != self._current_date:
                self._rotate(today)
            if self._file:
                self._file.write(alert.to_json_line() + "\n")
                self._file.flush()

    def _rotate(self, today: str) -> None:
        if self._file:
            self._file.close()
        filepath = self._directory / f"alerts_{today}.log"
        self._file = open(filepath, "a")  # noqa: SIM115
        self._current_date = today

    def close(self) -> None:
        with self._lock:
            if self._file:
                self._file.close()
                self._file = None
            self._current_date = None


# ---------------------------------------------------------------------------
# Destination: Telegram
# ---------------------------------------------------------------------------
def send_telegram_sync(
    bot_token: str,
    chat_id: str,
    message: str,
    timeout: float = 10.0,
) -> Tuple[bool, str]:
    """Send a message via Telegram Bot API (synchronous).

    Returns (success: bool, detail: str).
    """
    if requests is None:
        return False, "requests library not available"

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        resp = requests.post(url, json=payload, timeout=timeout)
        if resp.status_code == 200:
            data = resp.json()
            if data.get("ok"):
                return True, "sent"
            else:
                return False, f"API error: {data.get('description', 'unknown')}"
        else:
            return False, f"HTTP {resp.status_code}: {resp.text[:200]}"
    except requests.exceptions.Timeout:
        return False, "request timed out"
    except requests.exceptions.ConnectionError:
        return False, "connection error"
    except Exception as exc:
        return False, str(exc)


class TelegramSender:
    """Thread-safe Telegram message sender with async dispatch."""

    def __init__(self, bot_token: str, chat_id: str, enabled: bool = True) -> None:
        self._bot_token = bot_token
        self._chat_id = chat_id
        self._enabled = enabled
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return self._enabled

    def send(self, message: str) -> Tuple[bool, str]:
        """Send a Telegram message synchronously (blocking)."""
        if not self._enabled:
            return False, "telegram disabled"
        if not self._bot_token or not self._chat_id:
            return False, "bot_token or chat_id not configured"
        with self._lock:
            return send_telegram_sync(self._bot_token, self._chat_id, message)

    def send_async(self, message: str, callback: Optional[callable] = None) -> threading.Thread:
        """Send a Telegram message in a background thread.

        Args:
            message: The text to send.
            callback: Optional callable(result: Tuple[bool, str]) called on completion.
        Returns:
            The background thread handle.
        """
        def _worker() -> None:
            result = self.send(message)
            logger.info(
                "Telegram send %s: %s",
                "✓" if result[0] else "✗",
                result[1],
            )
            if callback:
                with suppress(Exception):
                    callback(result)

        t = threading.Thread(target=_worker, name="tg-send", daemon=True)
        t.start()
        return t


# ---------------------------------------------------------------------------
# Destination: SMS (Textbelt stub)
# ---------------------------------------------------------------------------
def send_sms_stub(
    phone: str = "+15551234567",
    message: str = "",
    api_key: str = "textbelt-stub-key",
    timeout: float = 5.0,
) -> Tuple[bool, str]:
    """Send SMS via Textbelt.com API (stub — uses a fake key by default).

    In production, replace ``api_key`` with a real Textbelt key.
    Returns (success: bool, detail: str).
    """
    if requests is None:
        return False, "requests library not available"

    url = "https://textbelt.com/text"
    payload = {
        "phone": phone,
        "message": message,
        "key": api_key,
    }
    try:
        resp = requests.post(url, json=payload, timeout=timeout)
        data = resp.json()
        if data.get("success"):
            return True, f"sms sent (quota: {data.get('quotaRemaining', '?')})"
        else:
            return False, f"sms error: {data.get('error', 'unknown')}"
    except requests.exceptions.Timeout:
        return False, "sms request timed out"
    except requests.exceptions.ConnectionError:
        return False, "sms connection error (textbelt may be unavailable)"
    except Exception as exc:
        return False, str(exc)


# ---------------------------------------------------------------------------
# Destination: Stdout
# ---------------------------------------------------------------------------
def print_alert(alert: Alert) -> None:
    """Print an alert to stdout with severity colouring."""
    severity_colors = {
        "info": "\033[36m",      # cyan
        "low": "\033[32m",       # green
        "medium": "\033[33m",    # yellow
        "high": "\033[91m",      # bright red
        "critical": "\033[1;91m",  # bold bright red
    }
    color = severity_colors.get(alert.severity, "\033[0m")
    reset = "\033[0m"
    icon = {
        "info": "ℹ️",
        "low": "✅",
        "medium": "⚠️",
        "high": "🔴",
        "critical": "🚨",
    }.get(alert.severity, "📢")

    print(
        f"{color}{icon} [{alert.severity.upper()}] "
        f"{alert.alert_type}: {alert.message}{reset}",
        flush=True,
    )


# ---------------------------------------------------------------------------
# Alert Manager — core orchestrator
# ---------------------------------------------------------------------------
class AlertManager:
    """Central alert router that receives, deduplicates, and dispatches alerts."""

    def __init__(self, config: Dict[str, Any]) -> None:
        alerts_cfg = config.get("alerts", {})

        # Cooldown
        cooldown = alerts_cfg.get("cooldown_minutes", DEFAULT_COOLDOWN_MINUTES)
        try:
            cooldown = int(cooldown)
        except (ValueError, TypeError):
            cooldown = DEFAULT_COOLDOWN_MINUTES

        self.store = AlertStore(cooldown_minutes=cooldown)

        # Telegram
        tg_cfg = alerts_cfg.get("telegram", {})
        tg_enabled = tg_cfg.get("enabled", True) and bool(tg_cfg.get("bot_token"))
        self.telegram = TelegramSender(
            bot_token=tg_cfg.get("bot_token", ""),
            chat_id=tg_cfg.get("chat_id", ""),
            enabled=tg_enabled,
        )

        # Local log
        self._local_log_enabled = alerts_cfg.get("local_log", True)
        log_dir = config.get("logging", {}).get("alerts_dir")
        self.local_log = LocalLogWriter(directory=log_dir)

        # SMS
        self._sms_enabled = alerts_cfg.get("sms_enabled", False)
        self._sms_phone = alerts_cfg.get("sms_phone", "+15551234567")
        self._sms_key = alerts_cfg.get("sms_key", "textbelt")

        # Dog name for message formatting
        self._dog_name = config.get("dog", {}).get("name", "Dog")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def send_alert(
        self,
        alert_type: str,
        severity: str,
        message: str,
        data: Optional[Dict[str, Any]] = None,
    ) -> Alert:
        """Create an alert, deduplicate, and route to enabled destinations.

        Returns the Alert object (regardless of dedup status). Check
        ``alert.destinations_sent`` to see which dests received it.
        """
        alert = Alert(
            alert_type=alert_type,
            severity=severity,
            message=message,
            data=data,
        )

        is_new = self.store.add(alert)

        if not is_new:
            logger.debug(
                "Alert '%s' suppressed (cooldown active): %s",
                alert_type, message,
            )
            print_alert(alert)  # Always print to stdout regardless of dedup
            return alert

        # -- Route to enabled destinations --

        # Always route to stdout
        print_alert(alert)
        alert.destinations_sent.append("stdout")

        # Local log file
        if self._local_log_enabled:
            try:
                self.local_log.write(alert)
                alert.destinations_sent.append("local_log")
                logger.debug("Alert logged to local file")
            except Exception as exc:
                logger.error("Failed to write alert to local log: %s", exc)

        # Telegram (async)
        if self.telegram.enabled:
            tg_message = self._format_telegram_message(alert)
            self.telegram.send_async(tg_message)
            alert.destinations_sent.append("telegram")

        # SMS stub (sync — textbelt is fast)
        if self._sms_enabled:
            try:
                self._send_sms_alert(alert)
                alert.destinations_sent.append("sms")
            except Exception as exc:
                logger.error("Failed to send SMS: %s", exc)

        return alert

    def send_telegram(self, message: str) -> Tuple[bool, str]:
        """Send an arbitrary message via Telegram (blocking)."""
        return self.telegram.send(message)

    def log_local(self, alert: Alert) -> None:
        """Write a single alert to the local log file."""
        try:
            self.local_log.write(alert)
        except Exception as exc:
            logger.error("Failed to write alert to local log: %s", exc)

    def get_recent(self, count: int = 10) -> List[Dict[str, Any]]:
        """Return the most recent N alerts as dicts."""
        return [a.to_dict() for a in self.store.get_recent(count)]

    def status_dict(self) -> Dict[str, Any]:
        """Return manager status for health endpoint."""
        recent = self.store.get_recent(5)
        return {
            "status": "ok",
            "service": "alert_manager",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "total_alerts": self.store.count(),
            "recent_alerts": [a.to_dict() for a in recent],
            "destinations": {
                "telegram": self.telegram.enabled,
                "local_log": self._local_log_enabled,
                "sms": self._sms_enabled,
                "stdout": True,
            },
            "dedup": self.store.dedup_stats(),
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _format_telegram_message(self, alert: Alert) -> str:
        """Format an alert as a Telegram HTML message."""
        emoji = {
            "info": "ℹ️",
            "low": "✅",
            "medium": "⚠️",
            "high": "🔴",
            "critical": "🚨",
        }.get(alert.severity, "📢")

        lines = [
            f"{emoji} <b>{self._dog_name} — Alert</b>",
            f"<b>Type:</b> {alert.alert_type}",
            f"<b>Severity:</b> {alert.severity.upper()}",
            f"<b>Time:</b> {alert.timestamp.strftime('%Y-%m-%d %H:%M:%S UTC')}",
            "",
            alert.message,
        ]

        if alert.data:
            extra = json.dumps(alert.data, indent=2, default=str)
            lines.append(f"\n<pre>{extra}</pre>")

        return "\n".join(lines)

    def _send_sms_alert(self, alert: Alert) -> None:
        """Send an SMS alert via Textbelt stub."""
        msg = f"[{self._dog_name}] {alert.severity.upper()}: {alert.message[:120]}"
        ok, detail = send_sms_stub(
            phone=self._sms_phone,
            message=msg,
            api_key=self._sms_key,
        )
        if ok:
            logger.info("SMS sent: %s", detail)
        else:
            logger.warning("SMS failed: %s", detail)


# ---------------------------------------------------------------------------
# Global state
# ---------------------------------------------------------------------------
_manager: Optional[AlertManager] = None
_http_server: Optional[HTTPServer] = None


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------
class AlertAPIHandler(BaseHTTPRequestHandler):
    """HTTP handler for the alert manager API."""

    manager_ref: Optional[AlertManager] = None

    def do_GET(self) -> None:
        if self.path == "/alerts/health":
            self._handle_health()
        elif self.path == "/alerts" or self.path.startswith("/alerts?"):
            self._handle_get_alerts()
        else:
            self._json_response({"error": "not found", "path": self.path}, 404)

    def do_POST(self) -> None:
        if self.path == "/alerts":
            content_length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(content_length)
            try:
                body = json.loads(raw.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                self._json_response({"error": "invalid JSON body"}, 400)
                return

            alert = self._handle_post_alert(body)
            if alert:
                self._json_response(alert.to_dict(), 201)
            else:
                self._json_response({"error": "invalid alert payload"}, 400)
        else:
            self._json_response({"error": "not found", "path": self.path}, 404)

    def _handle_health(self) -> None:
        mgr = self.manager_ref
        if mgr is None:
            self._json_response({"status": "error", "service": "alert_manager"}, 503)
            return
        self._json_response(mgr.status_dict())

    def _handle_get_alerts(self) -> None:
        mgr = self.manager_ref
        if mgr is None:
            self._json_response({"alerts": []})
            return

        # Parse optional ?count=N query param
        count = 10
        if "?" in self.path:
            query = self.path.split("?", 1)[1]
            params = dict(p.split("=", 1) for p in query.split("&") if "=" in p)
            with suppress(ValueError, KeyError):
                count = int(params.get("count", "10"))

        alerts = mgr.get_recent(count=count)
        self._json_response({"alerts": alerts, "count": len(alerts)})

    def _handle_post_alert(self, body: dict) -> Optional[Alert]:
        """Create an alert from a POST body."""
        mgr = self.manager_ref
        if mgr is None:
            return None

        alert_type = body.get("alert_type")
        severity = body.get("severity", "info")
        message = body.get("message", "")
        data = body.get("data")

        if not alert_type or not isinstance(alert_type, str):
            return None
        if severity not in VALID_SEVERITIES:
            return None
        if not message:
            return None

        return mgr.send_alert(
            alert_type=alert_type,
            severity=severity,
            message=message,
            data=data,
        )

    def _json_response(self, data: dict, status: int = 200) -> None:
        body = json.dumps(data, indent=2, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: Any) -> None:
        logger.debug("HTTP: " + fmt % args)


# ---------------------------------------------------------------------------
# Test mode
# ---------------------------------------------------------------------------
def run_test_mode(manager: AlertManager) -> None:
    """Send a series of sample alerts to verify all routing works."""
    print("\n" + "=" * 60)
    print("  🧪 Alert Manager — Test Mode")
    print("=" * 60)

    test_cases = [
        ("heart_rate", "info", "Heart rate normal: 72 bpm", {"bpm": 72}),
        ("heart_rate", "low", "Heart rate low: 38 bpm", {"bpm": 38}),
        ("heart_rate", "high", "Heart rate elevated: 165 bpm", {"bpm": 165}),
        ("heart_rate", "critical", "Heart rate critically high: 195 bpm", {"bpm": 195}),
        ("temperature", "info", "Temperature normal: 38.5 °C", {"temp_c": 38.5}),
        ("temperature", "medium", "Temperature elevated: 39.8 °C", {"temp_c": 39.8}),
        ("temperature", "high", "Fever detected: 40.2 °C", {"temp_c": 40.2}),
        ("geofence_escape", "high", "Fido is 250m from home zone!", {"distance_m": 250}),
        ("geofence_enter", "info", "Fido has entered safe zone 'home'", {"zone": "home"}),
        ("battery", "medium", "Battery level: 15%", {"percent": 15}),
        ("battery", "high", "Battery level: 5% — critical!", {"percent": 5}),
        ("inactivity", "medium", "Fido inactive for 180 minutes", {"minutes": 180}),
        ("routine_anomaly", "low", "Walk 15 minutes late vs schedule", {"delta_min": 15}),
        ("sensor_failure", "high", "Heart rate sensor not responding", {"sensor": "hr"}),
        ("sensor_failure", "critical", "All sensors offline for 5 minutes", {"offline_sec": 300}),
    ]

    for i, (atype, sev, msg, data) in enumerate(test_cases):
        print(f"\n  [{i + 1}/{len(test_cases)}] {atype} ({sev}): {msg}")
        alert = manager.send_alert(
            alert_type=atype,
            severity=sev,
            message=msg,
            data=data,
        )
        print(f"    → Destinations: {', '.join(alert.destinations_sent)}")
        time.sleep(0.3)

    print("\n" + "=" * 60)
    print(f"  ✅ Sent {len(test_cases)} test alerts")
    print(f"  📊 Total in store: {manager.store.count()}")
    print(f"  💡 Use: curl http://127.0.0.1:{DEFAULT_PORT}/alerts?count=5")
    print("=" * 60 + "\n")


# ---------------------------------------------------------------------------
# Server startup
# ---------------------------------------------------------------------------
def start_http_server(manager: AlertManager, port: int) -> HTTPServer:
    """Start the alert API HTTP server in a background daemon thread."""
    global _http_server

    AlertAPIHandler.manager_ref = manager
    server = HTTPServer(("127.0.0.1", port), AlertAPIHandler)
    _http_server = server

    thread = threading.Thread(
        target=server.serve_forever,
        name="alert-api-http",
        daemon=True,
    )
    thread.start()
    logger.info("🌐 Alert API: http://127.0.0.1:%d/alerts", port)
    return server


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------
def load_config(path: Optional[str] = None) -> Dict[str, Any]:
    """Load YAML config from the given path or default location."""
    config_path = Path(path) if path else DEFAULT_CONFIG_PATH
    if not config_path.exists():
        logger.warning("Config file not found: %s", config_path)
        return {}

    try:
        with open(config_path) as f:
            cfg = yaml.safe_load(f) or {}
        logger.info("Loaded config from %s", config_path)
        return cfg
    except Exception as exc:
        logger.error("Failed to load config: %s", exc)
        return {}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Dog Agent Alert Manager — routes alerts to configured destinations",
    )
    parser.add_argument(
        "--config", type=str, default=None,
        help="Path to config.yaml (default: %s)" % DEFAULT_CONFIG_PATH,
    )
    parser.add_argument(
        "--port", type=int, default=DEFAULT_PORT,
        help="HTTP API port (default: %d)" % DEFAULT_PORT,
    )
    parser.add_argument(
        "--test", action="store_true",
        help="Run test mode: send sample alerts to verify routing",
    )
    return parser.parse_args()


# ---------------------------------------------------------------------------
# Shutdown
# ---------------------------------------------------------------------------
_shutdown_in_progress = False


def graceful_shutdown(signum: Optional[int] = None, frame: Any = None) -> None:
    global _shutdown_in_progress
    if _shutdown_in_progress:
        return
    _shutdown_in_progress = True

    if signum is not None:
        try:
            sig_name = signal.Signals(signum).name
        except ValueError:
            sig_name = f"signal {signum}"
    else:
        sig_name = "atexit"

    logger.info("[SHUTDOWN] Received %s — shutting down alert manager...", sig_name)

    if _http_server:
        _http_server.shutdown()

    if _manager:
        _manager.local_log.close()

    logger.info("[DONE] Alert manager stopped.")
    sys.exit(0)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------
def main() -> None:
    global _manager

    args = parse_args()

    # Load config
    config = load_config(args.config)

    # Create the alert manager
    _manager = AlertManager(config)

    # Start HTTP API
    start_http_server(_manager, args.port)

    # Register signal handlers
    signal.signal(signal.SIGTERM, graceful_shutdown)
    signal.signal(signal.SIGINT, graceful_shutdown)

    # Test mode
    if args.test:
        run_test_mode(_manager)
        print("Test mode complete. HTTP API continues running.")
        print("Press Ctrl+C to stop.\n")

    logger.info(
        "[READY] Alert Manager running on 127.0.0.1:%d — "
        "destinations: telegram=%s, local_log=%s, sms=%s, stdout=yes",
        args.port,
        _manager.telegram.enabled,
        _manager._local_log_enabled,
        _manager._sms_enabled,
    )

    # Keep main thread alive
    try:
        while not _shutdown_in_progress:
            time.sleep(1)
    except KeyboardInterrupt:
        graceful_shutdown(signal.SIGINT)


if __name__ == "__main__":
    main()