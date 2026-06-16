#!/usr/bin/env python3
"""
Dog Agent — Weather Integration Module
======================================
Integrates with OpenWeatherMap API and local BME280 sensor to provide
weather-aware alerts and optimal walk time suggestions.

Features:
  - Current weather conditions
  - Hourly forecast (next 24 hours)
  - Heat stress warnings
  - UV exposure alerts
  - Optimal walk time recommendations
  - Weather-dog behavior correlation

Usage:
    python src/weather_integration.py
    python src/weather_integration.py --simulate
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
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlencode
from urllib.request import urlopen, URLError

import yaml

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logger = logging.getLogger("weather")
_handler = logging.StreamHandler(sys.stdout)
_handler.setFormatter(logging.Formatter(
    "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
))
logger.addHandler(_handler)
logger.setLevel(logging.INFO)

# ---------------------------------------------------------------------------
# Paths & Constants
# ---------------------------------------------------------------------------
PROJECT_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_DIR / "config.yaml"
WEATHER_CACHE_PATH = PROJECT_DIR / "data" / "weather_cache.json"

# OpenWeatherMap API (free tier: 60 calls/min, 1M calls/month)
OWM_API_URL = "https://api.openweathermap.org/data/2.5"

# Weather condition thresholds
HEAT_STRESS_TEMP_C = 30.0      # Too hot for long walks
COLD_STRESS_TEMP_C = -5.0      # Too cold for sensitive breeds
HIGH_UV_INDEX = 6              # High UV warning
WALK_TEMP_MIN_C = 5.0          # Minimum comfortable walk temp
WALK_TEMP_MAX_C = 25.0         # Maximum comfortable walk temp

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
def load_config() -> dict:
    """Load configuration from config.yaml."""
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH) as f:
                return yaml.safe_load(f) or {}
        except Exception as e:
            logger.warning(f"Failed to load config: {e}")
    return {}


def get_cfg(path: str, default: Any = None) -> Any:
    """Get config value by dot-delimited path."""
    cfg = load_config()
    for key in path.split("."):
        if isinstance(cfg, dict):
            cfg = cfg.get(key)
        else:
            return default
    return cfg if cfg is not None else default


# ---------------------------------------------------------------------------
# Weather Data Models
# ---------------------------------------------------------------------------
@dataclass
class WeatherConditions:
    """Current weather conditions."""
    temperature_c: float
    feels_like_c: float
    humidity_percent: float
    pressure_hpa: float
    wind_speed_ms: float
    wind_deg: int
    weather_main: str      # Clear, Clouds, Rain, Snow, etc.
    weather_desc: str      # Detailed description
    visibility_m: int
    uv_index: float
    timestamp: datetime
    
    def to_dict(self) -> dict:
        return {
            "temperature_c": self.temperature_c,
            "feels_like_c": self.feels_like_c,
            "humidity_percent": self.humidity_percent,
            "pressure_hpa": self.pressure_hpa,
            "wind_speed_ms": self.wind_speed_ms,
            "wind_deg": self.wind_deg,
            "weather_main": self.weather_main,
            "weather_desc": self.weather_desc,
            "visibility_m": self.visibility_m,
            "uv_index": self.uv_index,
            "timestamp": self.timestamp.isoformat(),
        }


@dataclass
class WalkRecommendation:
    """Recommendation for walk timing."""
    recommended: bool
    time_slot: str           # "morning", "afternoon", "evening"
    start_time: str          # "07:00", etc.
    end_time: str
    temperature_c: float
    reason: str              # Why this time is good/bad
    alerts: list             # Any warnings


# ---------------------------------------------------------------------------
# Weather Service
# ---------------------------------------------------------------------------
class WeatherService:
    """Fetches and caches weather data from OpenWeatherMap."""

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or get_cfg("weather.api_key")
        self.lat = get_cfg("geofence.home_zone.lat", 45.5152)
        self.lon = get_cfg("geofence.home_zone.lon", -122.6784)
        self.cache_duration_sec = get_cfg("weather.cache_duration_min", 10) * 60
        self._cache: Optional[WeatherConditions] = None
        self._cache_time: float = 0
        self._lock = threading.Lock()
        
        if not self.api_key:
            logger.warning("No OpenWeatherMap API key configured")

    def fetch_current(self) -> Optional[WeatherConditions]:
        """Fetch current weather from API or cache."""
        with self._lock:
            # Check cache
            if self._cache and time.time() - self._cache_time < self.cache_duration_sec:
                logger.debug("Using cached weather data")
                return self._cache

        if not self.api_key:
            return None

        try:
            url = f"{OWM_API_URL}/weather?" + urlencode({
                "lat": self.lat,
                "lon": self.lon,
                "appid": self.api_key,
                "units": "metric",
            })
            
            with urlopen(url, timeout=10) as response:
                data = json.loads(response.read().decode())
                
            conditions = WeatherConditions(
                temperature_c=data["main"]["temp"],
                feels_like_c=data["main"]["feels_like"],
                humidity_percent=data["main"]["humidity"],
                pressure_hpa=data["main"]["pressure"],
                wind_speed_ms=data["wind"]["speed"],
                wind_deg=data["wind"].get("deg", 0),
                weather_main=data["weather"][0]["main"],
                weather_desc=data["weather"][0]["description"],
                visibility_m=data.get("visibility", 10000),
                uv_index=0.0,  # UV requires separate API call
                timestamp=datetime.now(timezone.utc),
            )
            
            with self._lock:
                self._cache = conditions
                self._cache_time = time.time()
                
            logger.info(f"Fetched weather: {conditions.temperature_c}°C, {conditions.weather_desc}")
            return conditions
            
        except (URLError, json.JSONDecodeError, KeyError) as e:
            logger.error(f"Failed to fetch weather: {e}")
            return None

    def get_forecast(self) -> list:
        """Fetch hourly forecast (simplified)."""
        if not self.api_key:
            return []
            
        try:
            url = f"{OWM_API_URL}/forecast?" + urlencode({
                "lat": self.lat,
                "lon": self.lon,
                "appid": self.api_key,
                "units": "metric",
                "cnt": 8,  # Next 24 hours (3-hour steps)
            })
            
            with urlopen(url, timeout=10) as response:
                data = json.loads(response.read().decode())
                
            forecast = []
            for item in data.get("list", []):
                forecast.append({
                    "time": item["dt_txt"],
                    "temp_c": item["main"]["temp"],
                    "feels_like_c": item["main"]["feels_like"],
                    "weather": item["weather"][0]["description"],
                    "rain_chance": item.get("pop", 0) * 100,
                })
            return forecast
            
        except Exception as e:
            logger.error(f"Failed to fetch forecast: {e}")
            return []


# ---------------------------------------------------------------------------
# Weather Analyzer
# ---------------------------------------------------------------------------
class WeatherAnalyzer:
    """Analyzes weather conditions for dog safety."""

    def __init__(self, service: WeatherService):
        self.service = service

    def check_heat_stress(self, conditions: WeatherConditions) -> Optional[dict]:
        """Check for heat stress conditions."""
        if conditions.temperature_c > HEAT_STRESS_TEMP_C:
            return {
                "level": "high",
                "message": f"Heat stress risk: {conditions.temperature_c}°C. Limit walks to early morning/late evening.",
                "recommendation": "Keep walks under 15 minutes. Avoid pavement. Bring water.",
            }
        elif conditions.temperature_c > 25:
            return {
                "level": "moderate",
                "message": f"Warm weather: {conditions.temperature_c}°C. Monitor for overheating.",
                "recommendation": "Shorter walks, seek shade.",
            }
        return None

    def check_uv_exposure(self, uv_index: float) -> Optional[dict]:
        """Check for UV warnings."""
        if uv_index >= HIGH_UV_INDEX:
            return {
                "level": "high",
                "message": f"High UV index: {uv_index}. Risk for light-coated dogs.",
                "recommendation": "Apply dog-safe sunscreen or use protective clothing.",
            }
        return None

    def check_cold_stress(self, conditions: WeatherConditions) -> Optional[dict]:
        """Check for cold stress conditions."""
        if conditions.temperature_c < COLD_STRESS_TEMP_C:
            return {
                "level": "high",
                "message": f"Cold stress risk: {conditions.temperature_c}°C. Limit outdoor time.",
                "recommendation": "Short walks only. Consider booties for paw protection.",
            }
        return None

    def get_walk_recommendations(self, forecast: list) -> list:
        """Get optimal walk times from forecast."""
        recommendations = []
        
        for item in forecast[:5]:  # Next 15 hours
            temp = item["temp_c"]
            rain = item["rain_chance"]
            time_str = item["time"].split()[1][:5]  # HH:MM
            
            # Score this time slot
            temp_score = 1.0 - abs(temp - 15) / 15  # Optimal at 15°C
            temp_score = max(0, min(1, temp_score))
            rain_score = 1.0 - rain / 100
            
            score = (temp_score * 0.6 + rain_score * 0.4) * 100
            
            if score > 70:
                recommendations.append({
                    "time": time_str,
                    "temperature_c": round(temp, 1),
                    "rain_chance": rain,
                    "score": round(score, 1),
                    "recommendation": "Excellent walk conditions" if score > 85 else "Good walk conditions",
                })
        
        return sorted(recommendations, key=lambda x: x["score"], reverse=True)[:3]

    def generate_daily_summary(self) -> dict:
        """Generate comprehensive weather summary for the day."""
        conditions = self.service.fetch_current()
        if not conditions:
            return {"error": "Weather data unavailable"}
            
        forecast = self.service.get_forecast()
        
        alerts = []
        
        heat = self.check_heat_stress(conditions)
        if heat:
            alerts.append(heat)
            
        cold = self.check_cold_stress(conditions)
        if cold:
            alerts.append(cold)
            
        uv = self.check_uv_exposure(conditions.uv_index)
        if uv:
            alerts.append(uv)
        
        recommendations = self.get_walk_recommendations(forecast)
        
        return {
            "current": conditions.to_dict(),
            "alerts": alerts,
            "walk_recommendations": recommendations,
            "summary": f"{conditions.weather_desc.capitalize()}, {conditions.temperature_c}°C (feels like {conditions.feels_like_c}°C)",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }


# ---------------------------------------------------------------------------
# HTTP API Handler
# ---------------------------------------------------------------------------
class WeatherHTTPHandler(BaseHTTPRequestHandler):
    """HTTP request handler for weather API."""

    weather_service: Optional[WeatherService] = None
    weather_analyzer: Optional[WeatherAnalyzer] = None

    def log_message(self, format, *args):
        logger.debug(f"HTTP: {format % args}")

    def _send_json(self, data: Any, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data, indent=2, default=str).encode())

    def _send_error(self, status: int, message: str) -> None:
        self._send_json({"error": message}, status)

    def do_GET(self):
        path = self.path.strip("/")
        
        if path == "weather/health":
            self._send_json({
                "status": "ok",
                "service": "weather_integration",
                "api_configured": bool(self.weather_service and self.weather_service.api_key),
            })
            
        elif path == "weather/current":
            if not self.weather_service:
                self._send_error(503, "Weather service not initialized")
                return
            conditions = self.weather_service.fetch_current()
            if conditions:
                self._send_json(conditions.to_dict())
            else:
                self._send_error(503, "Weather data unavailable")
                
        elif path == "weather/summary":
            if not self.weather_analyzer:
                self._send_error(503, "Weather analyzer not initialized")
                return
            summary = self.weather_analyzer.generate_daily_summary()
            self._send_json(summary)
            
        elif path == "weather/forecast":
            if not self.weather_service:
                self._send_error(503, "Weather service not initialized")
                return
            forecast = self.weather_service.get_forecast()
            self._send_json({"forecast": forecast})
            
        elif path == "weather/walk-times":
            if not self.weather_analyzer:
                self._send_error(503, "Weather analyzer not initialized")
                return
            forecast = self.weather_service.get_forecast() if self.weather_service else []
            recommendations = self.weather_analyzer.get_walk_recommendations(forecast)
            self._send_json({"recommendations": recommendations})
            
        else:
            self._send_error(404, f"Unknown endpoint: {path}")

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()


# ---------------------------------------------------------------------------
# Simulation Mode
# ---------------------------------------------------------------------------
class WeatherSimulator:
    """Simulates weather data for testing."""

    def __init__(self, service: WeatherService, analyzer: WeatherAnalyzer):
        self.service = service
        self.analyzer = analyzer

    def run(self):
        """Run weather simulation scenarios."""
        logger.info("=== Weather Simulation Mode ===\n")
        
        # Simulate various weather conditions
        test_conditions = [
            WeatherConditions(
                temperature_c=32.0,
                feels_like_c=35.0,
                humidity_percent=65,
                pressure_hpa=1013,
                wind_speed_ms=2.0,
                wind_deg=180,
                weather_main="Clear",
                weather_desc="clear sky",
                visibility_m=10000,
                uv_index=8.0,
                timestamp=datetime.now(timezone.utc),
            ),
            WeatherConditions(
                temperature_c=8.0,
                feels_like_c=5.0,
                humidity_percent=80,
                pressure_hpa=1000,
                wind_speed_ms=5.0,
                wind_deg=270,
                weather_main="Rain",
                weather_desc="light rain",
                visibility_m=5000,
                uv_index=1.0,
                timestamp=datetime.now(timezone.utc),
            ),
        ]
        
        for i, conditions in enumerate(test_conditions, 1):
            logger.info(f"[Scenario {i}] {conditions.temperature_c}°C, {conditions.weather_desc}")
            
            heat = self.analyzer.check_heat_stress(conditions)
            if heat:
                logger.info(f"  Heat alert: {heat['message']}")
            
            cold = self.analyzer.check_cold_stress(conditions)
            if cold:
                logger.info(f"  Cold alert: {cold['message']}")
            
            uv = self.analyzer.check_uv_exposure(conditions.uv_index)
            if uv:
                logger.info(f"  UV alert: {uv['message']}")
            
            if not heat and not cold:
                logger.info("  No weather alerts")
            
            logger.info("")
        
        logger.info("=== Simulation Complete ===")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Dog Agent — Weather Integration")
    parser.add_argument("--port", type=int, default=9127, help="HTTP API port")
    parser.add_argument("--simulate", action="store_true", help="Run simulation mode")
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose logging")
    args = parser.parse_args()

    if args.verbose:
        logger.setLevel(logging.DEBUG)

    # Initialize services
    service = WeatherService()
    analyzer = WeatherAnalyzer(service)

    if args.simulate:
        sim = WeatherSimulator(service, analyzer)
        sim.run()
        return

    # Set up HTTP handler
    WeatherHTTPHandler.weather_service = service
    WeatherHTTPHandler.weather_analyzer = analyzer

    # Start HTTP server
    server = HTTPServer(("127.0.0.1", args.port), WeatherHTTPHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    logger.info(f"Weather API running on http://127.0.0.1:{args.port}")

    # Run until interrupted
    def signal_handler(sig, frame):
        logger.info(f"Received signal {sig}")
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
        logger.info("Weather module stopped")


if __name__ == "__main__":
    from dataclasses import dataclass
    main()