#!/usr/bin/env python3
"""
Bark Classifier Training Data Generator
=========================================
Generates synthetic audio training data for bark classification.
Creates WAV files with different bark types: play, distress, alert, howl.

Usage:
    python scripts/generate_bark_training_data.py
    python scripts/generate_bark_training_data.py --samples 500 --output data/training
    python scripts/generate_bark_training_data.py --visualize

Output:
    Creates a directory structure:
        output_dir/
        ├── play/
        │   ├── play_001.wav
        │   └── ...
        ├── distress/
        ├── alert/
        ├── howl/
        └── background/

Each generated file is accompanied by a JSON metadata file.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import wave
from pathlib import Path
from typing import Any, Dict, List, Tuple

import numpy as np

# Audio parameters
SAMPLE_RATE = 16000
DURATION_MIN = 0.5  # Minimum duration in seconds
DURATION_MAX = 2.0  # Maximum duration in seconds


def generate_play_bark(
    duration: float = 1.0,
    sample_rate: int = SAMPLE_RATE,
    pitch_variation: float = 0.2,
) -> np.ndarray:
    """
    Generate a play/excitement bark.
    Characteristics:
    - High pitch (400-800 Hz base)
    - Short duration (0.1-0.3s)
    - Rapid repetition
    - High energy spikes
    """
    t = np.linspace(0, duration, int(sample_rate * duration))
    
    # Base frequency around 600 Hz with variation
    base_freq = np.random.uniform(400, 800)
    freq_mod = base_freq + base_freq * pitch_variation * np.sin(2 * np.pi * 10 * t)
    
    # Create rapid bark bursts
    burst_rate = np.random.uniform(3, 6)  # bursts per second
    burst_env = np.maximum(0, np.sin(2 * np.pi * burst_rate * t) ** 20)
    
    # Generate tone with frequency modulation
    phase = np.cumsum(2 * np.pi * freq_mod / sample_rate)
    signal = np.sin(phase) * burst_env
    
    # Add harmonics for richness
    signal += 0.3 * np.sin(2 * phase) * burst_env
    signal += 0.1 * np.sin(3 * phase) * burst_env
    
    # Add some noise
    noise = np.random.normal(0, 0.05, len(signal))
    signal += noise
    
    return signal.astype(np.float32)


def generate_distress_bark(
    duration: float = 1.5,
    sample_rate: int = SAMPLE_RATE,
    whine_component: float = 0.5,
) -> np.ndarray:
    """
    Generate a distress/anxiety bark.
    Characteristics:
    - Whining component (800-1500 Hz)
    - Longer duration
    - Rising pitch pattern
    - Tremolo effect
    """
    t = np.linspace(0, duration, int(sample_rate * duration))
    
    # Base whine frequency
    base_freq = np.random.uniform(600, 1000)
    
    # Rising pitch characteristic of whining
    pitch_rise = base_freq * (1 + 0.3 * t / duration)
    
    # Tremolo effect (vibrato)
    tremolo_freq = np.random.uniform(5, 10)
    tremolo = 1 + 0.3 * np.sin(2 * np.pi * tremolo_freq * t)
    
    # Generate signal
    phase = np.cumsum(2 * np.pi * pitch_rise / sample_rate)
    signal = np.sin(phase) * tremolo
    
    # Add higher frequency whine component
    if whine_component > 0:
        whine_freq = base_freq * 1.5
        whine_phase = np.cumsum(2 * np.pi * whine_freq / sample_rate)
        signal += whine_component * np.sin(whine_phase) * tremolo
    
    # Envelope - starts and ends softer
    envelope = np.sin(np.pi * t / duration) ** 0.5
    signal *= envelope
    
    # Add breath noise
    breath = np.random.normal(0, 0.1, len(signal))
    signal += breath
    
    return signal.astype(np.float32)


def generate_alert_bark(
    duration: float = 1.0,
    sample_rate: int = SAMPLE_RATE,
    rhythmic: bool = True,
) -> np.ndarray:
    """
    Generate an alert/guard bark.
    Characteristics:
    - Lower pitch (150-350 Hz)
    - Sustained
    - Rhythmic pattern
    - Deep, authoritative
    """
    t = np.linspace(0, duration, int(sample_rate * duration))
    
    # Low fundamental frequency
    base_freq = np.random.uniform(150, 350)
    
    # Rhythmic barking pattern
    if rhythmic:
        bark_rate = np.random.uniform(1.5, 3.0)  # slower than play
        envelope = np.maximum(0, np.sin(2 * np.pi * bark_rate * t) ** 8)
    else:
        envelope = np.ones_like(t)
    
    # Generate signal with strong harmonics
    phase = np.cumsum(2 * np.pi * base_freq / sample_rate)
    signal = 0.6 * np.sin(phase) * envelope
    
    # Add strong harmonics for depth
    signal += 0.3 * np.sin(2 * phase) * envelope
    signal += 0.2 * np.sin(3 * phase) * envelope
    signal += 0.1 * np.sin(4 * phase) * envelope
    
    # Add growl component (subharmonic)
    growl_freq = base_freq / 2
    growl_phase = np.cumsum(2 * np.pi * growl_freq / sample_rate)
    signal += 0.15 * np.sin(growl_phase) * envelope
    
    # Add some low-frequency noise
    noise = np.random.normal(0, 0.05, len(signal))
    signal += noise
    
    return signal.astype(np.float32)


def generate_howl(
    duration: float = 2.0,
    sample_rate: int = SAMPLE_RATE,
    musical: bool = True,
) -> np.ndarray:
    """
    Generate a howl/bay vocalization.
    Characteristics:
    - Long duration
    - Low pitch (80-200 Hz)
    - Musical quality (multiple harmonics)
    - Gradual pitch modulation
    """
    t = np.linspace(0, duration, int(sample_rate * duration))
    
    # Fundamental frequency
    base_freq = np.random.uniform(80, 200)
    
    # Slow pitch modulation (like a wolf howl)
    if musical:
        mod_freq = np.random.uniform(0.5, 2.0)
        freq_mod = base_freq * (1 + 0.2 * np.sin(2 * np.pi * mod_freq * t))
    else:
        freq_mod = np.full_like(t, base_freq)
    
    # Generate phase
    phase = np.cumsum(2 * np.pi * freq_mod / sample_rate)
    signal = np.sin(phase)
    
    # Add rich harmonics for musical quality
    for harmonic in range(2, 8):
        amplitude = 1.0 / harmonic
        signal += amplitude * np.sin(harmonic * phase)
    
    # Envelope - crescendo then decrescendo
    envelope = np.sin(np.pi * t / duration) ** 0.3
    signal *= envelope
    
    # Add some breathiness
    breath = np.random.normal(0, 0.08, len(signal))
    # Filter breath to make it low-frequency
    breath = np.convolve(breath, np.ones(10)/10, mode='same')
    signal += breath
    
    return signal.astype(np.float32)


def generate_background(
    duration: float = 1.0,
    sample_rate: int = SAMPLE_RATE,
) -> np.ndarray:
    """
    Generate background noise (no bark).
    Characteristics:
    - Low energy
    - No distinct patterns
    - Ambient noise
    """
    t = np.linspace(0, duration, int(sample_rate * duration))
    
    # White noise at very low level
    signal = np.random.normal(0, 0.05, len(t))
    
    # Add some very low frequency rumble
    rumble_freq = np.random.uniform(20, 60)
    rumble = 0.1 * np.sin(2 * np.pi * rumble_freq * t)
    signal += rumble
    
    # Add some very high frequency hiss
    hiss = np.random.normal(0, 0.02, len(t))
    # Low-pass filter the hiss
    from scipy import signal as scipy_signal
    sos = scipy_signal.butter(4, 2000, 'lp', fs=sample_rate, output='sos')
    hiss = scipy_signal.sosfilt(sos, hiss)
    signal += hiss
    
    return signal.astype(np.float32)


def normalize_audio(audio: np.ndarray, target_db: float = -3.0) -> np.ndarray:
    """Normalize audio to target dB level."""
    max_val = np.max(np.abs(audio))
    if max_val > 0:
        audio = audio / max_val
        # Scale to target dB (rough approximation)
        scale = 10 ** (target_db / 20)
        audio *= scale
    return audio


def save_wav(audio: np.ndarray, filepath: Path, sample_rate: int = SAMPLE_RATE) -> None:
    """Save audio to WAV file."""
    # Ensure directory exists
    filepath.parent.mkdir(parents=True, exist_ok=True)
    
    # Normalize to 16-bit range
    audio_int = (audio * 32767).astype(np.int16)
    
    with wave.open(str(filepath), 'wb') as wf:
        wf.setnchannels(1)  # Mono
        wf.setsampwidth(2)   # 16-bit
        wf.setframerate(sample_rate)
        wf.writeframes(audio_int.tobytes())


def save_metadata(
    filepath: Path,
    bark_class: str,
    duration: float,
    features: Dict[str, Any],
) -> None:
    """Save metadata to JSON file."""
    metadata = {
        "class": bark_class,
        "duration_seconds": duration,
        "sample_rate": SAMPLE_RATE,
        "features": features,
    }
    
    meta_path = filepath.with_suffix('.json')
    with open(meta_path, 'w') as f:
        json.dump(metadata, f, indent=2)


def generate_sample(
    bark_class: str,
    output_dir: Path,
    index: int,
) -> Path:
    """Generate a single training sample."""
    # Select generator and parameters based on class
    generators = {
        "play": (generate_play_bark, DURATION_MIN, 0.8),
        "distress": (generate_distress_bark, DURATION_MIN + 0.3, DURATION_MAX),
        "alert": (generate_alert_bark, DURATION_MIN, 1.2),
        "howl": (generate_howl, 1.5, DURATION_MAX),
        "background": (generate_background, DURATION_MIN, DURATION_MAX),
    }
    
    gen_func, dur_min, dur_max = generators[bark_class]
    duration = np.random.uniform(dur_min, dur_max)
    
    # Generate audio
    audio = gen_func(duration=duration)
    
    # Normalize
    audio = normalize_audio(audio)
    
    # Save
    class_dir = output_dir / bark_class
    filename = f"{bark_class}_{index:04d}.wav"
    filepath = class_dir / filename
    
    save_wav(audio, filepath)
    
    # Calculate features for metadata
    features = {
        "rms_energy": float(np.sqrt(np.mean(audio ** 2))),
        "peak_amplitude": float(np.max(np.abs(audio))),
        "zero_crossing_rate": float(np.sum(np.diff(np.sign(audio)) != 0) / len(audio)),
    }
    
    # FFT-based features
    fft = np.fft.rfft(audio * np.hanning(len(audio)))
    freqs = np.fft.rfftfreq(len(audio), d=1.0 / SAMPLE_RATE)
    power = np.abs(fft) ** 2
    
    if np.sum(power) > 0:
        centroid = np.sum(freqs * power) / np.sum(power)
        features["spectral_centroid"] = float(centroid)
    
    save_metadata(filepath, bark_class, duration, features)
    
    return filepath


def visualize_samples(output_dir: Path, num_samples: int = 3) -> None:
    """Generate visualization of sample waveforms and spectrograms."""
    try:
        import matplotlib.pyplot as plt
    except ImportError:
        print("matplotlib not installed. Install with: pip install matplotlib")
        return
    
    classes = ["play", "distress", "alert", "howl", "background"]
    
    fig, axes = plt.subplots(len(classes), 3, figsize=(15, 12))
    fig.suptitle("Bark Classification Training Data Samples", fontsize=14)
    
    for i, bark_class in enumerate(classes):
        # Find samples
        class_dir = output_dir / bark_class
        if not class_dir.exists():
            continue
        
        wav_files = list(class_dir.glob("*.wav"))
        if not wav_files:
            continue
        
        # Load first sample
        with wave.open(str(wav_files[0]), 'rb') as wf:
            n_frames = wf.getnframes()
            audio_data = wf.readframes(n_frames)
            sample_width = wf.getsampwidth()
            sample_rate = wf.getframerate()
        
        # Convert to numpy
        if sample_width == 2:
            audio = np.frombuffer(audio_data, dtype=np.int16).astype(np.float32) / 32768.0
        else:
            audio = np.frombuffer(audio_data, dtype=np.uint8).astype(np.float32) / 255.0
        
        # Plot waveform
        t = np.arange(len(audio)) / sample_rate
        axes[i, 0].plot(t, audio)
        axes[i, 0].set_title(f"{bark_class.capitalize()} - Waveform")
        axes[i, 0].set_xlabel("Time (s)")
        axes[i, 0].set_ylabel("Amplitude")
        axes[i, 0].set_ylim(-1, 1)
        
        # Plot spectrogram
        from scipy import signal as scipy_signal
        f, t_spec, Sxx = scipy_signal.spectrogram(audio, sample_rate, nperseg=256)
        axes[i, 1].pcolormesh(t_spec, f, 10 * np.log10(Sxx + 1e-10), shading='gouraud')
        axes[i, 1].set_title(f"{bark_class.capitalize()} - Spectrogram")
        axes[i, 1].set_xlabel("Time (s)")
        axes[i, 1].set_ylabel("Frequency (Hz)")
        axes[i, 1].set_ylim(0, 2000)
        
        # Plot FFT
        fft = np.fft.rfft(audio * np.hanning(len(audio)))
        freqs = np.fft.rfftfreq(len(audio), d=1.0 / sample_rate)
        axes[i, 2].plot(freqs, 20 * np.log10(np.abs(fft) + 1e-10))
        axes[i, 2].set_title(f"{bark_class.capitalize()} - Frequency Spectrum")
        axes[i, 2].set_xlabel("Frequency (Hz)")
        axes[i, 2].set_ylabel("Magnitude (dB)")
        axes[i, 2].set_xlim(0, 2000)
    
    plt.tight_layout()
    viz_path = output_dir / "sample_visualization.png"
    plt.savefig(viz_path, dpi=150)
    print(f"Visualization saved to: {viz_path}")
    plt.close()


def generate_tflite_model(
    output_dir: Path,
    input_dim: int = 26,  # 13 MFCC means + 13 MFCC stds
    num_classes: int = 5,
) -> Path:
    """Generate a simple TFLite model for bark classification."""
    if tf is None:
        print("TensorFlow not installed. Cannot generate TFLite model.")
        print("Install with: pip install tensorflow")
        return None
    
    import tensorflow as tf
    from tensorflow import keras
    
    # Create a simple model
    model = keras.Sequential([
        keras.layers.Input(shape=(input_dim,)),
        keras.layers.Dense(64, activation='relu'),
        keras.layers.Dropout(0.3),
        keras.layers.Dense(32, activation='relu'),
        keras.layers.Dense(num_classes, activation='softmax'),
    ])
    
    model.compile(
        optimizer='adam',
        loss='sparse_categorical_crossentropy',
        metrics=['accuracy'],
    )
    
    # Create dummy weights (model needs to be trained on real data)
    # Initialize with reasonable biases for our classes
    model.build()
    
    # Convert to TFLite
    converter = tf.lite.TFLiteConverter.from_keras_model(model)
    converter.optimizations = [tf.lite.Optimize.DEFAULT]
    tflite_model = converter.convert()
    
    # Save
    models_dir = output_dir.parent / "models"
    models_dir.mkdir(parents=True, exist_ok=True)
    model_path = models_dir / "bark_classifier.tflite"
    
    with open(model_path, 'wb') as f:
        f.write(tflite_model)
    
    size_kb = model_path.stat().st_size / 1024
    print(f"TFLite model saved to: {model_path}")
    print(f"Model size: {size_kb:.2f} KB")
    
    return model_path


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate training data for bark classifier"
    )
    parser.add_argument(
        "--samples",
        type=int,
        default=100,
        help="Number of samples per class (default: 100)",
    )
    parser.add_argument(
        "--output",
        type=str,
        default="data/training",
        help="Output directory (default: data/training)",
    )
    parser.add_argument(
        "--visualize",
        action="store_true",
        help="Generate visualization plots",
    )
    parser.add_argument(
        "--model",
        action="store_true",
        help="Generate TFLite model template",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=42,
        help="Random seed (default: 42)",
    )
    args = parser.parse_args()
    
    np.random.seed(args.seed)
    
    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)
    
    classes = ["play", "distress", "alert", "howl", "background"]
    
    print(f"Generating {args.samples} samples per class...")
    print(f"Output directory: {output_dir}")
    print()
    
    # Generate samples for each class
    for bark_class in classes:
        print(f"Generating {bark_class} samples...")
        class_dir = output_dir / bark_class
        class_dir.mkdir(exist_ok=True)
        
        for i in range(args.samples):
            filepath = generate_sample(bark_class, output_dir, i + 1)
            if (i + 1) % 20 == 0:
                print(f"  Generated {i + 1}/{args.samples}")
        
        print(f"  Done! {args.samples} samples in {class_dir}")
    
    print()
    print("=" * 50)
    print("Training data generation complete!")
    print(f"Total samples: {args.samples * len(classes)}")
    print(f"Output directory: {output_dir.absolute()}")
    print()
    
    # Print summary
    for bark_class in classes:
        class_dir = output_dir / bark_class
        wav_count = len(list(class_dir.glob("*.wav")))
        json_count = len(list(class_dir.glob("*.json")))
        print(f"  {bark_class:12s}: {wav_count} WAV files, {json_count} metadata files")
    
    # Visualize if requested
    if args.visualize:
        print()
        print("Generating visualization...")
        visualize_samples(output_dir)
    
    # Generate model if requested
    if args.model:
        print()
        generate_tflite_model(output_dir)
    
    print()
    print("Next steps:")
    print("  1. Review the generated samples")
    print("  2. Train a TFLite model using these samples")
    print("  3. Place the trained model at models/bark_classifier.tflite")


if __name__ == "__main__":
    main()
