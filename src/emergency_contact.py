#!/usr/bin/env python3
"""
Emergency Contact Escalation Module — Dog Agent
===============================================
Automatic timeout-based escalation for critical alerts with three levels:
  - Level 1: Alert owner (primary contact)
  - Level 2: Alert secondary contact (15 min timeout if no acknowledge)
  - Level 3: Alert emergency contact (15 min timeout if no response)

Triggers escalation for:
  - Geofence escape
  - Severe fall detected
  - Health emergency (critical vitals)
  - Panic button hold

HTTP API on port 9136:
  GET /emergency/status      — active escalations, pending alerts
  POST /emergency/acknowledge — owner acknowledges alert
  GET /emergency/contacts    — list configured contacts
  POST /emergency/test       — test escalation flow

Usage:
    python src/emergency_contact.py                    # Normal mode
    python src/emergency_contact.py --simulate         # Test mode (no real alerts)
    python src/emergency_contact.py --config /path/to/config.yaml
    python src/emergency_contact.py --port 9136
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
from contextlib import suppress
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta
from enum import Enum, auto
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Dict, List, Optional, Callable, Set, Tuple

import yaml

try:
    import requests
except ImportError:
    requests = None  # type: ignore


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logger = logging.getLogger("emergency_contact")
_handler = logging.StreamHandler(sys.stdout)
_handler.setFormatter(logging.Formatter(
    "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
))
logger.addHandler(_handler)
logger.setLevel(logging.INFO)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
PROJECT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_DIR / "config.yaml"
DATA_DIR = PROJECT_DIR / "data"
EMERGENCY_LOG_DIR = DATA_DIR / "emergency"
DEFAULT_PORT = 9136

# Alert types that trigger escalation
TRIGGER_ALERTS = {
    "geofence_escape",
    "fall_severe",
    "health_critical",
    "panic_button_hold",
}

# GPS URL for location lookup
GPS_API_URL = "http://127.0.0.1:9110/gps"
ALERT_MANAGER_URL = "http://127.0.0.1:9118/alerts"


# ---------------------------------------------------------------------------
# Data Classes
# ---------------------------------------------------------------------------
class EscalationLevel(Enum):
    """Escalation hierarchy levels."""
    LEVEL_1_PRIMARY = 1
    LEVEL_2_SECONDARY = 2
    LEVEL_3_EMERGENCY = 3
    RESOLVED = 4


class EscalationState(Enum):
    """Current state of an escalation."""
    PENDING = "pending"           # Waiting for timeout/acknowledgment
    ACKNOWLEDGED = "acknowledged" # Owner acknowledged
    ESCALATED = "escalated"       # Escalated to next level
    RESOLVED = "resolved"         # Emergency resolved
    EXPIRED = "expired"           # Max escalation reached


@dataclass
class Contact:
    """Emergency contact configuration."""
    name: str
    phone: str
    telegram_id: Optional[str] = None
    email: Optional[str] = None
    relationship: Optional[str] = None  # e.g., "owner", "vet", "family"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "phone": self.phone,
            "telegram_id": self.telegram_id,
            "email": self.email,
            "relationship": self.relationship,
        }


@dataclass
class EmergencyAlert:
    """An emergency alert that triggers escalation."""
    alert_id: str
    alert_type: str
    severity: str
    message: str
    triggered_at: datetime
    location: Optional[Dict[str, Any]] = None
    data: Dict[str, Any] = field(default_factory=dict)
    
    # Escalation tracking
    current_level: EscalationLevel = EscalationLevel.LEVEL_1_PRIMARY
    state: EscalationState = EscalationState.PENDING
    acknowledged_at: Optional[datetime] = None
    acknowledged_by: Optional[str] = None
    resolved_at: Optional[datetime] = None
    
    # Contact notifications sent
    notifications_sent: List[Dict[str, Any]] = field(default_factory=list)
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "alert_id": self.alert_id,
            "alert_type": self.alert_type,
            "severity": self.severity,
            "message": self.message,
            "triggered_at": self.triggered_at.isoformat(),
            "location": self.location,
            "data": self.data,
            "current_level": self.current_level.value,
            "state": self.state.value,
            "acknowledged_at": self.acknowledged_at.isoformat() if self.acknowledged_at else None,
            "acknowledged_by": self.acknowledged_by,
            "resolved_at": self.resolved_at.isoformat() if self.resolved_at else None,
            "notifications_sent": self.notifications_sent,
        }


@dataclass
class EscalationConfig:
    """Configuration for escalation behavior."""
    primary: Optional[Contact] = None
    secondary: Optional[Contact] = None
    emergency: Optional[Contact] = None
    escalation_timeout_min: int = 15
    max_escalation_levels: int = 3
    gps_link_template: str = "https://maps.google.com/?q={lat},{lon}"
    
    @classmethod
    def from_dict(cls, config: Dict[str, Any]) -> "EscalationConfig":
        """Load configuration from config dict."""
        ec_cfg = config.get("emergency_contact", {})
        contacts_cfg = config.get("emergency_contacts", {})
        
        def load_contact(key: str) -> Optional[Contact]:
            if key not in contacts_cfg:
                return None
            c = contacts_cfg[key]
            return Contact(
                name=c.get("name", "Unknown"),
                phone=c.get("phone", ""),
                telegram_id=c.get("telegram_id"),
                email=c.get("email"),
                relationship=c.get("relationship"),
            )
        
        return cls(
            primary=load_contact("primary"),
            secondary=load_contact("secondary"),
            emergency=load_contact("emergency"),
            escalation_timeout_min=ec_cfg.get("escalation_timeout_min", 15),
            max_escalation_levels=ec_cfg.get("max_escalation_levels", 3),
            gps_link_template=ec_cfg.get(
                "gps_link_template",
                "https://maps.google.com/?q={lat},{lon}"
            ),
        )


# ---------------------------------------------------------------------------
# GPS Utilities
# ---------------------------------------------------------------------------
def get_current_location() -> Optional[Dict[str, Any]]:
    """Fetch current GPS location from the GPS daemon."""
    if requests is None:
        return None
    
    try:
        resp = requests.get(GPS_API_URL, timeout=5)
        if resp.status_code == 200:
            data = resp.json()
            if data.get("valid"):
                return {
                    "lat": data.get("lat"),
                    "lon": data.get("lon"),
                    "altitude": data.get("altitude"),
                    "speed_mps": data.get("speed_mps"),
                    "timestamp": data.get("timestamp"),
                }
    except Exception as exc:
        logger.debug("Failed to fetch GPS location: %s", exc)
    return None


def generate_gps_link(lat: float, lon: float, template: str) -> str:
    """Generate a GPS map link from coordinates."""
    return template.format(lat=lat, lon=lon)


# ---------------------------------------------------------------------------
# Notification Services
# ---------------------------------------------------------------------------
class NotificationService:
    """Base class for notification services."""
    
    def __init__(self, simulate: bool = False):
        self.simulate = simulate
    
    def send(self, contact: Contact, message: str, alert: EmergencyAlert) -> Tuple[bool, str]:
        """Send notification to contact. Returns (success, detail)."""
        raise NotImplementedError


class TelegramNotifier(NotificationService):
    """Send notifications via Telegram Bot API."""
    
    def __init__(self, bot_token: Optional[str] = None, simulate: bool = False):
        super().__init__(simulate)
        self.bot_token = bot_token
    
    def send(self, contact: Contact, message: str, alert: EmergencyAlert) -> Tuple[bool, str]:
        if self.simulate:
            logger.info("[SIMULATE] Telegram to %s: %s", contact.name, message[:50])
            return True, "simulated"
        
        if not self.bot_token or not contact.telegram_id:
            return False, "telegram not configured"
        
        if requests is None:
            return False, "requests library not available"
        
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        payload = {
            "chat_id": contact.telegram_id,
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
        }
        
        try:
            resp = requests.post(url, json=payload, timeout=10)
            data = resp.json()
            if data.get("ok"):
                return True, "sent"
            return False, f"API error: {data.get('description', 'unknown')}"
        except Exception as exc:
            return False, str(exc)


class SMSNotifier(NotificationService):
    """Send notifications via SMS (Textbelt stub)."""
    
    def __init__(self, api_key: str = "textbelt", simulate: bool = False):
        super().__init__(simulate)
        self.api_key = api_key
    
    def send(self, contact: Contact, message: str, alert: EmergencyAlert) -> Tuple[bool, str]:
        if self.simulate:
            logger.info("[SIMULATE] SMS to %s (%s): %s", contact.name, contact.phone, message[:50])
            return True, "simulated"
        
        if not contact.phone:
            return False, "phone number not configured"
        
        if requests is None:
            return False, "requests library not available"
        
        url = "https://textbelt.com/text"
        payload = {
            "phone": contact.phone,
            "message": message[:160],  # SMS limit
            "key": self.api_key,
        }
        
        try:
            resp = requests.post(url, json=payload, timeout=10)
            data = resp.json()
            if data.get("success"):
                return True, f"sent (quota: {data.get('quotaRemaining', '?')})"
            return False, f"error: {data.get('error', 'unknown')}"
        except Exception as exc:
            return False, str(exc)


class AlertManagerNotifier(NotificationService):
    """Send notifications via the alert_manager HTTP API."""
    
    def __init__(self, simulate: bool = False):
        super().__init__(simulate)
    
    def send(self, contact: Contact, message: str, alert: EmergencyAlert) -> Tuple[bool, str]:
        if self.simulate:
            logger.info("[SIMULATE] AlertManager notification: %s", message[:50])
            return True, "simulated"
        
        if requests is None:
            return False, "requests library not available"
        
        try:
            resp = requests.post(
                ALERT_MANAGER_URL,
                json={
                    "alert_type": f"emergency_escalation_{alert.current_level.value}",
                    "severity": "critical",
                    "message": message,
                    "data": alert.to_dict(),
                },
                timeout=5,
            )
            if resp.status_code in (200, 201):
                return True, "sent via alert_manager"
            return False, f"HTTP {resp.status_code}"
        except Exception as exc:
            return False, str(exc)


# ---------------------------------------------------------------------------
# Escalation State Machine
# ---------------------------------------------------------------------------
class EscalationManager:
    """Manages emergency escalation state machine with timeout handling."""
    
    def __init__(
        self,
        config: EscalationConfig,
        simulate: bool = False,
        telegram_token: Optional[str] = None,
    ):
        self.config = config
        self.simulate = simulate
        self._lock = threading.RLock()
        self._active_alerts: Dict[str, EmergencyAlert] = {}
        self._escalation_timers: Dict[str, threading.Timer] = {}
        self._dog_name = "Dog"
        
        # Notification services
        self._notifiers: List[NotificationService] = [
            TelegramNotifier(telegram_token, simulate),
            SMSNotifier(simulate=simulate),
            AlertManagerNotifier(simulate),
        ]
        
        # Callbacks for external integration
        self._on_escalation: List[Callable[[EmergencyAlert], None]] = []
        self._on_acknowledge: List[Callable[[EmergencyAlert], None]] = []
        self._on_resolve: List[Callable[[EmergencyAlert], None]] = []
        
        # Ensure log directory exists
        EMERGENCY_LOG_DIR.mkdir(parents=True, exist_ok=True)
    
    def set_dog_name(self, name: str) -> None:
        """Set the dog's name for messages."""
        self._dog_name = name
    
    def register_on_escalation(self, callback: Callable[[EmergencyAlert], None]) -> None:
        """Register callback for escalation events."""
        self._on_escalation.append(callback)
    
    def register_on_acknowledge(self, callback: Callable[[EmergencyAlert], None]) -> None:
        """Register callback for acknowledgment events."""
        self._on_acknowledge.append(callback)
    
    def register_on_resolve(self, callback: Callable[[EmergencyAlert], None]) -> None:
        """Register callback for resolution events."""
        self._on_resolve.append(callback)
    
    def _generate_alert_id(self, alert_type: str) -> str:
        """Generate unique alert ID."""
        ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        return f"{alert_type}_{ts}_{threading.current_thread().ident}"
    
    def _format_message(self, alert: EmergencyAlert, contact: Contact) -> str:
        """Format notification message for a contact."""
        level_names = {
            EscalationLevel.LEVEL_1_PRIMARY: "🚨 EMERGENCY ALERT",
            EscalationLevel.LEVEL_2_SECONDARY: "⚠️ ESCALATED ALERT",
            EscalationLevel.LEVEL_3_EMERGENCY: "🔴 CRITICAL EMERGENCY",
        }
        
        lines = [
            f"<b>{level_names.get(alert.current_level, 'EMERGENCY')}</b>",
            f"",
            f"<b>{self._dog_name}</b> - {alert.message}",
            f"",
            f"<b>Type:</b> {alert.alert_type}",
            f"<b>Time:</b> {alert.triggered_at.strftime('%Y-%m-%d %H:%M:%S UTC')}",
            f"<b>Level:</b> {alert.current_level.value}",
        ]
        
        # Add GPS link if location available
        if alert.location and alert.location.get("lat") and alert.location.get("lon"):
            lat = alert.location["lat"]
            lon = alert.location["lon"]
            gps_link = generate_gps_link(lat, lon, self.config.gps_link_template)
            lines.extend([
                f"",
                f"<b>📍 Location:</b> {lat:.5f}, {lon:.5f}",
                f"<a href='{gps_link}'>View on Map</a>",
            ])
        
        # Add acknowledgment instructions for primary contact
        if alert.current_level == EscalationLevel.LEVEL_1_PRIMARY:
            lines.extend([
                f"",
                f"<i>Reply 'ACK {alert.alert_id}' to acknowledge this alert.</i>",
            ])
        
        return "\n".join(lines)
    
    def _notify_contact(self, alert: EmergencyAlert, contact: Contact) -> None:
        """Send notification to a specific contact via all available channels."""
        message = self._format_message(alert, contact)
        
        for notifier in self._notifiers:
            try:
                success, detail = notifier.send(contact, message, alert)
                alert.notifications_sent.append({
                    "contact": contact.name,
                    "channel": notifier.__class__.__name__,
                    "success": success,
                    "detail": detail,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                })
                
                if success:
                    logger.info(
                        "Notification sent to %s via %s: %s",
                        contact.name, notifier.__class__.__name__, detail
                    )
                else:
                    logger.warning(
                        "Failed to notify %s via %s: %s",
                        contact.name, notifier.__class__.__name__, detail
                    )
            except Exception as exc:
                logger.error("Notification error: %s", exc)
        
        self._log_alert(alert)
    
    def _log_alert(self, alert: EmergencyAlert) -> None:
        """Log alert to file for persistence."""
        try:
            date_str = alert.triggered_at.strftime("%Y-%m-%d")
            log_file = EMERGENCY_LOG_DIR / f"emergency_{date_str}.jsonl"
            
            with open(log_file, "a") as f:
                f.write(json.dumps(alert.to_dict(), default=str) + "\n")
        except Exception as exc:
            logger.error("Failed to log alert: %s", exc)
    
    def _get_contact_for_level(self, level: EscalationLevel) -> Optional[Contact]:
        """Get the contact for a specific escalation level."""
        if level == EscalationLevel.LEVEL_1_PRIMARY:
            return self.config.primary
        elif level == EscalationLevel.LEVEL_2_SECONDARY:
            return self.config.secondary
        elif level == EscalationLevel.LEVEL_3_EMERGENCY:
            return self.config.emergency
        return None
    
    def _escalate(self, alert_id: str) -> None:
        """Escalate an alert to the next level."""
        with self._lock:
            alert = self._active_alerts.get(alert_id)
            if not alert:
                return
            
            # Check if already resolved or acknowledged
            if alert.state in (EscalationState.RESOLVED, EscalationState.ACKNOWLEDGED):
                logger.info("Alert %s already resolved/acknowledged, skipping escalation", alert_id)
                return
            
            # Determine next level
            next_level = EscalationLevel(alert.current_level.value + 1)
            
            # Check max escalation
            if next_level.value > self.config.max_escalation_levels:
                logger.warning("Alert %s reached max escalation level", alert_id)
                alert.state = EscalationState.EXPIRED
                return
            
            # Update alert
            alert.current_level = next_level
            alert.state = EscalationState.ESCALATED
            
            # Get contact for this level
            contact = self._get_contact_for_level(next_level)
            if contact:
                logger.warning(
                    "Escalating alert %s to level %d (%s)",
                    alert_id, next_level.value, contact.name
                )
                self._notify_contact(alert, contact)
                
                # Schedule next escalation
                if next_level.value < self.config.max_escalation_levels:
                    self._schedule_escalation(alert_id)
            else:
                logger.warning(
                    "No contact configured for level %d, cannot escalate",
                    next_level.value
                )
                alert.state = EscalationState.EXPIRED
            
            # Trigger callbacks
            for cb in self._on_escalation:
                try:
                    cb(alert)
                except Exception as exc:
                    logger.exception("Escalation callback error: %s", exc)
            
            self._log_alert(alert)
    
    def _schedule_escalation(self, alert_id: str) -> None:
        """Schedule escalation timer for an alert."""
        # Cancel any existing timer
        if alert_id in self._escalation_timers:
            self._escalation_timers[alert_id].cancel()
        
        timeout_sec = self.config.escalation_timeout_min * 60
        
        def escalate_timer():
            self._escalate(alert_id)
        
        timer = threading.Timer(timeout_sec, escalate_timer)
        timer.daemon = True
        timer.start()
        
        with self._lock:
            self._escalation_timers[alert_id] = timer
        
        logger.info(
            "Scheduled escalation for alert %s in %d minutes",
            alert_id, self.config.escalation_timeout_min
        )
    
    def trigger_emergency(
        self,
        alert_type: str,
        message: str,
        severity: str = "critical",
        data: Optional[Dict[str, Any]] = None,
    ) -> EmergencyAlert:
        """Trigger a new emergency alert and start escalation process."""
        # Validate alert type
        if alert_type not in TRIGGER_ALERTS:
            logger.warning(
                "Alert type '%s' not in trigger list %s, still processing",
                alert_type, TRIGGER_ALERTS
            )
        
        # Get current location
        location = get_current_location()
        
        # Create alert
        alert = EmergencyAlert(
            alert_id=self._generate_alert_id(alert_type),
            alert_type=alert_type,
            severity=severity,
            message=message,
            triggered_at=datetime.now(timezone.utc),
            location=location,
            data=data or {},
            current_level=EscalationLevel.LEVEL_1_PRIMARY,
            state=EscalationState.PENDING,
        )
        
        with self._lock:
            self._active_alerts[alert.alert_id] = alert
        
        # Notify primary contact immediately
        primary = self.config.primary
        if primary:
            logger.critical(
                "🚨 EMERGENCY: %s - Notifying primary contact %s",
                message, primary.name
            )
            self._notify_contact(alert, primary)
        else:
            logger.error("No primary contact configured for emergency!")
        
        # Schedule escalation if not acknowledged
        self._schedule_escalation(alert.alert_id)
        
        return alert
    
    def acknowledge(self, alert_id: str, acknowledged_by: str) -> Optional[EmergencyAlert]:
        """Acknowledge an alert, stopping escalation."""
        with self._lock:
            alert = self._active_alerts.get(alert_id)
            if not alert:
                return None
            
            # Cancel escalation timer
            if alert_id in self._escalation_timers:
                self._escalation_timers[alert_id].cancel()
                del self._escalation_timers[alert_id]
            
            # Update alert state
            alert.state = EscalationState.ACKNOWLEDGED
            alert.acknowledged_at = datetime.now(timezone.utc)
            alert.acknowledged_by = acknowledged_by
            
            logger.info(
                "Alert %s acknowledged by %s - escalation stopped",
                alert_id, acknowledged_by
            )
            
            # Trigger callbacks
            for cb in self._on_acknowledge:
                try:
                    cb(alert)
                except Exception as exc:
                    logger.exception("Acknowledge callback error: %s", exc)
            
            self._log_alert(alert)
            return alert
    
    def resolve(self, alert_id: str) -> Optional[EmergencyAlert]:
        """Mark an alert as resolved."""
        with self._lock:
            alert = self._active_alerts.get(alert_id)
            if not alert:
                return None
            
            # Cancel escalation timer
            if alert_id in self._escalation_timers:
                self._escalation_timers[alert_id].cancel()
                del self._escalation_timers[alert_id]
            
            # Update state
            alert.state = EscalationState.RESOLVED
            alert.resolved_at = datetime.now(timezone.utc)
            
            logger.info("Alert %s resolved", alert_id)
            
            # Trigger callbacks
            for cb in self._on_resolve:
                try:
                    cb(alert)
                except Exception as exc:
                    logger.exception("Resolve callback error: %s", exc)
            
            self._log_alert(alert)
            
            # Remove from active alerts (keep in history)
            del self._active_alerts[alert_id]
            
            return alert
    
    def get_active_alerts(self) -> List[EmergencyAlert]:
        """Get all active (non-resolved) alerts."""
        with self._lock:
            return list(self._active_alerts.values())
    
    def get_alert(self, alert_id: str) -> Optional[EmergencyAlert]:
        """Get a specific alert by ID."""
        with self._lock:
            return self._active_alerts.get(alert_id)
    
    def get_status(self) -> Dict[str, Any]:
        """Get current escalation status."""
        with self._lock:
            active = self.get_active_alerts()
            return {
                "active_alerts_count": len(active),
                "active_alerts": [a.to_dict() for a in active],
                "pending_escalations": len(self._escalation_timers),
                "config": {
                    "timeout_min": self.config.escalation_timeout_min,
                    "max_levels": self.config.max_escalation_levels,
                },
                "contacts_configured": {
                    "primary": self.config.primary is not None,
                    "secondary": self.config.secondary is not None,
                    "emergency": self.config.emergency is not None,
                },
            }
    
    def shutdown(self) -> None:
        """Cancel all pending timers on shutdown."""
        with self._lock:
            for timer in self._escalation_timers.values():
                timer.cancel()
            self._escalation_timers.clear()


