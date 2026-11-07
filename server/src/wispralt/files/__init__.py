"""
files/ — Uploaded-file transcription (mono mlx-whisper, lazily loaded).

Modules:
- ``pipeline``            — orchestration run in the runner's executor thread.
- ``mlx_whisper_loader``  — one-shot mlx-whisper load / transcribe / reset.
- ``segments``            — pure segment labelling helpers for the transcript JSON.
- ``output``              — atomic JSON/SRT/VTT/TXT writers.
"""
