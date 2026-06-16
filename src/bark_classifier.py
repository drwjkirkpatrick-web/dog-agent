#!/usr/bin/env python3
"""
Bark Classifier Module — Dog Agent
===================================
ML-based bark classification using TensorFlow Lite to identify different
types of barks: play/excitement, distress/anxiety, alert/guard, howl/bay,
and background (no bark).

Features:
  - Real-time audio classification from microphone input
  - 1-second sliding windows with FFT and MFCC preprocessing
  - TFLite inference with <500KB model
  - Confidence threshold-based classification
  - Distress alert trigger after prolonged distress detection
  - HTTP API on port 9131 for status, stats, and manual analysis

Classes Detected:
  - play: High pitch, rapid, short barks (excitement/joy)
  - distress: Whining component, prolonged duration (anxiety/pain)
  - alert: Low, sustained, rhythmic barks (territorial warning)
  - howl: Long, musical vocalizations
  - background: No bark detected (silence/ambient noise)

Usage:
    python src/bark_classifier.py                    # Normal mode
    python src/bark_classifier.py --simulate       # Rule-based simulation
    python src/bark_classifier.py --config /path/to/config.yaml
    python src/bark_classifier.py --port 9131
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import signal
import sys
import threading
import time
import wave
from collections import deque
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from enum import Enum
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import yaml

try:
    import sounddevice as sd
except ImportError:
    sd = None  # type: ignore[assignment]

try:
    import tensorflow as tf
except ImportError:
    tf = None  # type: ignore[assignment]

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logger = logging.getLogger("bark_classifier")
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
SAMPLE_RATE = 16000           # Audio capture sample rate (Hz)
WINDOW_SECONDS = 1.0          # Analysis window size (seconds)
N_MFCC = 13                   # Number of MFCC coefficients
N_FFT = 512                   # FFT window size
HOP_LENGTH = 256              # Hop length for spectrogram
MAX_FREQ = 4000               # Maximum frequency to analyze (Hz)
N_MELS = 40                   # Number of mel filter banks

# Confidence thresholds
CONFIDENCE_HIGH = 0.8         # Confident classification
CONFIDENCE_LOW = 0.5          # Uncertain, log only

# Class indices
CLASS_PLAY = 0
CLASS_DISTRESS = 1
CLASS_ALERT = 2
CLASS_HOWL = 3
CLASS_BACKGROUND = 4

CLASS_NAMES = ["play", "distress", "alert", "howl", "background"]

# Alert triggers
DEFAULT_DISTRESS_ALERT_SECONDS = 30  # Distress for >30s triggers alert

# Port
DEFAULT_API_PORT = 9131


# ---------------------------------------------------------------------------
# Data Structures
# ---------------------------------------------------------------------------
class BarkClass(Enum):
    """Enumeration of bark classification types."""
    PLAY = "play"
    DISTRESS = "distress"
    ALERT = "alert"
    HOWL = "howl"
    BACKGROUND = "background"
    UNKNOWN = "unknown"


@dataclass
class ClassificationResult:
    """Result of a bark classification."""
    bark_class: BarkClass
    confidence: float
    class_probabilities: Dict[str, float]
    features: Dict[str, Any] = field(default_factory=dict)
    timestamp: Optional[datetime] = None
    simulated: bool = False

    def __post_init__(self):
        if self.timestamp is None:
            self.timestamp = datetime.now(timezone.utc)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "bark_class": self.bark_class.value,
            "confidence": round(self.confidence, 4),
            "class_probabilities": {
                k: round(v, 4) for k, v in self.class_probabilities.items()
            },
            "features": self.features,
            "timestamp": self.timestamp.isoformat() if self.timestamp else None,
            "simulated": self.simulated,
        }


@dataclass
class DailyStats:
    """Daily classification statistics."""
    date: str
    counts: Dict[str, int] = field(default_factory=lambda: {c: 0 for c in CLASS_NAMES})
    total_classifications: int = 0
    high_confidence_count: int = 0
    distress_start_time: Optional[float] = None
    distress_duration_sec: float = 0.0

    def increment(self, bark_class: str, confidence: float) -> None:
        """Increment count for a bark class."""
        if bark_class in self.counts:
            self.counts[bark_class] += 1
        self.total_classifications += 1
        if confidence >= CONFIDENCE_HIGH:
            self.high_confidence_count += 1

    def update_distress(self, is_distress: bool, timestamp: float) -> Tuple[bool, float]:
        """
        Update distress tracking and return (should_alert, duration_seconds).
        """
        if is_distress:
            if self.distress_start_time is None:
                self.distress_start_time = timestamp
            self.distress_duration_sec = timestamp - self.distress_start_time
        else:
            self.distress_start_time = None
            self.distress_duration_sec = 0.0

        should_alert = self.distress_duration_sec >= DEFAULT_DISTRESS_ALERT_SECONDS
        return should_alert, self.distress_duration_sec

    def to_dict(self) -> Dict[str, Any]:
        return {
            "date": self.date,
            "counts": dict(self.counts),
            "total_classifications": self.total_classifications,
            "high_confidence_count": self.high_confidence_count,
            "distress_duration_sec": round(self.distress_duration_sec, 2),
        }


# ---------------------------------------------------------------------------
# Audio Preprocessing
# ---------------------------------------------------------------------------
class AudioPreprocessor:
    """Preprocesses audio for ML classification."""

    def __init__(
        self,
        sample_rate: int = SAMPLE_RATE,
        n_mfcc: int = N_MFCC,
        n_fft: int = N_FFT,
        hop_length: int = HOP_LENGTH,
        n_mels: int = N_MELS,
        max_freq: int = MAX_FREQ,
    ) -> None:
        self.sample_rate = sample_rate
        self.n_mfcc = n_mfcc
        self.n_fft = n_fft
        self.hop_length = hop_length
        self.n_mels = n_mels
        self.max_freq = max_freq

    def preprocess(self, audio: np.ndarray) -> np.ndarray:
        """
        Preprocess audio into MFCC features.

        Args:
            audio: 1D numpy array of float32 samples.

        Returns:
            MFCC features as numpy array of shape (n_mfcc, time_frames).
        """
        # Normalize amplitude
        audio = self._normalize(audio)

        # Compute spectrogram
        spectrogram = self._compute_spectrogram(audio)

        # Apply mel filter bank
        mel_spec = self._apply_mel_filter(spectrogram)

        # Compute MFCC
        mfcc = self._compute_mfcc(mel_spec)

        return mfcc

    def _normalize(self, audio: np.ndarray) -> np.ndarray:
        """Normalize audio to [-1, 1] range."""
        max_val = np.max(np.abs(audio))
        if max_val > 0:
            audio = audio / max_val
        return audio

    def _compute_spectrogram(self, audio: np.ndarray) -> np.ndarray:
        """Compute magnitude spectrogram."""
        # Pad audio if needed
        if len(audio) < self.n_fft:
            audio = np.pad(audio, (0, self.n_fft - len(audio)))

        # Compute STFT
        hop = self.hop_length
        n_frames = 1 + (len(audio) - self.n_fft) // hop

        spec = np.zeros((self.n_fft // 2 + 1, n_frames), dtype=np.float32)

        for i in range(n_frames):
            start = i * hop
            end = start + self.n_fft
            if end > len(audio):
                break
            frame = audio[start:end] * np.hanning(self.n_fft)
            fft = np.fft.rfft(frame)
            spec[:, i] = np.abs(fft)

        return spec

    def _hz_to_mel(self, hz: float) -> float:
        """Convert Hz to mel scale."""
        return 2595 * np.log10(1 + hz / 700)

    def _mel_to_hz(self, mel: float) -> float:
        """Convert mel scale to Hz."""
        return 700 * (10 ** (mel / 2595) - 1)

    def _create_mel_filter_bank(self, n_fft: int, n_mels: int) -> np.ndarray:
        """Create mel filter bank matrix."""
        min_mel = self._hz_to_mel(0)
        max_mel = self._hz_to_mel(self.max_freq)
        mel_points = np.linspace(min_mel, max_mel, n_mels + 2)
        hz_points = self._mel_to_hz(mel_points)

        # FFT bin frequencies
        fft_freqs = np.linspace(0, self.sample_rate / 2, n_fft // 2 + 1)

        # Create filter bank
        filter_bank = np.zeros((n_mels, n_fft // 2 + 1))

        for i in range(n_mels):
            # Filter triangle: left, center, right
            left = hz_points[i]
            center = hz_points[i + 1]
            right = hz_points[i + 2]

            for j, freq in enumerate(fft_freqs):
                if freq <= left or freq >= right:
                    filter_bank[i, j] = 0
                elif freq <= center:
                    filter_bank[i, j] = (freq - left) / (center - left)
                else:
                    filter_bank[i, j] = (right - freq) / (right - center)

        return filter_bank

    def _apply_mel_filter(self, spectrogram: np.ndarray) -> np.ndarray:
        """Apply mel filter bank to spectrogram."""
        filter_bank = self._create_mel_filter_bank(
            self.n_fft, self.n_mels
        )
        mel_spec = np.dot(filter_bank, spectrogram)
        # Prevent log of zero
        mel_spec = np.maximum(mel_spec, 1e-10)
        return mel_spec

    def _compute_mfcc(self, mel_spec: np.ndarray) -> np.ndarray:
        """Compute MFCC from mel spectrogram using DCT."""
        log_mel = np.log(mel_spec)

        # DCT-II
        n_mels, n_frames = log_mel.shape
        mfcc = np.zeros((self.n_mfcc, n_frames))

        for i in range(self.n_mfcc):
            for j in range(n_mels):
                mfcc[i, :] += log_mel[j, :] * np.cos(
                    np.pi * i * (j + 0.5) / n_mels
                )

        return mfcc

    def get_feature_vector(self, mfcc: np.ndarray) -> np.ndarray:
        """
        Convert MFCC to feature vector for model input.
        Flattens and normalizes the MFCC matrix.
        """
        # Take mean across time dimension for a compact representation
        # Or flatten for full temporal features
        if mfcc.shape[1] > 0:
            # Mean pooling across time
            features = np.mean(mfcc, axis=1)
            # Also add std for temporal variance
            features = np.concatenate([features, np.std(mfcc, axis=1)])
            # Normalize
            features = (features - np.mean(features)) / (np.std(features) + 1e-10)
        else:
            features = np.zeros(self.n_mfcc * 2)

        return features.astype(np.float32)


# ---------------------------------------------------------------------------
# TFLite Model Inference
# ---------------------------------------------------------------------------
class TFLiteBarkClassifier:
    """TensorFlow Lite bark classification model."""

    def __init__(self, model_path: str, preprocessor: AudioPreprocessor) -> None:
        self.model_path = Path(model_path)
        self.preprocessor = preprocessor
        self.interpreter: Optional[Any] = None
        self.input_details: Optional[Any] = None
        self.output_details: Optional[Any] = None
        self._lock = threading.Lock()

        if tf is not None and self.model_path.exists():
            self._load_model()
        else:
            if tf is None:
                logger.warning("TensorFlow not installed — using rule-based fallback")
            elif not self.model_path.exists():
                logger.warning(f"Model not found at {model_path} — using rule-based fallback")

    def _load_model(self) -> None:
        """Load the TFLite model."""
        try:
            self.interpreter = tf.lite.Interpreter(model_path=str(self.model_path))
            self.interpreter.allocate_tensors()
            self.input_details = self.interpreter.get_input_details()
            self.output_details = self.interpreter.get_output_details()
            logger.info(f"Loaded TFLite model from {self.model_path}")
        except Exception as exc:
            logger.error(f"Failed to load TFLite model: {exc}")
            self.interpreter = None

    def is_loaded(self) -> bool:
        """Check if model is loaded."""
        return self.interpreter is not None

    def classify(self, audio: np.ndarray) -> ClassificationResult:
        """
        Classify audio using TFLite model.

        Args:
            audio: Audio samples.

        Returns:
            ClassificationResult with class and confidence.
        """
        with self._lock:
            if self.interpreter is None:
                return self._rule_based_classify(audio)

            try:
                # Preprocess audio
                mfcc = self.preprocessor.preprocess(audio)
                features = self.preprocessor.get_feature_vector(mfcc)

                # Reshape for model input
                input_shape = self.input_details[0]["shape"]
                if len(features) != input_shape[1]:
                    # Pad or truncate
                    if len(features) < input_shape[1]:
                        features = np.pad(features, (0, input_shape[1] - len(features)))
                    else:
                        features = features[:input_shape[1]]

                input_data = np.expand_dims(features, axis=0).astype(np.float32)

                # Run inference
                self.interpreter.set_tensor(self.input_details[0]["index"], input_data)
                self.interpreter.invoke()
                output = self.interpreter.get_tensor(self.output_details[0]["index"])

                # Parse output
                probs = output[0]
                class_idx = int(np.argmax(probs))
                confidence = float(probs[class_idx])

                class_probabilities = {
                    CLASS_NAMES[i]: float(probs[i]) for i in range(len(CLASS_NAMES))
                }

                bark_class = BarkClass(CLASS_NAMES[class_idx])

                return ClassificationResult(
                    bark_class=bark_class,
                    confidence=confidence,
                    class_probabilities=class_probabilities,
                    features={
                        "mfcc_mean": float(np.mean(mfcc)),
                        "mfcc_std": float(np.std(mfcc)),
                    },
                )

            except Exception as exc:
                logger.error(f"TFLite inference failed: {exc}")
                return self._rule_based_classify(audio)

    def _rule_based_classify(self, audio: np.ndarray) -> ClassificationResult:
        """
        Rule-based classification fallback using frequency analysis.

        Heuristics:
        - play: High pitch (400-800 Hz), short bursts, rapid energy spikes
        - distress: High pitch with whine component (>1000 Hz), longer duration
        - alert: Lower pitch (100-400 Hz), sustained, rhythmic
        - howl: Low pitch, sustained, high harmonic content
        - background: Low energy, no distinct bark pattern
        """
        # Compute FFT
        audio = self.preprocessor._normalize(audio)
        n = min(len(audio), self.preprocessor.sample_rate)
        if n < 256:
            return ClassificationResult(
                bark_class=BarkClass.BACKGROUND,
                confidence=0.5,
                class_probabilities={c: 0.2 for c in CLASS_NAMES},
            )

        audio = audio[:n]
        fft = np.fft.rfft(audio * np.hanning(n))
        power = np.abs(fft) ** 2
        freqs = np.fft.rfftfreq(n, d=1.0 / self.preprocessor.sample_rate)

        # Frequency bands
        total_power = np.sum(power) + 1e-10

        play_band = np.sum(power[(freqs >= 400) & (freqs < 800)]) / total_power
        alert_band = np.sum(power[(freqs >= 100) & (freqs < 400)]) / total_power
        distress_band = np.sum(power[(freqs >= 600) & (freqs < 1500)]) / total_power
        howl_band = np.sum(power[(freqs >= 50) & (freqs < 300)]) / total_power
        high_freq = np.sum(power[freqs >= 1000]) / total_power

        # Calculate spectral centroid (brightness)
        centroid = np.sum(freqs * power) / total_power

        # Calculate zero-crossing rate (roughness)
        zcr = np.sum(np.diff(np.sign(audio)) != 0) / len(audio)

        # Energy features
        rms = np.sqrt(np.mean(audio ** 2))
        peak_to_rms = np.max(np.abs(audio)) / (rms + 1e-10)

        # Decision logic
        probs = np.zeros(5)

        # Play: high pitch, rapid (high ZCR), high energy spikes
        play_score = play_band * 2.0 + zcr * 0.5 + (peak_to_rms > 3) * 0.3
        probs[CLASS_PLAY] = min(play_score, 1.0)

        # Alert: low pitch, sustained, rhythmic
        alert_score = alert_band * 2.0 + (centroid < 300) * 0.5 - zcr * 0.3
        probs[CLASS_ALERT] = min(alert_score, 1.0)

        # Distress: high pitch, whine component, longer duration (implied by lower ZCR)
        distress_score = distress_band * 1.5 + high_freq * 0.5 - zcr * 0.2
        probs[CLASS_DISTRESS] = min(distress_score, 1.0)

        # Howl: very low, sustained, harmonic
        howl_score = howl_band * 1.5 + (centroid < 200) * 0.5 - zcr * 0.5
        probs[CLASS_HOWL] = min(howl_score, 1.0)

        # Background: low energy, no clear pattern
        if rms < 0.05:
            background_score = 1.0
        else:
            background_score = max(0, 1.0 - np.max(probs[:4]))
        probs[CLASS_BACKGROUND] = background_score

        # Normalize probabilities
        probs = probs / (np.sum(probs) + 1e-10)

        class_idx = int(np.argmax(probs))
        confidence = float(probs[class_idx])

        class_probabilities = {
            CLASS_NAMES[i]: float(probs[i]) for i in range(len(CLASS_NAMES))
        }

        bark_class = BarkClass(CLASS_NAMES[class_idx])

        return ClassificationResult(
            bark_class=bark_class,
            confidence=confidence,
            class_probabilities=class_probabilities,
            features={
                "spectral_centroid": float(centroid),
                "zero_crossing_rate": float(zcr),
                "rms_energy": float(rms),
                "play_band": float(play_band),
                "alert_band": float(alert_band),
                "distress_band": float(distress_band),
            },
        )


# ---------------------------------------------------------------------------
# Bark Classifier Service
# ---------------------------------------------------------------------------
class BarkClassifierService:
    """
    Main bark classification service that manages:
    - Real-time audio capture and classification
    - Classification history and statistics
    - Alert triggers for prolonged distress
    - HTTP API server
    """

    def __init__(
        self,
        config: Dict[str, Any],
        simulate: bool = False,
        device: Optional[int] = None,
    ) -> None:
        self.config = config
        self.simulate = simulate
        self.device = device

        # Load configuration
        bc_cfg = config.get("bark_classifier", {})
        self.enabled = bc_cfg.get("enabled", True)
        model_path = bc_cfg.get("model_path", "models/bark_classifier.tflite")
        self.confidence_threshold = bc_cfg.get("confidence_threshold", 0.7)
        self.distress_alert_duration = bc_cfg.get(
            "distress_alert_duration_sec", DEFAULT_DISTRESS_ALERT_SECONDS
        )

        # Initialize components
        self.preprocessor = AudioPreprocessor()
        self.classifier = TFLiteBarkClassifier(model_path, self.preprocessor)

        # State
        self._running = False
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

        # Classification history (ring buffer of last N classifications)
        self.history: deque = deque(maxlen=1000)

        # Daily statistics
        self._daily_stats: Dict[str, DailyStats] = {}
        self._current_date = str(date.today())
        self._current_stats = DailyStats(date=self._current_date)
        self._daily_stats[self._current_date] = self._current_stats

        # Distress tracking
        self._distress_start: Optional[float] = None

        # HTTP server
        self._server: Optional[HTTPServer] = None
        self._server_thread: Optional[threading.Thread] = None
        self.api_port = bc_cfg.get("api_port", DEFAULT_API_PORT)

        # Alert callback (set externally)
        self.on_distress_alert: Optional[callable] = None
        self.on_alert_bark: Optional[callable] = None

        logger.info(
            f"BarkClassifier initialized: enabled={self.enabled}, "
            f"model_loaded={self.classifier.is_loaded()}, simulate={simulate}"
        )

    def start(self) -> None:
        """Start the classification service."""
        if self._running:
            logger.warning("Bark classifier already running")
            return

        if not self.enabled:
            logger.info("Bark classifier disabled in config")
            return

        self._stop_event.clear()
        self._running = True

        # Start classification thread
        self._thread = threading.Thread(
            target=self._classification_loop,
            name="bark-classifier",
            daemon=True,
        )
        self._thread.start()

        # Start HTTP server
        self._start_http_server()

        logger.info("Bark classifier service started")

    def stop(self) -> None:
        """Stop the classification service."""
        logger.info("Stopping bark classifier...")
        self._stop_event.set()
        self._running = False

        if self._thread:
            self._thread.join(timeout=5.0)

        if self._server:
            self._server.shutdown()

        logger.info("Bark classifier stopped")

    def _classification_loop(self) -> None:
        """Main classification loop."""
        if self.simulate:
            self._simulate_classification_loop()
        else:
            self._real_classification_loop()

    def _real_classification_loop(self) -> None:
        """Real audio capture and classification."""
        if sd is None:
            logger.error("sounddevice not installed — cannot capture audio")
            return

        samples_per_buffer = int(SAMPLE_RATE * WINDOW_SECONDS)
        ring_buffer: np.ndarray = np.array([], dtype="float32")

        def audio_callback(indata: np.ndarray, frames: int, time_info: Any, status: Any) -> None:
            """Audio capture callback."""
            nonlocal ring_buffer

            if status:
                logger.warning(f"Audio stream status: {status}")

            ring_buffer = np.concatenate([ring_buffer, indata.flatten()])

            # Process in 1-second chunks
            while len(ring_buffer) >= samples_per_buffer:
                chunk = ring_buffer[:samples_per_buffer]
                ring_buffer = ring_buffer[samples_per_buffer:]
                self._process_audio_chunk(chunk)

        try:
            stream = sd.InputStream(
                samplerate=SAMPLE_RATE,
                channels=1,
                dtype="float32",
                device=self.device,
                callback=audio_callback,
                blocksize=samples_per_buffer,
            )

            with stream:
                logger.info(f"Audio stream started on device={self.device}")
                while not self._stop_event.is_set():
                    time.sleep(0.1)

        except Exception as exc:
            logger.exception(f"Classification loop crashed: {exc}")
        finally:
            self._running = False

    def _simulate_classification_loop(self) -> None:
        """Simulate classification with random results."""
        logger.info("SIMULATE: bark classification running")

        rng = random.Random()
        last_class_time = time.monotonic()

        # Simulate different bark patterns
        patterns = [
            (BarkClass.PLAY, 5.0, 15.0),      # Play every 5-15s
            (BarkClass.ALERT, 20.0, 60.0),    # Alert every 20-60s
            (BarkClass.DISTRESS, 30.0, 120.0), # Distress rarely
            (BarkClass.HOWL, 60.0, 180.0),     # Howl rarely
        ]

        next_patterns = {p[0]: time.monotonic() + rng.uniform(5, 15) for p in patterns}

        while not self._stop_event.is_set():
            now = time.monotonic()

            for bark_class, min_interval, max_interval in patterns:
                if now >= next_patterns[bark_class]:
                    # Generate classification
                    confidence = rng.uniform(0.7, 0.95)

                    # Create simulated result
                    probs = {c: 0.05 for c in CLASS_NAMES}
                    probs[bark_class.value] = confidence
                    probs[BarkClass.BACKGROUND.value] = 1.0 - confidence

                    result = ClassificationResult(
                        bark_class=bark_class,
                        confidence=confidence,
                        class_probabilities=probs,
                        features={"simulated": True},
                        simulated=True,
                    )

                    self._handle_classification(result)
                    next_patterns[bark_class] = now + rng.uniform(min_interval, max_interval)

            time.sleep(0.5)

        self._running = False

    def _process_audio_chunk(self, audio: np.ndarray) -> None:
        """Process a single audio chunk."""
        result = self.classifier.classify(audio)
        self._handle_classification(result)

    def _handle_classification(self, result: ClassificationResult) -> None:
        """Handle classification result."""
        now = time.time()

        with self._lock:
            # Check date rollover
            self._check_date_rollover()

            # Store in history
            self.history.append(result)

            # Log all classifications
            self._log_classification(result)

            # Update statistics based on confidence threshold
            if result.confidence >= self.confidence_threshold:
                self._current_stats.increment(
                    result.bark_class.value, result.confidence
                )

                # Handle distress alert
                if result.bark_class == BarkClass.DISTRESS:
                    should_alert, duration = self._current_stats.update_distress(True, now)
                    if should_alert and self.on_distress_alert:
                        self.on_distress_alert(duration, result)
                else:
                    self._current_stats.update_distress(False, now)

                    # Handle alert bark (for daily summary)
                    if result.bark_class == BarkClass.ALERT and self.on_alert_bark:
                        self.on_alert_bark(result)

            elif result.confidence >= CONFIDENCE_LOW:
                # Uncertain - log only
                logger.debug(
                    f"Uncertain classification: {result.bark_class.value} "
                    f"({result.confidence:.2f})"
                )

    def _check_date_rollover(self) -> None:
        """Check for date change and rotate stats."""
        today = str(date.today())
        if today != self._current_date:
            logger.info(f"Date rollover: {self._current_date} -> {today}")
            self._current_date = today
            self._current_stats = DailyStats(date=today)
            self._daily_stats[today] = self._current_stats

    def _log_classification(self, result: ClassificationResult) -> None:
        """Log classification to file."""
        log_entry = {
            "timestamp": result.timestamp.isoformat() if result.timestamp else None,
            "class": result.bark_class.value,
            "confidence": round(result.confidence, 4),
            "probabilities": result.class_probabilities,
            "features": result.features,
            "simulated": result.simulated,
        }

        # Write to daily log file
        log_dir = Path("data/bark_classifications")
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / f"bark_classifications_{self._current_date}.log"

        try:
            with open(log_file, "a") as f:
                f.write(json.dumps(log_entry) + "\n")
        except Exception as exc:
            logger.error(f"Failed to write classification log: {exc}")

        # Also log high-confidence classifications
        if result.confidence >= CONFIDENCE_HIGH:
            logger.info(
                f"Bark classified: {result.bark_class.value.upper()} "
                f"({result.confidence:.2%})"
            )

    def _start_http_server(self) -> None:
        """Start HTTP API server."""
        server_class = type("BarkClassifierHTTPServer", (HTTPServer,), {})
        self._server = server_class(("0.0.0.0", self.api_port), BarkClassifierHTTPHandler)
        self._server.service = self  # type: ignore[attr-defined]

        self._server_thread = threading.Thread(
            target=self._server.serve_forever,
            name="bark-classifier-http",
            daemon=True,
        )
        self._server_thread.start()
        logger.info(f"HTTP API server started on port {self.api_port}")

    # -----------------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------------

    def analyze_audio_file(self, file_path: str) -> ClassificationResult:
        """
        Analyze a WAV audio file and return classification.

        Args:
            file_path: Path to WAV file.

        Returns:
            ClassificationResult.
        """
        try:
            with wave.open(file_path, "rb") as wf:
                n_frames = wf.getnframes()
                audio_data = wf.readframes(n_frames)
                sample_width = wf.getsampwidth()
                sample_rate = wf.getframerate()

            # Convert to float32 numpy array
            if sample_width == 2:
                audio = np.frombuffer(audio_data, dtype=np.int16).astype(np.float32)
            elif sample_width == 4:
                audio = np.frombuffer(audio_data, dtype=np.int32).astype(np.float32)
            else:
                audio = np.frombuffer(audio_data, dtype=np.uint8).astype(np.float32)

            # Normalize
            audio = audio / np.max(np.abs(audio))

            # Resample if needed
            if sample_rate != SAMPLE_RATE:
                # Simple resampling
                ratio = SAMPLE_RATE / sample_rate
                new_len = int(len(audio) * ratio)
                indices = np.linspace(0, len(audio) - 1, new_len)
                audio = np.interp(indices, np.arange(len(audio)), audio)

            # Use 1-second windows
            samples_per_window = int(SAMPLE_RATE * WINDOW_SECONDS)
            results = []

            for start in range(0, len(audio), samples_per_window):
                chunk = audio[start:start + samples_per_window]
                if len(chunk) < samples_per_window // 2:
                    continue
                result = self.classifier.classify(chunk)
                results.append(result)

            if not results:
                return ClassificationResult(
                    bark_class=BarkClass.BACKGROUND,
                    confidence=0.5,
                    class_probabilities={c: 0.2 for c in CLASS_NAMES},
                )

            # Aggregate results (vote)
            class_votes = {c: 0 for c in CLASS_NAMES}
            for r in results:
                if r.confidence >= self.confidence_threshold:
                    class_votes[r.bark_class.value] += r.confidence

            winner = max(class_votes, key=class_votes.get)
            total_votes = sum(class_votes.values())
            confidence = class_votes[winner] / (total_votes + 1e-10)

            # Average probabilities
            avg_probs = {}
            for c in CLASS_NAMES:
                avg_probs[c] = sum(r.class_probabilities.get(c, 0) for r in results) / len(results)

            return ClassificationResult(
                bark_class=BarkClass(winner),
                confidence=confidence,
                class_probabilities=avg_probs,
                features={"num_windows": len(results)},
            )

        except Exception as exc:
            logger.error(f"Failed to analyze audio file: {exc}")
            raise

    def get_recent_classifications(self, count: int = 50) -> List[Dict[str, Any]]:
        """Get recent classification results."""
        with self._lock:
            return [r.to_dict() for r in list(self.history)[-count:]]

    def get_stats(self) -> Dict[str, Any]:
        """Get classification statistics."""
        with self._lock:
            self._check_date_rollover()
            return self._current_stats.to_dict()

    def get_daily_stats(self, date_str: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Get statistics for a specific date."""
        with self._lock:
            if date_str is None:
                date_str = self._current_date
            stats = self._daily_stats.get(date_str)
            return stats.to_dict() if stats else None

    def get_health(self) -> Dict[str, Any]:
        """Get service health status."""
        return {
            "service": "bark_classifier",
            "status": "ok" if self._running else "stopped",
            "running": self._running,
            "enabled": self.enabled,
            "model_loaded": self.classifier.is_loaded(),
            "api_port": self.api_port,
            "confidence_threshold": self.confidence_threshold,
            "history_size": len(self.history),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }


