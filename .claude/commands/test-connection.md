---
description: End-to-end connectivity + functional test of the WisprAlt server. Verifies tunnel, auth, dictation roundtrip, readiness, and metrics.
---

# /test-connection

Run a full health/functional check of the server from the current Mac.

## Prerequisites

Read `server/.env` (or read `tmp/client-config.txt`) for `SERVER_URL` and `WISPRALT_API_KEY` (or `API_KEY`). Abort with a clear message if either is missing.

## Checks (run in order, report each)

### 1. Tunnel + healthz (no auth)

```bash
curl -fsS -o /dev/null -w "%{http_code}\n" "$SERVER_URL/healthz"
# expect: 200
```

If this returns `000`, the tunnel is not propagating — wait 30s and retry. If `502`, FastAPI is not running on the Mac mini — instruct user to check `~/Library/Logs/WisprAlt/server.log` and run `scripts/server-launchd.sh status`.

### 2. Bearer auth + dictation readiness

```bash
curl -fsS -H "Authorization: Bearer $API_KEY" "$SERVER_URL/readyz/dictation"
# expect: 200
```

Note: a `200` with header `X-Dictation-Degraded: true` means a meeting is currently being processed; dictation latency may be elevated but is still functional. Surface this to the user.

### 3. Meeting readiness

```bash
curl -fsS -H "Authorization: Bearer $API_KEY" "$SERVER_URL/readyz/meeting"
# expect: 200
```

If `503`, models still loading or available memory < 2GB. Wait and retry. If still 503 after 3 minutes, check the server log.

### 4. Dictation roundtrip

Generate a tiny silent WAV via Python (no ffmpeg dependency):

```bash
python3 -c "import numpy as np, soundfile as sf; sf.write('/tmp/wispralt_test.wav', np.zeros(16000, dtype='float32'), 16000)"
```

Send it:

```bash
curl -fsS -H "Authorization: Bearer $API_KEY" \
  -F "file=@/tmp/wispralt_test.wav;type=audio/wav" \
  "$SERVER_URL/transcribe/dictate"
# expect: HTTP 200, JSON {"text":"","model_id":"...","duration_ms":<n>}
```

Empty `text` is correct (silent audio). The point is to verify the full pipeline.

### 5. Metrics

```bash
curl -fsS -H "Authorization: Bearer $API_KEY" "$SERVER_URL/metrics" | python3 -m json.tool
```

Pretty-print and surface to user. Highlight: `parakeet.p50_ms`, `meeting.active`, `memory.available_mb`, `disk.free_gb`.

## Summary

Print a green ✓ / red ✗ table:
- Tunnel: ✓
- Auth: ✓
- Dictation ready: ✓ (degraded? note)
- Meeting ready: ✓
- Dictation roundtrip: ✓
- Metrics: ✓

If any failed, point to the specific section in `docs/TROUBLESHOOTING.md`.

## Never

- Do not log the API key to chat or file.
- Do not assume `gh` or `ffmpeg` are installed; use Python+soundfile for the test WAV.
