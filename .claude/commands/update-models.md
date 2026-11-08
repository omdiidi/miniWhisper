---
description: Re-run model downloads and reload the FastAPI launchd agent so the server picks up fresh weights. Required after upgrading any of the model dependencies.
---

# /update-models

Update model weights and bounce the server cleanly.

## Steps

1. **Run downloader**:
   ```bash
   bash scripts/download-models.sh
   ```
   This pulls latest revisions of `mlx-community/parakeet-tdt-0.6b-v2` (dictation) and `mlx-community/whisper-large-v3-turbo` (file transcription). Both repos are public; no Hugging Face token is needed.

2. **Bounce the LaunchAgent** (R3#16 — must restart so in-memory weights are re-loaded from disk):
   ```bash
   launchctl bootout gui/$UID/co.wispralt.server || true
   launchctl bootstrap gui/$UID ~/Library/LaunchAgents/co.wispralt.server.plist
   ```

3. **Wait for dictation readiness** — Parakeet is loaded at startup:

       curl -fsS -H "Authorization: Bearer $API_KEY" "$SERVER_URL/readyz/dictation"

   Expect 200 within about 60 s.

4. **Verify the file model** (optional): the Whisper file model loads lazily on the first file job and is evicted when idle. To prove the new weights load, submit a short file:

       curl -fsS -H "Authorization: Bearer $API_KEY" -F "file=@/path/to/short.wav" "$SERVER_URL/transcribe/file"
       # -> 202 {"job_id": "..."}; then poll GET $SERVER_URL/transcribe/file/<job_id> until status is done

   Then `curl -H "Authorization: Bearer $API_KEY" "$SERVER_URL/metrics"` shows `meeting.models_warm: true` (the `meeting` block is the file-job block; the key name is kept for now) and `parakeet.last_inference_at` populated after any dictation.

## Never

- Do not skip the bounce. The Python process holds the model weights in memory; without a restart, the new files on disk are not used.
- Do not run during an active file transcription. Check `/metrics` first; if `meeting.active == true`, wait for it to finish (or accept the in-flight job will be killed by the bounce — its staged audio remains and `recover_orphans` will fail it cleanly on restart).
- Do not push to GitHub without explicit approval.

## Troubleshooting

- If `download-models.sh` fails: check network access to huggingface.co; on HTTP 429, wait a few minutes and retry.
- If launchctl fails: check `~/Library/Logs/WisprAlt/server.err.log`.
- If the verification file job stays "running" past 5 min, or ends "failed": the lazy load likely couldn't fetch or decode the new weights. Check `tail -200 ~/Library/Logs/WisprAlt/server.err.log` for missing model files or out-of-memory, and confirm ≥4 GB free RAM (`/metrics` → `memory.available_mb`).
