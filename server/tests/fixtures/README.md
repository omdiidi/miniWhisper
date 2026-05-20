# Test audio fixtures

Deterministic 1-second 440Hz sine wave at 16kHz mono. Re-encoded into multiple container/codec combos for sync_decode.py format-coverage tests.

Generated via:

```
ffmpeg -y -f lavfi -i "sine=frequency=440:duration=1" -ar 16000 -ac 1 tiny.wav
ffmpeg -y -i tiny.wav -ar 16000 -ac 1 -codec:a libmp3lame -b:a 64k tiny.mp3
ffmpeg -y -i tiny.wav -ar 16000 -ac 1 -codec:a aac -b:a 64k tiny.m4a
ffmpeg -y -i tiny.wav -ar 16000 -ac 1 -codec:a libopus -b:a 64k tiny.webm
```

ffmpeg version used: `ffmpeg version 8.1 Copyright (c) 2000-2026 the FFmpeg developers`

If `libopus` isn't available, use `-codec:a opus`. If `aac` isn't available on
the host, use `-codec:a aac_at` (macOS native).

NOTE: pure tonal sine produces empty/garbage Parakeet transcripts at runtime
— fixtures are for HTTP-shape tests with MOCKED ParakeetService only. Live
verification on the Mac mini uses a real speech sample (see
`scripts/verify-openai-compat.sh`).
