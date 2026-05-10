#!/usr/bin/env python3
"""Phase 0 spike: benchmark mlx-whisper on the prod-mini against a real file.

Separately times ffmpeg_decode, transcribe, and (optionally) pyannote diarize.
Samples RSS via psutil every 2 s while the heavy steps run.

Usage:
    python3 benchmark-mlx-whisper.py --input /path/to/audio.m4a --mode file
    python3 benchmark-mlx-whisper.py --input /path/to/audio.m4a --mode meeting

Outputs a single JSON line to stdout with all timings + peak RSS. Exit non-zero
on any failure.

Pins the model to ``mlx-community/whisper-large-v3-turbo`` per plan T0.5. Use
``WHISPER_MODEL`` env var to override (e.g. fallback to ``whisper-large-v3-mlx``).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import numpy as np
import psutil


WHISPER_MODEL = os.environ.get(
    "WHISPER_MODEL", "mlx-community/whisper-large-v3-turbo"
)


def ffprobe_duration(src: Path) -> float:
    out = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(src),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return float(out.stdout.strip())


def ffmpeg_decode_to_wav(src: Path, dst: Path) -> None:
    """Decode any container to 16 kHz mono Float32 WAV via ffmpeg."""
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(src),
            "-ac",
            "1",
            "-ar",
            "16000",
            "-f",
            "wav",
            "-acodec",
            "pcm_f32le",
            str(dst),
        ],
        check=True,
        capture_output=True,
    )


def load_wav_to_numpy(wav_path: Path) -> np.ndarray:
    """Load a 16 kHz WAV into a mono float32 numpy array.

    Handles Float32 (sampwidth=4), Int24 (sampwidth=3), Int16 (sampwidth=2).
    Falls back to ffmpeg-decode-to-stdout if `wave` can't parse the header
    (covers WAVEFORMATEXTENSIBLE + float-tagged headers etc).
    """
    import wave

    try:
        with wave.open(str(wav_path), "rb") as wf:
            sr = wf.getframerate()
            nframes = wf.getnframes()
            nchans = wf.getnchannels()
            sampwidth = wf.getsampwidth()
            raw = wf.readframes(nframes)
    except wave.Error:
        # Fall back to ffmpeg piping raw f32le into stdin
        return _ffmpeg_decode_to_numpy(wav_path)

    if sampwidth == 4:
        audio = np.frombuffer(raw, dtype=np.float32)
    elif sampwidth == 3:
        # Int24 (3 bytes per sample, little-endian). Pad each sample to int32.
        n = len(raw) // 3
        ints = np.zeros(n, dtype=np.int32)
        ints |= np.frombuffer(raw, dtype=np.uint8)[0::3].astype(np.int32)
        ints |= np.frombuffer(raw, dtype=np.uint8)[1::3].astype(np.int32) << 8
        ints |= np.frombuffer(raw, dtype=np.uint8)[2::3].astype(np.int32) << 16
        # Sign-extend from 24-bit
        ints = np.where(ints & 0x800000, ints | ~0xFFFFFF, ints)
        audio = ints.astype(np.float32) / (2**23)
    elif sampwidth == 2:
        audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    else:
        raise RuntimeError(f"Unsupported sample width: {sampwidth}")

    if nchans > 1:
        audio = audio.reshape(-1, nchans).mean(axis=1)
    if sr != 16000:
        raise RuntimeError(f"Expected 16 kHz, got {sr}")

    return np.ascontiguousarray(audio, dtype=np.float32)


def _ffmpeg_decode_to_numpy(src: Path) -> np.ndarray:
    """Last-resort decoder: invoke ffmpeg to produce f32 mono 16k via stdin."""
    proc = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            str(src),
            "-ac",
            "1",
            "-ar",
            "16000",
            "-f",
            "f32le",
            "pipe:1",
        ],
        check=True,
        capture_output=True,
    )
    return np.frombuffer(proc.stdout, dtype=np.float32)


def sample_rss(stop_flag: dict, peak: dict) -> None:
    """Background thread: every 2 s, sample this process's RSS and update peak."""
    proc = psutil.Process(os.getpid())
    while not stop_flag["stop"]:
        try:
            rss_mb = proc.memory_info().rss / (1024 * 1024)
            if rss_mb > peak["mb"]:
                peak["mb"] = rss_mb
        except Exception:
            pass
        time.sleep(2)