# ---------------------------------------------------------------------------
# HTTP API Handler
# ---------------------------------------------------------------------------
class BarkClassifierHTTPHandler(BaseHTTPRequestHandler):
    """HTTP API handler for bark classifier."""

    def _get_service(self) -> Optional[BarkClassifierService]:
        """Get service reference from server."""
        server = getattr(self, "server", None)
        return getattr(server, "service", None) if server else None

    def do_GET(self) -> None:
        """Handle GET requests."""
        if self.path == "/bark/health":
            self._handle_health()
        elif self.path == "/bark/status":
            self._handle_status()
        elif self.path == "/bark/stats":
            self._handle_stats()
        else:
            self._send_json({"error": "not_found"}, 404)

    def do_POST(self) -> None:
        """Handle POST requests."""
        if self.path == "/bark/analyze":
            self._handle_analyze()
        else:
            self._send_json({"error": "not_found"}, 404)

    def _handle_health(self) -> None:
        """GET /bark/health - Service health check."""
        service = self._get_service()
        if service:
            self._send_json(service.get_health())
        else:
            self._send_json({"error": "service_unavailable"}, 503)

    def _handle_status(self) -> None:
        """GET /bark/status - Recent classifications and confidence scores."""
        service = self._get_service()
        if service:
            recent = service.get_recent_classifications(50)
            self._send_json({
                "recent_classifications": recent,
                "count": len(recent),
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })
        else:
            self._send_json({"error": "service_unavailable"}, 503)

    def _handle_stats(self) -> None:
        """GET /bark/stats - Daily counts by type."""
        service = self._get_service()
        if service:
            stats = service.get_stats()
            self._send_json({
                "current_stats": stats,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })
        else:
            self._send_json({"error": "service_unavailable"}, 503)

    def _handle_analyze(self) -> None:
        """POST /bark/analyze - Analyze audio file."""
        service = self._get_service()
        if not service:
            self._send_json({"error": "service_unavailable"}, 503)
            return

        body = self._read_body()
        if body is None:
            self._send_json({"error": "invalid_json"}, 400)
            return

        file_path = body.get("file_path")
        if not file_path:
            self._send_json({"error": "missing_file_path"}, 400)
            return

        try:
            result = service.analyze_audio_file(file_path)
            self._send_json({
                "result": result.to_dict(),
                "file_path": file_path,
            })
        except Exception as exc:
            logger.error(f"Analyze failed: {exc}")
            self._send_json({"error": "analysis_failed", "detail": str(exc)}, 500)

    def _read_body(self) -> Optional[Dict[str, Any]]:
        """Read and parse JSON request body."""
        try:
            length = int(self.headers.get("Content-Length", 0))
            if length == 0:
                return {}
            body = self.rfile.read(length)
            return json.loads(body)
        except (json.JSONDecodeError, ValueError):
            return None

    def _send_json(self, data: Dict[str, Any], status: int = 200) -> None:
        """Send JSON response."""
        body = json.dumps(data, indent=2, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: Any) -> None:
        """Log HTTP requests."""
        logger.debug(f"HTTP: {fmt % args}")


