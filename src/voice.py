#!/usr/bin/env python3
"""
Voice Module — Dog Agent
========================
Provides voice interaction capabilities:
  - Text-to-Speech (TTS) via edge-tts CLI
  - Speech-to-Text (STT) stub via sounddevice capture (Whisper/vosk ready)
  - Bark detection via numpy FFT analysis

Exposes HTTP API on localhost:9110/voice:
  POST /voice/say          — Speak text through speaker
  GET  /voice/bark/status  — Last bark detection result + count today
  POST /voice/bark/listen  — Manual 10-second listen window
  GET  /voice/health       — Health check

Usage:
    python src/voice.py                          # Normal mode
    python src/voice.py --simulate               # Simulate mode
    python src/voice.py --config /path/to/config.yaml
    python src/voice.py --port 9111
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import random
import signal
import subprocess  # noqa: S404 — subprocess used for edge-tts CLI only
import sys
import threading
import time
from collections import deque
from datetime import datetime, date, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import yaml

try:
    import sounddevice as sd
except ImportError:
    sd = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logger = logging.getLogger("voice")
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
SAMPLE_RATE = 16000            # Audio capture sample rate (Hz)
BUFFER_SECONDS = 1.0           # Analysis buffer size (seconds)
BARK_LOW_FREQ = 100            # Bark band low (Hz)
BARK_HIGH_FREQ = 800           # Bark band high (Hz)
TOTAL_HIGH_FREQ = 3000         # Total analysis band high (Hz)
BARK_POWER_THRESHOLD = 0.40    # Fraction of total power in bark band
NOISE_FLOOR_WINDOW = 60        # Rolling noise floor window (seconds)
DEFAULT_LISTEN_SECONDS = 10    # Manual listen window duration (seconds)


# ---------------------------------------------------------------------------
# Configuration loader
# ---------------------------------------------------------------------------
def load_config(path: str) -> dict:
    """Load YAML config, returning defaults for missing voice keys."""
    with open(path) as f:
        cfg = yaml.safe_load(f) or {}

    voice_cfg = cfg.get("voice", {})
    hermes_cfg = cfg.get("hermes", {})

    return {
        "voice": voice_cfg,
        "api_port": hermes_cfg.get("api_port", 9110),
    }


# ---------------------------------------------------------------------------
# Text-to-Speech (TTS)
# ---------------------------------------------------------------------------
class TTSManager:
    """Manages text-to-speech output via edge-tts CLI.

    Runs each TTS request in a background thread so it doesn't block
    the HTTP API. Uses asyncio subprocess to call the edge-tts command
    and pipe output to aplay for audio playback.
    """

    def __init__(self, tts_enabled: bool = True, simulate: bool = False) -> None:
        self._enabled = tts_enabled
        self._simulate = simulate
        self._lock = threading.Lock()
        self._speaking = False
        self._last_text: Optional[str] = None

    # -- Public properties --

    @property
    def is_speaking(self) -> bool:
        return self._speaking

    @property
    def last_text(self) -> Optional[str]:
        return self._last_text

    # -- Public API --

    def say(self, text: str, voice: str = "en-US-JennyNeural") -> None:
        """Speak *text* using the given *voice* in a background thread.

        Returns immediately — TTS runs asynchronously.
        """
        if not self._enabled and not self._simulate:
            logger.info("TTS disabled — would speak: %s", text)
            self._last_text = text
            return

        thread = threading.Thread(
            target=self._speak_thread,
            args=(text, voice),
            name="tts-speak",
            daemon=True,
        )
        thread.start()

    def wait_until_done(self, timeout: Optional[float] = None) -> bool:
        """Wait until the current TTS utterance finishes.

        Args:
            timeout: Max seconds to wait, or None to block indefinitely.

        Returns:
            True if speaking finished, False on timeout.
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        while self._speaking:
            if deadline is not None and time.monotonic() >= deadline:
                return False
            time.sleep(0.05)
        return True

    def stop(self) -> None:
        """Stop any in-progress TTS."""
        with self._lock:
            self._speaking = False

    # -- Internal --

    def _speak_thread(self, text: str, voice: str) -> None:
        """Run edge-tts CLI as a subprocess to speak *text*."""
        with self._lock:
            self._speaking = True
            self._last_text = text

        try:
            if self._simulate:
                logger.info("SIMULATE: would speak '%s' with voice '%s'", text, voice)
                # Simulate speaking duration proportional to text length
                sim_duration = max(0.5, len(text) * 0.04)
                time.sleep(sim_duration)
            else:
                logger.info("Speaking: '%s' (voice=%s)", text, voice)
                asyncio.run(self._run_edge_tts(text, voice))
        except Exception:
            logger.exception("TTS failed for text: %s", text)
        finally:
            with self._lock:
                self._speaking = False

    async def _run_edge_tts(self, text: str, voice: str) -> None:
        """Execute edge-tts CLI asynchronously and play through speaker.

        The edge-tts command outputs WAV audio to stdout; we pipe it to
        aplay for local playback.
        """
        edge_cmd = [
            "edge-tts",
            "--voice", voice,
            "--text", text,
            "--write-media", "-",
        ]
        play_cmd = ["aplay", "-r", "16000", "-f", "S16_LE", "-c", "1"]

        try:
            # Start edge-tts process
            edge_proc = await asyncio.create_subprocess_exec(
                *edge_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            if edge_proc.stdout is None:
                logger.error("edge-tts produced no stdout")
                return

            # Try playback via aplay; fall back to edge-tts --play if aplay
            # is not available (macOS, Windows, or containerized environments)
            try:
                play_proc = await asyncio.create_subprocess_exec(
                    *play_cmd,
                    stdin=edge_proc.stdout,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                _, stderr = await edge_proc.communicate()

                if edge_proc.returncode != 0:
                    error_msg = (
                        stderr.decode("utf-8", errors="replace") if stderr else ""
                    )
                    logger.error(
                        "edge-tts failed (code %d): %s",
                        edge_proc.returncode,
                        error_msg,
                    )
                    return

                await play_proc.communicate()

            except FileNotFoundError:
                # aplay not found — try edge-tts --play which uses the
                # default audio output
                logger.warning(
                    "aplay not found — falling back to edge-tts built-in playback"
                )
                # Re-start edge-tts without piping since stdout was consumed
                fallback_proc = await asyncio.create_subprocess_exec(
                    *edge_cmd,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE,
                )
                _, stderr = await fallback_proc.communicate()
                if fallback_proc.returncode != 0:
                    error_msg = (
                        stderr.decode("utf-8", errors="replace") if stderr else ""
                    )
                    logger.error(
                        "edge-tts playback failed (code %d): %s",
                        fallback_proc.returncode,
                        error_msg,
                    )

        except FileNotFoundError:
            logger.error(
                "edge-tts not found. Install with: pip install edge-tts"
            )
        except Exception:
            logger.exception("Error during TTS playback")


# ---------------------------------------------------------------------------
# Speech-to-Text (STT)
# ---------------------------------------------------------------------------
class STTManager:
    """Manages speech-to-text via sounddevice microphone capture.

    Provides a capture buffer that can be connected to Whisper, vosk,
    or any offline STT engine. Currently returns a placeholder stub
    with audio energy metadata.
    """

    def __init__(
        self,
        stt_enabled: bool = True,
        device: Optional[int] = None,
        simulate: bool = False,
    ) -> None:
        self._enabled = stt_enabled
        self._device = device
        self._simulate = simulate
        self._lock = threading.Lock()
        self.is_listening = False
        self.capture_buffer: List[np.ndarray] = []

    def start_listening(self, duration_seconds: float = 10.0) -> str:
        """Record audio from microphone for *duration_seconds*.

        Returns a placeholder transcription string. In production, this
        buffer would be sent to Whisper/vosk for actual transcription.

        Args:
            duration_seconds: How long to listen (clamped 1-60).

        Returns:
            Transcription string, or empty string on failure/silence.
        """
        if sd is None and not self._simulate:
            logger.error("sounddevice not installed — cannot capture audio")
            return ""

        if self._simulate:
            logger.info(
                "SIMULATE: would listen for %.1f seconds", duration_seconds
            )
            time.sleep(duration_seconds)
            return "simulated voice command"

        if not self._enabled:
            logger.info("STT disabled — not listening")
            return ""

        duration_seconds = max(1.0, min(60.0, float(duration_seconds)))
        logger.info(
            "Listening for %.1f seconds (device=%s)...",
            duration_seconds,
            self._device,
        )
        self.is_listening = True
        self.capture_buffer = []

        try:
            num_samples = int(SAMPLE_RATE * duration_seconds)
            recording = sd.rec(
                num_samples,
                samplerate=SAMPLE_RATE,
                channels=1,
                dtype="float32",
                device=self._device,
                blocking=True,
            )
            audio = recording.flatten()
            self.capture_buffer.append(audio)
            logger.info("Captured %d samples (%.1f s)", len(audio), len(audio) / SAMPLE_RATE)

            # TODO: Connect to Whisper/vosk for actual transcription
            return self._stub_transcribe(audio)

        except Exception:
            logger.exception("Error during audio capture")
            return ""
        finally:
            self.is_listening = False

    def _stub_transcribe(self, audio: np.ndarray) -> str:
        """Placeholder transcription — computes energy level and returns stub.

        In production, replace this with a call to:
          - Whisper (openai-whisper)
          - Vosk (vosk-api)
          - Or any other offline STT engine
        """
        energy = float(np.sqrt(np.mean(audio ** 2)))
        duration = len(audio) / SAMPLE_RATE

        if energy < 0.01:
            logger.info("Captured silence (energy=%.4f)", energy)
            return ""

        logger.info(
            "Audio captured: %.1f seconds, RMS energy=%.4f (stub transcription)",
            duration,
            energy,
        )
        return f"[audio {duration:.1f}s energy={energy:.4f}]"

    def get_captured_audio(self) -> Optional[np.ndarray]:
        """Return the concatenated capture buffer, or None if empty."""
        with self._lock:
            if not self.capture_buffer:
                return None
            return np.concatenate(self.capture_buffer)


# ---------------------------------------------------------------------------
# Bark Detection
# ---------------------------------------------------------------------------
class BarkDetector:
    """Detects dog barks using FFT-based frequency analysis.

    Captures 1-second audio buffers at 16 kHz, computes FFT power in
    the bark frequency band (100-800 Hz) vs the total analysis band
    (100-3000 Hz), and classifies a bark when:
      - Bark-band power > 40% of total band power
      - Total power exceeds a rolling background noise floor threshold

    Maintains a 60-second rolling noise floor estimate and a daily
    bark counter (resets at midnight UTC).
    """

    def __init__(
        self,
        bark_detection_enabled: bool = True,
        device: Optional[int] = None,
        simulate: bool = False,
    ) -> None:
        self._enabled = bark_detection_enabled
        self._device = device
        self._simulate = simulate
        self._running = False
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

        # Detection state
        self._lock = threading.Lock()

        # Rolling noise floor: deque of (monotonic_timestamp, noise_floor_level)
        self._noise_floor: deque = deque()

        # Bark counters
        self._daily_count: int = 0
        self._current_date: date = date.today()

        # Last detection result
        self._last_detection: Optional[Dict[str, Any]] = None

    # -- Properties --

    @property
    def daily_count(self) -> int:
        with self._lock:
            self._check_date_rollover()
            return self._daily_count

    @property
    def last_detection(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            return self._last_detection

    @property
    def is_running(self) -> bool:
        return self._running

    # -- Date rollover --

    def _check_date_rollover(self) -> None:
        """Reset daily counter if the date has changed (UTC)."""
        today = date.today()
        if today != self._current_date:
            logger.info(
                "Bark counter: %d barks on %s → reset for %s",
                self._daily_count,
                self._current_date,
                today,
            )
            self._daily_count = 0
            self._current_date = today

    # -- Background noise floor --

    def _update_noise_floor(self, total_power: float) -> None:
        """Update the rolling noise floor estimate.

        Maintains a deque of (monotonic_time, power) tuples, pruning
        entries older than *NOISE_FLOOR_WINDOW* seconds.
        """
        now = time.monotonic()
        cutoff = now - NOISE_FLOOR_WINDOW

        # Prune old entries
        while self._noise_floor and self._noise_floor[0][0] < cutoff:
            self._noise_floor.popleft()

        # Add new measurement
        self._noise_floor.append((now, total_power))

    def _get_background_threshold(self) -> float:
        """Return the current noise floor threshold.

        Computed as mean + 2 * std of the rolling window, or mean * 1.5
        if fewer than 3 samples exist.
        """
        if not self._noise_floor:
            return 0.0

        powers = [p for _, p in self._noise_floor]
        if len(powers) < 3:
            return float(np.mean(powers)) * 1.5

        mean = float(np.mean(powers))
        std = float(np.std(powers))
        return mean + 2.0 * std

    # -- FFT Analysis --

    def _analyze_buffer(self, audio: np.ndarray) -> Optional[Dict[str, Any]]:
        """Analyze a 1-second audio buffer for bark content.

        Args:
            audio: 1D numpy array of float32 samples at 16 kHz.

        Returns:
            A dict with analysis results, or None if audio is too short/silent.
        """
        if len(audio) < SAMPLE_RATE // 4:
            return None  # Too short for meaningful analysis

        # Apply Hanning window and compute FFT
        n = len(audio)
        freqs = np.fft.rfftfreq(n, d=1.0 / SAMPLE_RATE)
        fft = np.fft.rfft(audio * np.hanning(n))
        power = np.abs(fft) ** 2

        # Total power in analysis band [100-3000 Hz]
        total_mask = (freqs >= BARK_LOW_FREQ) & (freqs <= TOTAL_HIGH_FREQ)
        total_power = float(np.sum(power[total_mask]))

        if total_power < 1e-10:
            return None  # Effectively silence

        # Bark band power [100-800 Hz]
        bark_mask = (freqs >= BARK_LOW_FREQ) & (freqs <= BARK_HIGH_FREQ)
        bark_power = float(np.sum(power[bark_mask]))

        # Fraction of total power concentrated in bark band
        bark_ratio = bark_power / total_power

        # Update noise floor with total power from this buffer
        self._update_noise_floor(total_power)

        # Check against background threshold
        bg_threshold = self._get_background_threshold()
        is_loud = total_power > bg_threshold and total_power > 1.0
        is_bark = bark_ratio > BARK_POWER_THRESHOLD and is_loud

        return {
            "bark_ratio": round(bark_ratio, 4),
            "total_power": round(total_power, 2),
            "bark_power": round(bark_power, 2),
            "bg_threshold": round(bg_threshold, 2),
            "is_loud": is_loud,
            "is_bark": is_bark,
            "confidence": round(bark_ratio, 4),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    # -- Continuous detection loop --

    def _detection_loop(self) -> None:
        """Background loop: continuously capture and analyze audio."""
        if sd is None and not self._simulate:
            logger.error(
                "sounddevice not installed — bark detection unavailable. "
                "Install with: pip install sounddevice"
            )
            return

        if self._simulate:
            self._simulate_detection_loop()
            return

        if not self._enabled:
            logger.info("Bark detection disabled in config — not starting")
            return

        self._running = True
        logger.info(
            "Bark detection started (device=%s, rate=%d Hz)",
            self._device,
            SAMPLE_RATE,
        )

        samples_per_buffer = int(SAMPLE_RATE * BUFFER_SECONDS)
        ring_buffer: np.ndarray = np.array([], dtype="float32")

        def audio_callback(
            indata: np.ndarray,
            frames: int,
            time_info: Any,
            status: Any,
        ) -> None:
            """Callback for sounddevice InputStream — processes audio chunks."""
            nonlocal ring_buffer

            if status:
                logger.warning("Audio stream status: %s", status)

            # Append new audio data to ring buffer
            ring_buffer = np.concatenate([ring_buffer, indata.flatten()])

            # Process in 1-second chunks
            while len(ring_buffer) >= samples_per_buffer:
                chunk = ring_buffer[:samples_per_buffer]
                ring_buffer = ring_buffer[samples_per_buffer:]

                result = self._analyze_buffer(chunk)
                if result and result["is_bark"]:
                    with self._lock:
                        self._check_date_rollover()
                        self._daily_count += 1
                        self._last_detection = result
                        logger.info(
                            "BARK detected! ratio=%.4f power=%.2f "
                            "bg=%.2f daily_count=%d",
                            result["bark_ratio"],
                            result["total_power"],
                            result["bg_threshold"],
                            self._daily_count,
                        )

        try:
            stream = sd.InputStream(
                samplerate=SAMPLE_RATE,
                channels=1,
                dtype="float32",
                device=self._device,
                callback=audio_callback,
                blocksize=samples_per_buffer,
            )

            with stream:
                while not self._stop_event.is_set():
                    time.sleep(0.1)

        except Exception:
            logger.exception("Bark detection loop crashed unexpectedly")
        finally:
            self._running = False
            logger.info("Bark detection stopped")

    def _simulate_detection_loop(self) -> None:
        """Simulate bark detections for testing without a microphone.

        Generates fake bark events at random intervals (15-45 seconds)
        with varying confidence levels.
        """
        self._running = True
        logger.info("SIMULATE: bark detection running (fake detections)")

        rng = random.Random()
        last_fake = time.monotonic()

        while not self._stop_event.is_set():
            now = time.monotonic()

            # Generate a fake bark every 15-45 seconds
            if now - last_fake > rng.uniform(15.0, 45.0):
                confidence = rng.uniform(0.4, 0.95)
                result: Dict[str, Any] = {
                    "bark_ratio": round(confidence, 4),
                    "total_power": round(rng.uniform(10.0, 100.0), 2),
                    "bark_power": round(confidence * rng.uniform(10.0, 100.0), 2),
                    "bg_threshold": round(rng.uniform(1.0, 5.0), 2),
                    "is_loud": True,
                    "is_bark": confidence > 0.4,
                    "confidence": round(confidence, 4),
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "simulated": True,
                }

                with self._lock:
                    self._check_date_rollover()
                    self._daily_count += 1
                    self._last_detection = result

                logger.info(
                    "SIMULATE BARK: confidence=%.4f daily_count=%d",
                    confidence,
                    self._daily_count,
                )
                last_fake = now

            time.sleep(0.1)

        self._running = False

    # -- Manual listen window --

    def listen_window(self, duration_seconds: float = DEFAULT_LISTEN_SECONDS) -> int:
        """Manually trigger a *duration_seconds* listen window.

        Captures audio and analyzes it in 1-second chunks, returning
        the number of barks detected.

        Args:
            duration_seconds: Length of the listen window (clamped 1-60).

        Returns:
            Number of barks detected during the window.
        """
        if sd is None and not self._simulate:
            logger.error("sounddevice not installed — cannot listen")
            return 0

        duration_seconds = max(1.0, min(60.0, float(duration_seconds)))

        if self._simulate:
            logger.info(
                "SIMULATE: manual listen for %.1f seconds", duration_seconds
            )
            time.sleep(duration_seconds)
            rng = random.Random()
            n_barks = rng.randint(0, 3)
            if n_barks > 0:
                with self._lock:
                    self._check_date_rollover()
                    self._daily_count += n_barks
                    self._last_detection = {
                        "bark_ratio": 0.65,
                        "total_power": 45.0,
                        "bark_power": 29.25,
                        "bg_threshold": 3.0,
                        "is_loud": True,
                        "is_bark": True,
                        "confidence": 0.65,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "simulated": True,
                    }
                logger.info(
                    "SIMULATE: detected %d barks in listen window", n_barks
                )
            return n_barks

        if not self._enabled:
            logger.info("Bark detection disabled — not listening")
            return 0

        logger.info(
            "Manual listen window: %.1f seconds (device=%s)...",
            duration_seconds,
            self._device,
        )

        try:
            num_samples = int(SAMPLE_RATE * duration_seconds)
            recording = sd.rec(
                num_samples,
                samplerate=SAMPLE_RATE,
                channels=1,
                dtype="float32",
                device=self._device,
                blocking=True,
            )
            audio = recording.flatten()

            # Analyze in 1-second chunks
            barks_detected = 0
            samples_per_chunk = int(SAMPLE_RATE * BUFFER_SECONDS)

            for start in range(0, len(audio), samples_per_chunk):
                chunk = audio[start:start + samples_per_chunk]
                result = self._analyze_buffer(chunk)
                if result and result["is_bark"]:
                    barks_detected += 1
                    with self._lock:
                        self._check_date_rollover()
                        self._daily_count += 1
                        self._last_detection = result

            logger.info(
                "Listen window complete: %d barks detected", barks_detected
            )
            return barks_detected

        except Exception:
            logger.exception("Error during listen window capture/analysis")
            return 0

    # -- Lifecycle --

    def start(self) -> None:
        """Start the continuous bark detection background thread."""
        if self._running:
            logger.warning("Bark detection already running")
            return

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._detection_loop,
            name="bark-detection",
            daemon=True,
        )
        self._thread.start()
        logger.debug("Bark detection thread launched")

    def stop(self) -> None:
        """Stop bark detection."""
        logger.info("Stopping bark detection...")
        self._stop_event.set()
        self._running = False

    def get_status(self) -> Dict[str, Any]:
        """Return current bark detection status as a dict."""
        with self._lock:
            self._check_date_rollover()
            return {
                "service": "voice",
                "component": "bark_detection",
                "running": self._running,
                "daily_count": self._daily_count,
                "current_date": str(self._current_date),
                "last_detection": self._last_detection,
                "noise_floor_samples": len(self._noise_floor),
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }


# ---------------------------------------------------------------------------
# HTTP API Handler
# ---------------------------------------------------------------------------
class VoiceAPIHandler(BaseHTTPRequestHandler):
    """Serves the voice interaction HTTP API.

    Endpoints:
      GET  /voice/health       — Component health/status
      GET  /voice/bark/status  — Bark detection status + daily count
      POST /voice/say          — Speak text (TTS)
      POST /voice/bark/listen  — Manual listen window

    Class-level references set by the server:
      - ``tts: TTSManager``
      - ``stt: STTManager``
      - ``bark: BarkDetector``
    """

    tts: TTSManager = None       # type: ignore[assignment]
    stt: STTManager = None       # type: ignore[assignment]
    bark: BarkDetector = None    # type: ignore[assignment]

    # -- Route dispatch --

    def do_GET(self) -> None:
        if self.path == "/voice/health":
            self._serve_health()
        elif self.path == "/voice/bark/status":
            self._serve_bark_status()
        else:
            self._json_response({"error": "not found"}, status=404)

    def do_POST(self) -> None:
        if self.path == "/voice/say":
            self._handle_say()
        elif self.path == "/voice/bark/listen":
            self._handle_bark_listen()
        else:
            self._json_response({"error": "not found"}, status=404)

    # -- GET /voice/health --

    def _serve_health(self) -> None:
        """Return health status for the voice service components."""
        data = {
            "service": "voice",
            "status": "ok",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "components": {
                "tts": {
                    "enabled": self.tts._enabled or self.tts._simulate,
                    "speaking": self.tts.is_speaking,
                    "simulate": self.tts._simulate,
                },
                "stt": {
                    "enabled": self.stt._enabled,
                    "listening": self.stt.is_listening,
                    "simulate": self.stt._simulate,
                },
                "bark_detection": {
                    "running": self.bark.is_running,
                    "simulate": self.bark._simulate,
                },
            },
        }
        self._json_response(data)

    # -- GET /voice/bark/status --

    def _serve_bark_status(self) -> None:
        """Return bark detection status and daily count."""
        data = self.bark.get_status()
        self._json_response(data)

    # -- POST /voice/say --

    def _handle_say(self) -> None:
        """Handle TTS request: speak the provided text.

        Request body (JSON):
          {"text": "...", "voice": "en-US-JennyNeural"}

        *voice* is optional, defaults to en-US-JennyNeural.
        """
        payload = self._parse_body()
        if payload is None:
            return

        text = payload.get("text", "").strip()
        if not text:
            self._json_response(
                {"error": "Missing 'text' field in request body"},
                status=400,
            )
            return

        voice = payload.get("voice", "en-US-JennyNeural")

        # Start TTS in background (non-blocking)
        self.tts.say(text, voice)

        self._json_response({
            "status": "speaking",
            "text": text,
            "voice": voice,
            "simulate": self.tts._simulate,
        })

    # -- POST /voice/bark/listen --

    def _handle_bark_listen(self) -> None:
        """Handle manual listen window request.

        Request body (JSON, optional):
          {"duration_seconds": 10}

        *duration_seconds* is clamped to [1, 60].
        """
        payload = self._parse_body()
        if payload is None:
            duration = DEFAULT_LISTEN_SECONDS
        else:
            duration = float(payload.get("duration_seconds", DEFAULT_LISTEN_SECONDS))

        duration = max(1.0, min(60.0, duration))

        # Run listen window (blocking during capture, but POST semantics
        # allow this — the response waits for the window to complete)
        barks_detected = self.bark.listen_window(duration)

        self._json_response({
            "status": "complete",
            "duration_seconds": duration,
            "barks_detected": barks_detected,
            "daily_count": self.bark.daily_count,
            "simulate": self.bark._simulate,
        })

    # -- Helpers --

    def _parse_body(self) -> Optional[Dict[str, Any]]:
        """Parse JSON request body. Returns None on error (response sent)."""
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length) if length > 0 else b"{}"
            return json.loads(body) if body else {}
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            self._json_response(
                {"error": f"Invalid JSON in request body: {exc}"},
                status=400,
            )
            return None

    def _json_response(self, data: dict, status: int = 200) -> None:
        """Send a JSON HTTP response."""
        body = json.dumps(data, indent=2, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: Any) -> None:
        """Route HTTP log messages to the module logger."""
        logger.debug("HTTP: " + fmt % args)


# ---------------------------------------------------------------------------
# Main daemon
# ---------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(description="Dog Agent Voice Module")
    parser.add_argument(
        "--config",
        default=None,
        help="Path to config.yaml (default: ../config.yaml relative to this script)",
    )
    parser.add_argument(
        "--simulate",
        action="store_true",
        help="Simulate mode — log TTS and generate fake bark detections",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="HTTP API port (default: from config, usually 9110)",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Enable debug logging"
    )
    args = parser.parse_args()

    if args.verbose:
        logger.setLevel(logging.DEBUG)

    # --- Resolve config path ---
    if args.config:
        config_path = args.config
    else:
        script_dir = Path(__file__).resolve().parent
        config_path = str(script_dir.parent / "config.yaml")

    if os.path.exists(config_path):
        cfg = load_config(config_path)
        logger.info("Loaded config from %s", config_path)
    else:
        logger.warning(
            "No config.yaml found at %s; using defaults. "
            "Copy config.example.yaml to config.yaml and edit.",
            config_path,
        )
        cfg = {
            "voice": {
                "tts_enabled": False,
                "stt_enabled": False,
                "bark_detection": False,
                "audio_device": 0,
            },
            "api_port": 9110,
        }

    # --- Override from CLI args ---
    if args.port is not None:
        cfg["api_port"] = args.port

    voice_cfg = cfg.get("voice", {})
    api_port = cfg.get("api_port", 9110)
    audio_device = voice_cfg.get("audio_device", 0)

    # --- Initialize managers ---
    tts_enabled = voice_cfg.get("tts_enabled", False)
    stt_enabled = voice_cfg.get("stt_enabled", False)
    bark_enabled = voice_cfg.get("bark_detection", False)

    tts_mgr = TTSManager(tts_enabled=tts_enabled, simulate=args.simulate)
    stt_mgr = STTManager(
        stt_enabled=stt_enabled,
        device=audio_device if not args.simulate else None,
        simulate=args.simulate,
    )
    bark_det = BarkDetector(
        bark_detection_enabled=bark_enabled,
        device=audio_device if not args.simulate else None,
        simulate=args.simulate,
    )

    logger.info("Voice module starting (simulate=%s)", args.simulate)
    logger.info(
        "TTS=%s STT=%s Bark=%s AudioDevice=%s",
        tts_enabled or args.simulate,
        stt_enabled or args.simulate,
        bark_enabled or args.simulate,
        audio_device,
    )

    # --- Start continuous bark detection ---
    if bark_enabled or args.simulate:
        bark_det.start()

    # --- Start HTTP API server ---
    VoiceAPIHandler.tts = tts_mgr
    VoiceAPIHandler.stt = stt_mgr
    VoiceAPIHandler.bark = bark_det

    server = HTTPServer(("127.0.0.1", api_port), VoiceAPIHandler)

    try:
        server_thread = threading.Thread(
            target=server.serve_forever,
            name="voice-api",
            daemon=True,
        )
        server_thread.start()
        logger.info(
            "Voice API server listening on http://127.0.0.1:%d/voice",
            api_port,
        )
    except OSError as e:
        logger.error(
            "Failed to start HTTP server on port %d: %s", api_port, e
        )
        logger.error(
            "Port %d may be in use. Use --port to specify a different port.",
            api_port,
        )
        bark_det.stop()
        sys.exit(1)

    # --- Graceful shutdown ---
    def shutdown(signum: int, frame: Any) -> None:
        logger.info("Received signal %d — shutting down...", signum)
        bark_det.stop()
        server.shutdown()
        logger.info("Voice module stopped.")
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