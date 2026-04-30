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
   This pulls latest revisions of: `mlx-community/parakeet-tdt-0.6b-v2`, `nyrahealth/faster_CrisperWhisper`, `pyannote/speaker-diarization-3.1`, `pyannote/segmentation-3.0`, wav2vec2 align (via whisperx), DeepFilterNet 3 (via `df.init_df`).

2. **Bounce the LaunchAgent** (R3#16 — must restart so in-memory weights are re-loaded from disk):
   ```bash
   launchctl bootout gui/$UID/co.wispralt.server || true
   launchctl bootstrap gui/$UID ~/Library/LaunchAgents/co.wispralt.server.plist
   ```

3. **Wait for server health** — poll `/readyz/meeting` once (expect 200 immediately):

       curl -fsS -H "Authorization: Bearer $WISPRALT_API_KEY" "$SERVER_URL/readyz/meeting"

   Note: this returns 200 even when models are cold. To verify the new weights actually
   load, run `scripts/smoke-meeting.sh` after the curl. Smoke uploads a 5s test WAV,
   waits for transcription, and prints RSS delta — confirming the lazy load fires
   against the new weights.

4. **Confirm**: print the result of `curl /admin/metrics` showing `meeting.models_warm: true`
   (set by smoke-meeting.sh) and the `last_inference_at` field freshly populated.

## Never

- Do not skip the bounce. The Python process holds the model weights in memory; without a restart, the new files on disk are not used.
- Do not run during an active meeting transcription. Check `/metrics` first; if `meeting.active == true`, wait for it to finish (or accept the in-flight job will be killed by the bounce — its WAV will remain in staging and `recover_orphans` will fail it cleanly on restart).
- Do not push to GitHub without explicit approval.

## Troubleshooting

- If `download-models.sh` fails with HF 401: regenerate token, ensure terms are accepted on both pyannote pages.
- If launchctl fails: check `~/Library/Logs/WisprAlt/server.err.log`.
- If `scripts/smoke-meeting.sh` fails (job stays "running" past 5 min, or "failed"):
  the lazy load likely couldn't fetch/decode the new weights. Check `tail -200
  ~/wispralt/logs/server.error.log` for HF token errors, missing model files, or
  out-of-memory. Verify `HF_TOKEN` is set in `~/wispralt/server/.env` and that you
  have ≥4 GB free RAM (`/admin/metrics` → `memory.available_mb`).
