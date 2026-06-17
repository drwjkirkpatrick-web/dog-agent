#!/usr/bin/env python3
"""
Dog Agent Orchestrator — main.py
=================================
Orchestrates all dog-agent sub-modules via subprocess (Popen) management.

Each sub-module runs as an independent subprocess with its own HTTP server
on a dedicated port.  The orchestrator provides a unified dashboard on
http://127.0.0.1:9110/ with health/status of all modules.

Port assignments:
  9110 — Orchestrator dashboard
  9111 — GPS daemon
  9112 — Sensor daemon
  9113 — Health monitor
  9114 — Geofence
  9115 — Behavior
  9116 — Voice
  9117 — Data logger

Usage:
    python src/main.py --all                  # Start all enabled modules
    python src/main.py --gps-only             # Start only GPS
    python src/main.py --health-only          # Start health + GPS + sensors
    python src/main.py --simulate             # Run all in simulation mode
    python src/main.py --list-modules         # Print available modules and exit
"""

from __future__ import annotations

import argparse
import atexit
import json
import logging
import os
import signal
import subprocess  # noqa: S404 — Popen for managed subprocesses
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

# v4.0: Import shared infrastructure
try:
    from shared import ConfigCache, ConnectionPool
    SHARED_AVAILABLE = True
except ImportError:
    SHARED_AVAILABLE = False

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logger = logging.getLogger("orchestrator")
_handler = logging.StreamHandler(sys.stdout)
_handler.setFormatter(logging.Formatter(
    "\033[36m%(asctime)s\033[0m [\033[1m%(levelname)s\033[0m] \033[33m%(name)s\033[0m: %(message)s",
    datefmt="%H:%M:%S",
))
logger.addHandler(_handler)
logger.setLevel(logging.INFO)

# ---------------------------------------------------------------------------
# ANSI colour helpers
# ---------------------------------------------------------------------------
class C:
    GREEN = "\033[32m"
    RED = "\033[31m"
    YELLOW = "\033[33m"
    CYAN = "\033[36m"
    BOLD = "\033[1m"
    RESET = "\033[0m"

# ---------------------------------------------------------------------------
# Paths & constants
# ---------------------------------------------------------------------------
PROJECT_DIR = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_DIR / "src"
DATA_DIR = PROJECT_DIR / "data"
TEMP_DIR = PROJECT_DIR / ".orchestrator_tmp"
ORCHESTRATOR_PORT = 9110

# Module definitions: name -> metadata
# Each module can declare:
#   module (str):     Python file stem (e.g. "gps_daemon" → src/gps_daemon.py)
#   port (int):       Unique HTTP port for this module's API
#   config_key (str): Dot-delimited config path for enabled/disabled check (e.g. "gps.enabled")
#   sim_flag (str):   CLI flag to pass for simulation mode (e.g. "--simulate" or "--test")
#   has_port (bool):  If True, module accepts --port CLI argument
#   needs_config (bool): If True, module reads port from YAML config (no --port CLI)
#   runner (bool):    If True, a wrapper script is generated (for hardcoded-port modules)
MODULE_DEFS: Dict[str, Dict[str, Any]] = {
    "gps": {
        "module": "gps_daemon",
        "port": 9111,
        "config_key": "gps.enabled",
        "sim_flag": "--test",
        "needs_config": True,  # reads hermes.api_port from YAML
    },
    "sensors": {
        "module": "sensor_daemon",
        "port": 9112,
        "config_key": "sensors.enabled",
        "sim_flag": "--simulate",
        "needs_config": True,
    },
    "health": {
        "module": "health_monitor",
        "port": 9113,
        "config_key": "health",
        "sim_flag": "--simulate",
        "has_port": True,
    },
    "geofence": {
        "module": "geofence",
        "port": 9114,
        "config_key": "geofence",
        "sim_flag": "--test",
        "has_port": True,
    },
    "behavior": {
        "module": "behavior",
        "port": 9115,
        "config_key": "behavior",
        "sim_flag": "--simulate",
        "runner": True,  # needs wrapper because BEHAVIOR_API_PORT is hardcoded
    },
    "voice": {
        "module": "voice",
        "port": 9116,
        "config_key": "voice",
        "sim_flag": "--simulate",
        "has_port": True,
    },
    "logger": {
        "module": "data_logger",
        "port": 9117,
        "config_key": "logging",
        "sim_flag": None,
        "has_port": True,
    },
    "power": {
        "module": "power_manager",
        "port": 9120,
        "config_key": "power.deep_sleep.enabled",
        "sim_flag": "--simulate",
        "has_port": True,
    },
    "lorawan": {
        "module": "lorawan_backup",
        "port": 9140,
        "config_key": "lorawan.enabled",
        "sim_flag": "--simulate",
        "has_port": True,
    },
}