def run_transcribe(audio_16k: np.ndarray, word_timestamps: bool) -> dict:
    """Returns mlx-whisper transcribe result dict."""
    import mlx_whisper

    return mlx_whisper.transcribe(
        audio_16k,
        path_or_hf_repo=WHISPER_MODEL,
        word_timestamps=word_timestamps,
        language="en",
        hallucination_silence_threshold=2.0,
        verbose=False,
    )


def run_diarize(audio_16k: np.ndarray, hf_token: str | None) -> tuple[int, dict]:
    """Returns (num_speakers, diarization annotation). Loads pyannote on demand."""
    import torch
    from pyannote.audio import Pipeline

    pipeline = Pipeline.from_pretrained(
        "pyannote/speaker-diarization-3.1",
        use_auth_token=hf_token,
    )
    device = torch.device("mps") if torch.backends.mps.is_available() else torch.device("cpu")
    pipeline.to(device)

    # Pyannote expects a dict with waveform + sample_rate; in-memory path.
    waveform_t = torch.from_numpy(audio_16k).unsqueeze(0)
    annotation = pipeline({"waveform": waveform_t, "sample_rate": 16000})
    speakers = set()
    for _, _, speaker in annotation.itertracks(yield_label=True):
        speakers.add(speaker)
    return len(speakers), {"num_speakers": len(speakers)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--mode", required=True, choices=["file", "meeting"])
    parser.add_argument("--out", type=Path, default=None, help="JSON output path; default stdout")
    args = parser.parse_args()

    if not args.input.exists():
        print(f"ERROR: input file not found: {args.input}", file=sys.stderr)
        return 2

    result: dict = {
        "input": str(args.input),
        "mode": args.mode,
        "model": WHISPER_MODEL,
        "audio_duration_s": None,
        "ffmpeg_decode_s": None,
        "transcribe_s": None,
        "pyannote_s": None,
        "wall_clock_s": None,
        "realtime_ratio": None,
        "peak_rss_mb": None,
        "segments_count": None,
        "speakers_detected": None,
        "error": None,
    }

    peak = {"mb": 0.0}
    stop_flag = {"stop": False}
    sampler = threading.Thread(target=sample_rss, args=(stop_flag, peak), daemon=True)
    sampler.start()

    wall_t0 = time.monotonic()

    try:
        # ffprobe duration
        duration_s = ffprobe_duration(args.input)
        result["audio_duration_s"] = duration_s

        # ffmpeg decode
        with tempfile.TemporaryDirectory() as tmp:
            wav_path = Path(tmp) / "decoded.wav"
            t0 = time.monotonic()
            ffmpeg_decode_to_wav(args.input, wav_path)
            result["ffmpeg_decode_s"] = round(time.monotonic() - t0, 2)

            audio_16k = load_wav_to_numpy(wav_path)

            # transcribe
            word_ts = args.mode == "meeting"
            t0 = time.monotonic()
            transcribe_result = run_transcribe(audio_16k, word_timestamps=word_ts)
            result["transcribe_s"] = round(time.monotonic() - t0, 2)
            result["segments_count"] = len(transcribe_result.get("segments", []))

            # pyannote (meeting mode only)
            if args.mode == "meeting":
                hf_token = os.environ.get("HF_TOKEN")
                if not hf_token:
                    print(
                        "WARNING: HF_TOKEN not set; pyannote may fail to download model",
                        file=sys.stderr,
                    )
                t0 = time.monotonic()
                n_speakers, _ = run_diarize(audio_16k, hf_token)
                result["pyannote_s"] = round(time.monotonic() - t0, 2)
                result["speakers_detected"] = n_speakers

        result["wall_clock_s"] = round(time.monotonic() - wall_t0, 2)
        if result["transcribe_s"]:
            result["realtime_ratio"] = round(duration_s / result["transcribe_s"], 2)
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        stop_flag["stop"] = True
        result["peak_rss_mb"] = round(peak["mb"], 1)
        if args.out:
            args.out.write_text(json.dumps(result, indent=2))
        print(json.dumps(result, indent=2))
        return 1

    stop_flag["stop"] = True
    sampler.join(timeout=3)
    result["peak_rss_mb"] = round(peak["mb"], 1)

    output_json = json.dumps(result, indent=2)
    if args.out:
        args.out.write_text(output_json)
    print(output_json)
    return 0


if __name__ == "__main__":
    sys.exit(main())