# ---------------------------------------------------------------------------
# Configuration Loader
# ---------------------------------------------------------------------------
def load_config(path: Optional[str] = None) -> Dict[str, Any]:
    """Load configuration from YAML file."""
    project_dir = Path(__file__).resolve().parent.parent

    if path:
        config_path = Path(path)
    else:
        config_path = project_dir / "config.yaml"

    if config_path.exists():
        with open(config_path) as f:
            return yaml.safe_load(f) or {}
    else:
        logger.warning(f"Config file not found: {config_path}")
        return {}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(description="Bark Classifier Module")
    parser.add_argument(
        "--config",
        default=None,
        help="Path to config.yaml (default: ../config.yaml)",
    )
    parser.add_argument(
        "--simulate",
        action="store_true",
        help="Simulate mode — rule-based classification without microphone",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help=f"HTTP API port (default: {DEFAULT_API_PORT})",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable debug logging",
    )
    args = parser.parse_args()

    if args.verbose:
        logger.setLevel(logging.DEBUG)

    # Load configuration
    config = load_config(args.config)

    # Override port if specified
    if args.port:
        if "bark_classifier" not in config:
            config["bark_classifier"] = {}
        config["bark_classifier"]["api_port"] = args.port

    # Create and start service
    service = BarkClassifierService(
        config=config,
        simulate=args.simulate,
    )

    # Set up alert callbacks
    def on_distress(duration: float, result: ClassificationResult) -> None:
        logger.warning(f"🚨 DISTRESS ALERT: {duration:.0f}s of distress detected!")
        # Could integrate with alert_manager here

    def on_alert(result: ClassificationResult) -> None:
        logger.info(f"📢 Alert bark noted for daily summary")

    service.on_distress_alert = on_distress
    service.on_alert_bark = on_alert

    # Set up signal handlers
    def signal_handler(sig: int, frame: Any) -> None:
        logger.info("Shutting down...")
        service.stop()
        sys.exit(0)

    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    # Start service
    service.start()

    # Keep main thread alive
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        service.stop()


if __name__ == "__main__":
    main()