# Ordering for dashboard display
MODULE_ORDER = ["gps", "sensors", "health", "geofence", "behavior", "voice", "logger", "power", "lorawan"]

# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------
_full_config: Dict[str, Any] = {}
_config_mtime: float = 0


def load_config() -> Dict[str, Any]:
    """Load project config.yaml, caching it globally."""
    global _full_config, _config_mtime
    path = PROJECT_DIR / "config.yaml"
    if path.exists():
        try:
            mtime = path.stat().st_mtime
            if mtime != _config_mtime:
                with open(path) as f:
                    _full_config = yaml.safe_load(f) or {}
                _config_mtime = mtime
                logger.debug("Loaded config from %s", path)
        except Exception as exc:
            logger.warning("Failed to load config: %s", exc)
    else:
        _full_config = {}
    return _full_config


def config_enabled(config_key: str) -> bool:
    """Check whether a module is enabled in config via a dot-delimited key path."""
    cfg = load_config()
    keys = config_key.split(".")
    val: Any = cfg
    for k in keys:
        if isinstance(val, dict):
            val = val.get(k, {})
        else:
            return True  # key not found == enabled by default
    if isinstance(val, dict):
        return val.get("enabled", True)
    if isinstance(val, bool):
        return val
    return True


def config_changed() -> bool:
    """Return True if config.yaml has been modified since last load."""
    path = PROJECT_DIR / "config.yaml"
    if not path.exists():
        return False
    try:
        return path.stat().st_mtime != _config_mtime
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Temp config / runner generation
# ---------------------------------------------------------------------------
def _ensure_temp_dir() -> None:
    TEMP_DIR.mkdir(parents=True, exist_ok=True)


def _write_temp_config(name: str, port: int) -> str:
    """Write a temporary YAML config for a module that reads port from config.

    Returns the path to the temp file.
    """
    _ensure_temp_dir()
    base = load_config()
    config = dict(base)
    if "hermes" not in config:
        config["hermes"] = {}
    config["hermes"]["api_port"] = port

    path = TEMP_DIR / f"{name}_config.yaml"
    with open(path, "w") as f:
        yaml.dump(config, f, default_flow_style=False)
    logger.debug("Wrote temp config for '%s' → %s", name, path)
    return str(path)


def _write_behavior_runner(simulate: bool) -> str:
    """Generate a thin wrapper script for behavior.py that patches its
    hardcoded BEHAVIOR_API_PORT and CONFIG_PATH constants at runtime."""
    _ensure_temp_dir()
    port = MODULE_DEFS["behavior"]["port"]
    sim_flag = "--simulate" if simulate else ""

    # Write temp config for behavior (it reads CONFIG_PATH for dog/geofence settings)
    behavior_config_path = _write_temp_config("behavior", port)

    runner_path = TEMP_DIR / "_run_behavior.py"
    runner_code = f'''#!/usr/bin/env python3
"""Auto-generated runner for behavior module (port={port})."""
import sys, os
sys.path.insert(0, {str(PROJECT_DIR)!r})

# Load the module (module-level assignments will overwrite our constants)
import importlib
spec = importlib.util.spec_from_file_location("behavior", {str(SRC_DIR / "behavior.py")!r})
mod = importlib.util.module_from_spec(spec)
sys.modules["behavior"] = mod
spec.loader.exec_module(mod)

# Patch constants AFTER loading so exec_module doesn't overwrite them
mod.BEHAVIOR_API_PORT = {port}
mod.CONFIG_PATH = {behavior_config_path!r}

# Now start the daemon
mod.main()
'''
    with open(runner_path, "w") as f:
        f.write(runner_code)
    os.chmod(runner_path, 0o755)
    logger.debug("Wrote behavior runner → %s", runner_path)
    return str(runner_path)