# ---------------------------------------------------------------------------
# HTTP API Handler
# ---------------------------------------------------------------------------
class EmergencyAPIHandler(BaseHTTPRequestHandler):
    """HTTP API for emergency contact escalation."""
    
    manager: Optional[EscalationManager] = None
    
    def log_message(self, fmt: str, *args: Any) -> None:
        logger.debug("HTTP: " + fmt % args)
    
    def _send_json(self, data: Any, status: int = 200) -> None:
        body = json.dumps(data, indent=2, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)
    
    def do_GET(self) -> None:
        parsed = self._parse_path()
        path = parsed["path"]
        query = parsed["query"]
        
        if path == "/emergency/status":
            self._handle_status()
        elif path == "/emergency/contacts":
            self._handle_contacts()
        elif path == "/emergency/health":
            self._handle_health()
        else:
            self._send_json({"error": "not found", "path": path}, 404)
    
    def do_POST(self) -> None:
        parsed = self._parse_path()
        path = parsed["path"]
        
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length).decode("utf-8") if content_length > 0 else "{}"
        
        try:
            data = json.loads(body) if body else {}
        except json.JSONDecodeError:
            self._send_json({"error": "invalid JSON"}, 400)
            return
        
        if path == "/emergency/acknowledge":
            self._handle_acknowledge(data)
        elif path == "/emergency/test":
            self._handle_test(data)
        elif path == "/emergency/trigger":
            self._handle_trigger(data)
        elif path == "/emergency/resolve":
            self._handle_resolve(data)
        else:
            self._send_json({"error": "not found", "path": path}, 404)
    
    def _parse_path(self) -> Dict[str, Any]:
        """Parse path and query parameters."""
        raw = self.path.split("?", 1)
        path = raw[0].rstrip("/")
        query: Dict[str, List[str]] = {}
        if len(raw) > 1 and raw[1]:
            for part in raw[1].split("&"):
                if "=" in part:
                    k, v = part.split("=", 1)
                    query.setdefault(k, []).append(v)
        return {"path": path, "query": query}
    
    def _handle_status(self) -> None:
        """Return current escalation status."""
        if self.manager is None:
            self._send_json({"error": "manager not initialized"}, 503)
            return
        
        self._send_json(self.manager.get_status())
    
    def _handle_contacts(self) -> None:
        """Return configured contacts (sanitized - no tokens)."""
        if self.manager is None:
            self._send_json({"error": "manager not initialized"}, 503)
            return
        
        cfg = self.manager.config
        contacts = {}
        for key, contact in [
            ("primary", cfg.primary),
            ("secondary", cfg.secondary),
            ("emergency", cfg.emergency),
        ]:
            if contact:
                contacts[key] = {
                    "name": contact.name,
                    "phone": contact.phone[:6] + "****" if contact.phone else None,
                    "telegram_id": "configured" if contact.telegram_id else None,
                    "relationship": contact.relationship,
                }
        
        self._send_json({
            "contacts": contacts,
            "escalation_timeout_min": cfg.escalation_timeout_min,
            "max_escalation_levels": cfg.max_escalation_levels,
        })
    
    def _handle_acknowledge(self, data: Dict[str, Any]) -> None:
        """Acknowledge an alert."""
        if self.manager is None:
            self._send_json({"error": "manager not initialized"}, 503)
            return
        
        alert_id = data.get("alert_id")
        acknowledged_by = data.get("acknowledged_by", "api_user")
        
        if not alert_id:
            self._send_json({"error": "missing alert_id"}, 400)
            return
        
        alert = self.manager.acknowledge(alert_id, acknowledged_by)
        if alert:
            self._send_json({
                "status": "acknowledged",
                "alert": alert.to_dict(),
            })
        else:
            self._send_json({"error": "alert not found"}, 404)
    
    def _handle_resolve(self, data: Dict[str, Any]) -> None:
        """Resolve an alert."""
        if self.manager is None:
            self._send_json({"error": "manager not initialized"}, 503)
            return
        
        alert_id = data.get("alert_id")
        if not alert_id:
            self._send_json({"error": "missing alert_id"}, 400)
            return
        
        alert = self.manager.resolve(alert_id)
        if alert:
            self._send_json({
                "status": "resolved",
                "alert": alert.to_dict(),
            })
        else:
            self._send_json({"error": "alert not found"}, 404)
    
    def _handle_trigger(self, data: Dict[str, Any]) -> None:
        """Manually trigger an emergency (for testing or external integration)."""
        if self.manager is None:
            self._send_json({"error": "manager not initialized"}, 503)
            return
        
        alert_type = data.get("alert_type", "manual_trigger")
        message = data.get("message", "Manual emergency trigger")
        severity = data.get("severity", "critical")
        
        alert = self.manager.trigger_emergency(
            alert_type=alert_type,
            message=message,
            severity=severity,
            data=data.get("data"),
        )
        
        self._send_json({
            "status": "triggered",
            "alert": alert.to_dict(),
        }, 201)
    
    def _handle_test(self, data: Dict[str, Any]) -> None:
        """Test escalation flow with a simulated alert."""
        if self.manager is None:
            self._send_json({"error": "manager not initialized"}, 503)
            return
        
        test_type = data.get("test_type", "full_escalation")
        
        if test_type == "notification_only":
            # Just test notifications, don't start escalation timer
            self._send_json({
                "status": "test_notifications_sent",
                "test_type": test_type,
            })
        elif test_type == "single_level":
            # Test only level 1
            alert = self.manager.trigger_emergency(
                alert_type="test_alert",
                message="TEST: Single level escalation test",
                severity="low",
                data={"test": True, "type": "single_level"},
            )
            # Immediately acknowledge to prevent escalation
            self.manager.acknowledge(alert.alert_id, "test_system")
            self._send_json({
                "status": "test_single_level_complete",
                "alert_id": alert.alert_id,
            })
        else:
            # Full escalation test (will escalate through all levels)
            alert = self.manager.trigger_emergency(
                alert_type="test_alert",
                message="TEST: Full escalation flow test",
                severity="critical",
                data={"test": True, "type": "full_escalation"},
            )
            
            self._send_json({
                "status": "test_escalation_started",
                "alert_id": alert.alert_id,
                "message": (
                    f"Test alert triggered. Will escalate every "
                    f"{self.manager.config.escalation_timeout_min} minutes "
                    f"until acknowledged or max level reached."
                ),
            })
    
    def _handle_health(self) -> None:
        """Health check endpoint."""
        self._send_json({
            "status": "ok",
            "service": "emergency_contact",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })


# ---------------------------------------------------------------------------
# Webhook Listener (for external integrations)
# ---------------------------------------------------------------------------
class WebhookListener:
    """Listen for alerts from other modules via HTTP webhooks."""
    
    def __init__(self, manager: EscalationManager):
        self.manager = manager
    
    def handle_alert_manager_webhook(self, alert_data: Dict[str, Any]) -> None:
        """Process alert from alert_manager."""
        alert_type = alert_data.get("alert_type", "")
        
        # Map alert_manager types to emergency types
        if alert_type == "geofence_escape":
            self._trigger_geofence_escape(alert_data)
        elif alert_type == "fall_severe":
            self._trigger_severe_fall(alert_data)
        elif alert_type == "health_critical":
            self._trigger_health_emergency(alert_data)
        elif alert_type == "panic_button_hold":
            self._trigger_panic_emergency(alert_data)
    
    def _trigger_geofence_escape(self, data: Dict[str, Any]) -> None:
        """Trigger geofence escape emergency."""
        message = data.get("message", "Dog has escaped the safe zone!")
        self.manager.trigger_emergency(
            alert_type="geofence_escape",
            message=message,
            severity="high",
            data=data,
        )
    
    def _trigger_severe_fall(self, data: Dict[str, Any]) -> None:
        """Trigger severe fall emergency."""
        message = data.get("message", "Severe fall detected!")
        self.manager.trigger_emergency(
            alert_type="fall_severe",
            message=message,
            severity="critical",
            data=data,
        )
    
    def _trigger_health_emergency(self, data: Dict[str, Any]) -> None:
        """Trigger health emergency."""
        message = data.get("message", "Critical health alert!")
        self.manager.trigger_emergency(
            alert_type="health_critical",
            message=message,
            severity="critical",
            data=data,
        )
    
    def _trigger_panic_emergency(self, data: Dict[str, Any]) -> None:
        """Trigger panic button emergency."""
        message = data.get("message", "Panic button activated!")
        self.manager.trigger_emergency(
            alert_type="panic_button_hold",
            message=message,
            severity="critical",
            data=data,
        )


