#!/usr/bin/env python3
"""
Dog Agent — OTA Updates
=======================
Over-the-air software updates without removing device from dog.

Features:
  - Check GitHub releases for updates
  - Download and verify updates
  - Install on next reboot
  - Rollback on failure

Usage:
    python src/ota_updates.py
    python src/ota_updates.py --check
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.request import urlopen, URLError

import yaml

# Logging
logger = logging.getLogger("ota_updates")
handler = logging.StreamHandler(sys.stdout)
handler.setFormatter(logging.Formatter(
    "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
))
logger.addHandler(handler)
logger.setLevel(logging.INFO)

PROJECT_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_DIR / "config.yaml"
UPDATE_DIR = PROJECT_DIR / "updates"
VERSION_FILE = PROJECT_DIR / ".version"

GITHUB_API_URL = "https://api.github.com/repos/drwjkirkpatrick-web/dog-agent/releases/latest"


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
class UpdateInfo:
    version: str
    download_url: str
    checksum: str
    release_notes: str
    size_bytes: int


class OTAUpdater:
    """Handles over-the-air updates."""
    
    def __init__(self):
        self.enabled = get_cfg("ota.enabled", False)
        self.check_interval_hours = get_cfg("ota.check_interval_hours", 24)
        self.auto_install = get_cfg("ota.auto_install", False)
        self.github_repo = get_cfg("ota.github_repo", "drwjkirkpatrick-web/dog-agent")
        
        self._current_version = self._load_current_version()
        self._pending_update: Optional[UpdateInfo] = None
        self._lock = threading.Lock()
        
        UPDATE_DIR.mkdir(exist_ok=True)
    
    def _load_current_version(self) -> str:
        if VERSION_FILE.exists():
            return VERSION_FILE.read_text().strip()
        return "2.0.0"  # Default
    
    def check_for_update(self) -> Optional[UpdateInfo]:
        """Check GitHub for new release."""
        if not self.enabled:
            return None
        
        try:
            url = f"https://api.github.com/repos/{self.github_repo}/releases/latest"
            with urlopen(url, timeout=30) as response:
                data = json.loads(response.read().decode())
            
            latest_version = data["tag_name"].lstrip("v")
            
            if self._is_newer(latest_version, self._current_version):
                # Find asset
                asset = None
                for a in data.get("assets", []):
                    if a["name"].endswith(".tar.gz"):
                        asset = a
                        break
                
                if asset:
                    return UpdateInfo(
                        version=latest_version,
                        download_url=asset["browser_download_url"],
                        checksum="",  # Would be in release notes or separate file
                        release_notes=data.get("body", ""),
                        size_bytes=asset["size"],
                    )
            
            return None
            
        except (URLError, json.JSONDecodeError) as e:
            logger.error(f"Failed to check for updates: {e}")
            return None
    
    def _is_newer(self, new: str, current: str) -> bool:
        """Compare version strings."""
        def parse(v):
            return [int(x) for x in v.split(".")]
        return parse(new) > parse(current)
    
    def download_update(self, update: UpdateInfo) -> bool:
        """Download update package."""
        try:
            logger.info(f"Downloading update {update.version}")
            
            update_file = UPDATE_DIR / f"update_{update.version}.tar.gz"
            
            with urlopen(update.download_url, timeout=300) as response:
                with open(update_file, "wb") as f:
                    f.write(response.read())
            
            # Verify size
            if update_file.stat().st_size != update.size_bytes:
                logger.error("Download size mismatch")
                return False
            
            with self._lock:
                self._pending_update = update
            
            logger.info(f"Update {update.version} downloaded successfully")
            return True
            
        except Exception as e:
            logger.error(f"Failed to download update: {e}")
            return False
    
    def install_update(self) -> bool:
        """Install pending update."""
        with self._lock:
            update = self._pending_update
        
        if not update:
            logger.warning("No pending update to install")
            return False
        
        try:
            update_file = UPDATE_DIR / f"update_{update.version}.tar.gz"
            
            # Create backup
            backup_dir = PROJECT_DIR / "backup"
            logger.info("Creating backup...")
            subprocess.run(
                ["tar", "-czf", str(backup_dir / "backup.tar.gz"), "-C", str(PROJECT_DIR), "."],
                check=True
            )
            
            # Extract update
            logger.info(f"Installing update {update.version}")
            subprocess.run(
                ["tar", "-xzf", str(update_file), "-C", str(PROJECT_DIR)],
                check=True
            )
            
            # Update version file
            VERSION_FILE.write_text(update.version)
            
            logger.info(f"Update {update.version} installed. Reboot to activate.")
            return True
            
        except subprocess.CalledProcessError as e:
            logger.error(f"Failed to install update: {e}")
            self._rollback()
            return False
    
    def _rollback(self):
        """Restore from backup."""
        logger.warning("Rolling back to previous version...")
        try:
            backup_file = PROJECT_DIR / "backup" / "backup.tar.gz"
            if backup_file.exists():
                subprocess.run(
                    ["tar", "-xzf", str(backup_file), "-C", str(PROJECT_DIR)],
                    check=True
                )
                logger.info("Rollback complete")
        except Exception as e:
            logger.error(f"Rollback failed: {e}")
    
    def get_status(self) -> dict:
        with self._lock:
            pending = self._pending_update
        
        return {
            "current_version": self._current_version,
            "pending_version": pending.version if pending else None,
            "update_available": pending is not None,
            "auto_install": self.auto_install,
            "last_check": datetime.now(timezone.utc).isoformat(),
        }


class OTAHTTPHandler(BaseHTTPRequestHandler):
    """HTTP API for OTA updates."""
    
    updater: Optional[OTAUpdater] = None
    
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
        
        if path == "ota/health":
            self._send_json({
                "status": "ok",
                "service": "ota_updates",
                "enabled": bool(self.updater and self.updater.enabled),
            })
        elif path == "ota/status":
            if not self.updater:
                self._send_json({"error": "Updater not initialized"}, 503)
                return
            self._send_json(self.updater.get_status())
        else:
            self._send_json({"error": f"Unknown endpoint: {path}"}, 404)
    
    def do_POST(self):
        path = self.path.strip("/")
        
        if path == "ota/check":
            if not self.updater:
                self._send_json({"error": "Updater not initialized"}, 503)
                return
            update = self.updater.check_for_update()
            self._send_json({
                "update_available": update is not None,
                "version": update.version if update else None,
            })
        elif path == "ota/install":
            if not self.updater:
                self._send_json({"error": "Updater not initialized"}, 503)
                return
            success = self.updater.install_update()
            self._send_json({"installed": success})
        else:
            self._send_json({"error": f"Unknown endpoint: {path}"}, 404)


def main():
    parser = argparse.ArgumentParser(description="Dog Agent — OTA Updates")
    parser.add_argument("--port", type=int, default=9142, help="HTTP API port")
    parser.add_argument("--check", action="store_true", help="Check for updates now")
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose logging")
    args = parser.parse_args()
    
    if args.verbose:
        logger.setLevel(logging.DEBUG)
    
    updater = OTAUpdater()
    
    if args.check:
        update = updater.check_for_update()
        if update:
            print(f"Update available: {update.version}")
            print(f"Release notes: {update.release_notes[:200]}...")
        else:
            print("No updates available")
        return
    
    OTAHTTPHandler.updater = updater
    
    server = HTTPServer(("127.0.0.1", args.port), OTAHTTPHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    logger.info(f"OTA API on http://127.0.0.1:{args.port}")
    
    # Background check loop
    def check_loop():
        while True:
            if updater.enabled:
                update = updater.check_for_update()
                if update and updater.auto_install:
                    updater.download_update(update)
            time.sleep(updater.check_interval_hours * 3600)
    
    check_thread = threading.Thread(target=check_loop, daemon=True)
    check_thread.start()
    
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
        logger.info("OTA module stopped")


if __name__ == "__main__":
    main()