# ---------------------------------------------------------------------------
# Module process management
# ---------------------------------------------------------------------------
class ManagedModule:
    """Tracks a single sub-module subprocess."""

    __slots__ = (
        "name", "port", "proc", "started_at", "error_log",
        "_lock",
    )

    def __init__(self, name: str, port: int) -> None:
        self.name = name
        self.port = port
        self.proc: Optional[subprocess.Popen] = None
        self.started_at: Optional[float] = None
        self.error_log: List[str] = []
        self._lock = threading.Lock()

    @property
    def is_running(self) -> bool:
        if self.proc is None:
            return False
        ret = self.proc.poll()
        if ret is not None:
            return False
        return True

    @property
    def exit_code(self) -> Optional[int]:
        if self.proc is None:
            return None
        return self.proc.poll()

    def start(self, cmd: List[str]) -> None:
        self.started_at = time.monotonic()
        self.error_log = []
        try:
            self.proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd=str(PROJECT_DIR),
            )
            logger.info(
                "%s[%s]%s started on port %d (PID %d)",
                C.GREEN, self.name, C.RESET, self.port, self.proc.pid,
            )
        except Exception as exc:
            logger.error("Failed to start %s: %s", self.name, exc)
            self.proc = None

    def stop(self) -> None:
        with self._lock:
            if self.proc is None:
                return
            if not self.is_running:
                self.proc = None
                return
            pid = self.proc.pid
            logger.info("Stopping %s (PID %d)...", self.name, pid)
            # Try graceful SIGTERM first
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                logger.warning("%s did not stop in 5s — sending SIGKILL", self.name)
                self.proc.kill()
                self.proc.wait(timeout=2)
            logger.info("%s[%s]%s stopped", C.RED, self.name, C.RESET)
            self.proc = None

    def health_check(self, timeout: float = 2.0) -> Optional[Dict[str, Any]]:
        """Try to fetch /health or equivalent from the module's HTTP endpoint."""
        try:
            import urllib.request
            url = f"http://127.0.0.1:{self.port}/health"
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return data
        except Exception:
            # Try module-specific health endpoints
            alt_paths = {
                "gps": "/gps/health",
                "sensors": "/sensors/health",
                "health": "/health",
                "geofence": "/geofence/health",
                "behavior": "/behavior/health",
                "voice": "/voice/health",
                "logger": "/logger/health",
            }
            alt = alt_paths.get(self.name, "/health")
            if alt != "/health":
                try:
                    import urllib.request
                    url = f"http://127.0.0.1:{self.port}{alt}"
                    req = urllib.request.Request(url)
                    with urllib.request.urlopen(req, timeout=timeout) as resp:
                        data = json.loads(resp.read().decode("utf-8"))
                        return data
                except Exception:
                    return None
            return None

    def capture_stderr(self) -> str:
        """Return any captured stderr output (non-blocking poll)."""
        if self.proc is None or self.proc.stderr is None:
            return ""
        try:
            lines = []
            while True:
                line = self.proc.stderr.readline()
                if not line:
                    break
                lines.append(line.rstrip())
            return "\n".join(lines[-20:])  # last 20 lines
        except Exception:
            return ""

    def status_dict(self) -> Dict[str, Any]:
        running = self.is_running
        return {
            "name": self.name,
            "port": self.port,
            "running": running,
            "pid": self.proc.pid if running and self.proc else None,
            "exit_code": self.exit_code,
            "uptime_seconds": round(time.monotonic() - self.started_at, 1) if running and self.started_at else None,
        }


# ---------------------------------------------------------------------------
# Module registry
# ---------------------------------------------------------------------------
_modules: Dict[str, ManagedModule] = {}
_modules_lock = threading.Lock()