# ---------------------------------------------------------------------------
# Config Loading
# ---------------------------------------------------------------------------
def load_config(path: Optional[str] = None) -> Tuple[EscalationConfig, Dict[str, Any]]:
    """Load configuration from YAML file."""
    config_path = Path(path) if path else DEFAULT_CONFIG_PATH
    
    if not config_path.exists():
        logger.warning("Config file not found: %s, using defaults", config_path)
        return EscalationConfig(), {}
    
    try:
        with open(config_path) as f:
            config = yaml.safe_load(f) or {}
        
        escalation_config = EscalationConfig.from_dict(config)
        logger.info("Loaded config from %s", config_path)
        return escalation_config, config
    except Exception as exc:
        logger.error("Failed to load config: %s", exc)
        return EscalationConfig(), {}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Emergency Contact Escalation Module for Dog Agent",
    )
    parser.add_argument(
        "--config", type=str, default=None,
        help=f"Path to config.yaml (default: {DEFAULT_CONFIG_PATH})",
    )
    parser.add_argument(
        "--port", type=int, default=DEFAULT_PORT,
        help=f"HTTP API port (default: {DEFAULT_PORT})",
    )
    parser.add_argument(
        "--simulate", action="store_true",
        help="Simulation mode - no real alerts sent",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Enable debug logging",
    )
    return parser.parse_args()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    args = parse_args()
    
    if args.verbose:
        logger.setLevel(logging.DEBUG)
    
    # Load configuration
    escalation_cfg, full_config = load_config(args.config)
    
    # Get dog name from config
    dog_name = full_config.get("dog", {}).get("name", "Dog")
    
    # Get Telegram token from config
    telegram_token = full_config.get("alerts", {}).get("telegram", {}).get("bot_token")
    
    # Create escalation manager
    manager = EscalationManager(
        config=escalation_cfg,
        simulate=args.simulate,
        telegram_token=telegram_token,
    )
    manager.set_dog_name(dog_name)
    
    # Register callbacks for logging
    def on_escalation(alert: EmergencyAlert):
        logger.critical(
            "ESCALATION EVENT: Alert %s escalated to level %d",
            alert.alert_id, alert.current_level.value
        )
    
    def on_acknowledge(alert: EmergencyAlert):
        logger.info(
            "ACKNOWLEDGMENT: Alert %s acknowledged by %s",
            alert.alert_id, alert.acknowledged_by
        )
    
    def on_resolve(alert: EmergencyAlert):
        logger.info(
            "RESOLUTION: Alert %s resolved",
            alert.alert_id
        )
    
    manager.register_on_escalation(on_escalation)
    manager.register_on_acknowledge(on_acknowledge)
    manager.register_on_resolve(on_resolve)
    
    # Create webhook listener
    webhook_listener = WebhookListener(manager)
    
    # Set up HTTP server
    EmergencyAPIHandler.manager = manager
    server = HTTPServer(("127.0.0.1", args.port), EmergencyAPIHandler)
    
    server_thread = threading.Thread(
        target=server.serve_forever,
        name="emergency-api-http",
        daemon=True,
    )
    server_thread.start()
    
    logger.info("=" * 60)
    logger.info("Emergency Contact Escalation Module")
    logger.info("=" * 60)
    logger.info("HTTP API: http://127.0.0.1:%d/emergency/*", args.port)
    logger.info("Dog Name: %s", dog_name)
    logger.info("Simulation Mode: %s", "YES" if args.simulate else "NO")
    logger.info("Escalation Timeout: %d minutes", escalation_cfg.escalation_timeout_min)
    logger.info("Max Escalation Levels: %d", escalation_cfg.max_escalation_levels)
    logger.info("Contacts Configured:")
    logger.info("  Primary: %s", escalation_cfg.primary.name if escalation_cfg.primary else "NOT SET")
    logger.info("  Secondary: %s", escalation_cfg.secondary.name if escalation_cfg.secondary else "NOT SET")
    logger.info("  Emergency: %s", escalation_cfg.emergency.name if escalation_cfg.emergency else "NOT SET")
    logger.info("=" * 60)
    
    # Graceful shutdown
    def shutdown(signum: int, frame: Any) -> None:
        logger.info("Received signal %d - shutting down...", signum)
        manager.shutdown()
        server.shutdown()
        logger.info("Emergency contact module stopped.")
        sys.exit(0)
    
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    
    # Keep main thread alive
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        shutdown(signal.SIGINT, None)


if __name__ == "__main__":
    main()