def start_module(name: str, simulate: bool = False) -> Optional[ManagedModule]:
    """Start a single sub-module by name. Returns the ManagedModule or None."""
    if name not in MODULE_DEFS:
        logger.error("Unknown module: %s", name)
        return None

    meta = MODULE_DEFS[name]
    port = meta["port"]
    mod_name = meta["module"]

    with _modules_lock:
        if name in _modules and _modules[name].is_running:
            logger.warning("%s is already running", name)
            return _modules[name]

        managed = ManagedModule(name=name, port=port)
        cmd: List[str] = [sys.executable, str(SRC_DIR / f"{mod_name}.py")]

        # Simulation flag
        sim_flag = meta.get("sim_flag")
        if simulate and sim_flag:
            cmd.append(sim_flag)

        # Port argument
        if meta.get("has_port"):
            cmd.extend(["--port", str(port)])

        # Config file (for modules that read port from YAML)
        if meta.get("needs_config"):
            config_path = _write_temp_config(name, port)
            cmd.extend(["--config", config_path])

        # Special case: behavior needs a wrapper runner
        if meta.get("runner"):
            runner_path = _write_behavior_runner(simulate)
            cmd = [sys.executable, runner_path]

        managed.start(cmd)
        _modules[name] = managed
        return managed


def stop_module(name: str) -> None:
    """Stop a single sub-module by name."""
    with _modules_lock:
        managed = _modules.get(name)
        if managed is None:
            logger.warning("Module '%s' is not managed", name)
            return
        managed.stop()
        _modules.pop(name, None)


def stop_all_modules() -> None:
    """Gracefully stop all running modules."""
    logger.info("Stopping all modules...")
    with _modules_lock:
        names = list(_modules.keys())
    for name in reversed(names):
        stop_module(name)
    logger.info("All modules stopped.")


def get_enabled_modules(simulate: bool = False) -> List[str]:
    """Return the list of module names that should be started."""
    cfg = load_config()
    enabled = []
    for name in MODULE_ORDER:
        meta = MODULE_DEFS[name]
        # In simulate mode, start ALL modules (ignore config enabled flag)
        if simulate:
            enabled.append(name)
            continue
        # Check config
        if config_enabled(meta["config_key"]):
            enabled.append(name)
        else:
            logger.info("Module '%s' is disabled in config — skipping", name)
    return enabled


# ---------------------------------------------------------------------------
# Config watcher
# ---------------------------------------------------------------------------
_config_watch_stop = threading.Event()


def _config_watcher() -> None:
    """Background thread: check config.yaml for changes every 5s."""
    last_warn_time = 0.0
    while not _config_watch_stop.is_set():
        try:
            if config_changed():
                now = time.monotonic()
                if now - last_warn_time > 30:  # rate-limit warnings
                    logger.warning(
                        "%s[CONFIG]%s config.yaml has changed. "
                        "Restart modules to apply changes.",
                        C.YELLOW, C.RESET,
                    )
                    last_warn_time = now
                # Reload
                load_config()
        except Exception:
            pass
        _config_watch_stop.wait(5.0)


# ---------------------------------------------------------------------------
# Health polling thread
# ---------------------------------------------------------------------------
_last_health_results: Dict[str, Optional[Dict[str, Any]]] = {}
_health_poll_stop = threading.Event()


def _health_poller() -> None:
    """Background thread: poll each running module's health endpoint every 10s."""
    while not _health_poll_stop.is_set():
        with _modules_lock:
            mods = dict(_modules)
        results: Dict[str, Optional[Dict[str, Any]]] = {}
        for name, mod in mods.items():
            if mod.is_running:
                results[name] = mod.health_check()
            else:
                results[name] = None
        global _last_health_results
        _last_health_results = results
        _health_poll_stop.wait(10.0)


# ---------------------------------------------------------------------------
# Dashboard HTTP server
# ---------------------------------------------------------------------------
class DashboardHandler(BaseHTTPRequestHandler):
    """HTTP handler for the orchestrator dashboard on port 9110."""

    # Class-level references
    modules_ref: Dict[str, ManagedModule] = {}
    health_results_ref: Dict[str, Optional[Dict[str, Any]]] = {}

    # ------------------------------------------------------------------
    # Routes
    # ------------------------------------------------------------------

    def do_GET(self) -> None:
        if self.path == "/" or self.path == "":
            self._serve_dashboard()
        elif self.path == "/health":
            self._serve_consolidated_health()
        elif self.path == "/api/status":
            self._serve_json_status()
        elif self.path == "/api/modules":
            self._serve_modules_json()
        else:
            self._json_response({"error": "not found", "path": self.path}, 404)

    def do_POST(self) -> None:
        if self.path.startswith("/restart/"):
            module_name = self.path[len("/restart/"):].rstrip("/")
            self._handle_restart(module_name)
        elif self.path == "/shutdown":
            self._handle_shutdown()
        else:
            self._json_response({"error": "not found", "path": self.path}, 404)

    # ------------------------------------------------------------------
    # Dashboard HTML
    # ------------------------------------------------------------------

    def _serve_dashboard(self) -> None:
        """Render a minimal HTML dashboard with colour-coded module status."""
        html = self._build_dashboard_html()
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _build_dashboard_html(self) -> str:
        mods = self.modules_ref
        health = self.health_results_ref

        rows = ""
        for name in MODULE_ORDER:
            mm = mods.get(name)
            if mm is None:
                status_str = "not started"
                status_class = "unknown"
                port_str = str(MODULE_DEFS[name]["port"])
                uptime = "—"
            elif mm.is_running:
                status_str = "running"
                status_class = "running"
                port_str = str(mm.port)
                uptime = f"{mm.status_dict()['uptime_seconds']:.0f}s" if mm.status_dict().get('uptime_seconds') else "—"
            else:
                status_str = f"exited ({mm.exit_code})"
                status_class = "stopped"
                port_str = str(mm.port)
                uptime = "—"

            # Health indicator
            h = health.get(name)
            if h and isinstance(h, dict):
                h_status = h.get("status", "unknown")
                if h_status == "ok":
                    health_str = "✅ OK"
                else:
                    health_str = f"⚠️ {h_status}"
            elif mm and mm.is_running:
                health_str = "⏳ polling..."
            else:
                health_str = "—"

            rows += (
                f"<tr class='{status_class}'>"
                f"<td>{name}</td>"
                f"<td>{port_str}</td>"
                f"<td><span class='badge badge-{status_class}'>{status_str}</span></td>"
                f"<td>{health_str}</td>"
                f"<td>{uptime}</td>"
                f"<td><a href='/restart/{name}' class='btn-small'>restart</a></td>"
                f"</tr>\n"
            )

        return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Dog Agent — Orchestrator Dashboard</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
          background: #0d1117; color: #c9d1d9; padding: 2rem; }}
  h1 {{ font-size: 1.5rem; margin-bottom: 0.5rem; color: #58a6ff; }}
  .subtitle {{ color: #8b949e; font-size: 0.9rem; margin-bottom: 1.5rem; }}
  table {{ width: 100%; border-collapse: collapse; background: #161b22;
           border-radius: 8px; overflow: hidden; }}
  th {{ background: #21262d; padding: 0.75rem 1rem; text-align: left;
        font-size: 0.8rem; text-transform: uppercase; letter-spacing: 0.05em;
        color: #8b949e; border-bottom: 1px solid #30363d; }}
  td {{ padding: 0.75rem 1rem; border-bottom: 1px solid #21262d; font-size: 0.9rem; }}
  .badge {{ display: inline-block; padding: 0.15rem 0.5rem; border-radius: 999px;
            font-size: 0.75rem; font-weight: 600; }}
  .badge-running {{ background: #1b4721; color: #3fb950; }}
  .badge-stopped {{ background: #49201e; color: #f85149; }}
  .badge-unknown {{ background: #21262d; color: #8b949e; }}
  tr.running td:first-child {{ border-left: 3px solid #3fb950; }}
  tr.stopped td:first-child {{ border-left: 3px solid #f85149; }}
  .btn-small {{ display: inline-block; padding: 0.2rem 0.6rem; border-radius: 4px;
                background: #21262d; color: #58a6ff; text-decoration: none;
                font-size: 0.75rem; }}
  .btn-small:hover {{ background: #30363d; }}
  .actions {{ margin-top: 1.5rem; display: flex; gap: 0.5rem; }}
  .actions a {{ display: inline-block; padding: 0.5rem 1rem; border-radius: 6px;
                background: #21262d; color: #c9d1d9; text-decoration: none;
                font-size: 0.85rem; }}
  .actions a:hover {{ background: #30363d; }}
  .actions .danger {{ color: #f85149; }}
  .footer {{ margin-top: 2rem; color: #484f58; font-size: 0.75rem; }}
</style>
</head>
<body>
<h1>🐕 Dog Agent — Orchestrator</h1>
<p class="subtitle">Unified dashboard · {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}</p>
<table>
<thead><tr><th>Module</th><th>Port</th><th>Status</th><th>Health</th><th>Uptime</th><th>Action</th></tr></thead>
<tbody>
{rows}
</tbody>
</table>
<div class="actions">
<a href="/api/status">📊 JSON Status</a>
<a href="/shutdown" class="danger">⏹ Shutdown All</a>
</div>
<p class="footer">Dog Agent — Powered by Nous Research Hermes Agent</p>
</body>
</html>"""

    # ------------------------------------------------------------------
    # JSON endpoints
    # ------------------------------------------------------------------

    def _serve_consolidated_health(self) -> None:
        """Return consolidated health check as JSON."""
        mods = self.modules_ref
        health = self.health_results_ref

        all_running = True
        modules_status = {}
        for name in MODULE_ORDER:
            mm = mods.get(name)
            h = health.get(name)
            running = mm is not None and mm.is_running
            if not running:
                all_running = False
            modules_status[name] = {
                "running": running,
                "port": MODULE_DEFS[name]["port"],
                "health": h if h else None,
                "uptime_seconds": mm.status_dict().get("uptime_seconds") if mm and running else None,
            }

        self._json_response({
            "status": "ok" if all_running else "degraded",
            "service": "dog-agent-orchestrator",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "modules": modules_status,
            "all_modules_running": all_running,
            "running_count": sum(1 for m in modules_status.values() if m["running"]),
            "total_count": len(MODULE_ORDER),
        })

    def _serve_json_status(self) -> None:
        """Return a machine-readable status of all modules."""
        self._serve_consolidated_health()

    def _serve_modules_json(self) -> None:
        """Return module definitions list."""
        self._json_response({
            "modules": [
                {
                    "name": name,
                    "port": meta["port"],
                    "module_file": f"{meta['module']}.py",
                    "supports_simulate": meta.get("sim_flag") is not None,
                }
                for name, meta in MODULE_DEFS.items()
            ]
        })

    # ------------------------------------------------------------------
    # POST handlers
    # ------------------------------------------------------------------

    def _handle_restart(self, module_name: str) -> None:
        """Restart a single module."""
        if module_name not in MODULE_DEFS:
            self._json_response({"error": f"unknown module: {module_name}"}, 404)
            return

        logger.info("Restarting module '%s'...", module_name)
        stop_module(module_name)
        time.sleep(1)
        start_module(module_name, simulate=_simulate_mode)
        self._json_response({"status": "ok", "module": module_name, "action": "restart"})

    def _handle_shutdown(self) -> None:
        """Graceful shutdown of all modules."""
        logger.info("Shutdown requested via API")
        self._json_response({"status": "ok", "message": "shutting down all modules"})
        # Schedule shutdown in a separate thread so HTTP response is sent first
        t = threading.Thread(target=_graceful_shutdown, args=(0, None), daemon=True)
        t.start()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _json_response(self, data: dict, status: int = 200) -> None:
        body = json.dumps(data, indent=2, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: Any) -> None:
        """Quiet default HTTP logging."""
        logger.debug("HTTP: " + fmt % args)


# ---------------------------------------------------------------------------
# Global state
# ---------------------------------------------------------------------------
_simulate_mode = False
_dashboard_server: Optional[HTTPServer] = None


def start_dashboard() -> None:
    """Start the orchestrator dashboard HTTP server on port 9110."""
    global _dashboard_server
    DashboardHandler.modules_ref = _modules
    DashboardHandler.health_results_ref = _last_health_results

    try:
        _dashboard_server = HTTPServer(("127.0.0.1", ORCHESTRATOR_PORT), DashboardHandler)
        thread = threading.Thread(
            target=_dashboard_server.serve_forever,
            name="dashboard-http",
            daemon=True,
        )
        thread.start()
        logger.info(
            "%s[DASHBOARD]%s 🖥️  http://127.0.0.1:%d/",
            C.CYAN, C.RESET, ORCHESTRATOR_PORT,
        )
    except OSError as exc:
        logger.error("Cannot bind dashboard to port %d: %s", ORCHESTRATOR_PORT, exc)
        sys.exit(1)


# ---------------------------------------------------------------------------
# Graceful shutdown
# ---------------------------------------------------------------------------
_shutdown_in_progress = False


def _graceful_shutdown(signum: Optional[int], frame: Any) -> None:
    """Cleanly stop all modules, config watcher, health poller, and dashboard."""
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
    logger.info("%s[SHUTDOWN]%s Received %s — shutting down...", C.YELLOW, C.RESET, sig_name)

    # Stop background threads
    _config_watch_stop.set()
    _health_poll_stop.set()

    # Stop dashboard server
    if _dashboard_server:
        _dashboard_server.shutdown()

    # Stop all subprocess modules
    stop_all_modules()

    # Cleanup temp directory
    try:
        import shutil
        if TEMP_DIR.exists():
            shutil.rmtree(TEMP_DIR)
            logger.debug("Cleaned up %s", TEMP_DIR)
    except Exception:
        pass

    logger.info("%s[DONE]%s All modules stopped. Goodbye.", C.GREEN, C.RESET)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def print_module_list() -> None:
    """Print available modules and their ports."""
    print(f"\n{C.BOLD}Dog Agent — Available Modules{C.RESET}\n")
    print(f"  {'Name':<12} {'Port':<6} {'File':<20} {'Sim Flag':<12} Enabled?")
    print(f"  {'─'*12} {'─'*6} {'─'*20} {'─'*12} {'─'*10}")
    for name in MODULE_ORDER:
        meta = MODULE_DEFS[name]
        enabled = config_enabled(meta["config_key"])
        flag = meta.get("sim_flag") or "—"
        en_str = f"{C.GREEN}yes{C.RESET}" if enabled else f"{C.RED}no{C.RESET}"
        print(f"  {name:<12} {meta['port']:<6} {meta['module']+'.py':<20} {str(flag):<12} {en_str}")
    print()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Dog Agent Orchestrator — start/stop all sub-modules",
    )
    parser.add_argument(
        "--all", action="store_true",
        help="Start all enabled modules",
    )
    parser.add_argument(
        "--gps-only", action="store_true",
        help="Start only the GPS module",
    )
    parser.add_argument(
        "--health-only", action="store_true",
        help="Start health monitor + GPS + sensors",
    )
    parser.add_argument(
        "--simulate", action="store_true",
        help="Run all modules in simulation mode (no hardware needed)",
    )
    parser.add_argument(
        "--list-modules", action="store_true",
        help="Print available modules and exit",
    )
    return parser.parse_args()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    global _simulate_mode

    args = parse_args()

    # Load config
    load_config()

    # --list-modules
    if args.list_modules:
        print_module_list()
        return

    # Determine which modules to start
    if args.simulate:
        _simulate_mode = True

    modules_to_start: List[str] = []

    if args.all:
        modules_to_start = get_enabled_modules(simulate=args.simulate)
        logger.info(
            "Starting ALL modules (%s)", ", ".join(modules_to_start),
        )
    elif args.gps_only:
        modules_to_start = ["gps"]
        logger.info("Starting GPS-only mode")
    elif args.health_only:
        modules_to_start = ["gps", "sensors", "health"]
        logger.info("Starting health-monitoring mode (GPS + sensors + health)")
    else:
        # Default: start all enabled modules
        modules_to_start = get_enabled_modules(simulate=args.simulate)
        logger.info(
            "Starting enabled modules: %s", ", ".join(modules_to_start) or "(none)",
        )

    # Start config watcher
    config_thread = threading.Thread(
        target=_config_watcher, name="config-watcher", daemon=True,
    )
    config_thread.start()

    # Start health poller
    poll_thread = threading.Thread(
        target=_health_poller, name="health-poller", daemon=True,
    )
    poll_thread.start()

    # Start dashboard
    start_dashboard()

    # Start selected modules
    for name in modules_to_start:
        start_module(name, simulate=args.simulate)

    # Register signal handlers
    signal.signal(signal.SIGTERM, _graceful_shutdown)
    signal.signal(signal.SIGINT, _graceful_shutdown)
    atexit.register(lambda: _graceful_shutdown(None, None))

    # Log summary
    logger.info(
        "%s[READY]%s Dog Agent orchestrator running. Dashboard: http://127.0.0.1:%d/",
        C.GREEN, C.RESET, ORCHESTRATOR_PORT,
    )

    # Keep main thread alive
    try:
        while not _shutdown_in_progress:
            time.sleep(1)
    except KeyboardInterrupt:
        _graceful_shutdown(signal.SIGINT, None)


if __name__ == "__main__":
    main()