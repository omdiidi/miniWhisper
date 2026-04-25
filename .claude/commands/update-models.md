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

3. **Wait for readiness** — poll `/readyz/meeting` up to 180s × 5s intervals:
   ```bash
   for i in $(seq 1 36); do
     curl -fsS -H "Authorization: Bearer $WISPRALT_API_KEY" "$SERVER_URL/readyz/meeting" && break
     sleep 5
   done
   ```

4. **Confirm**: print the result of `curl /metrics` showing the new `meeting_models_ready: true` and the `last_inference_at: null` (cleared by the restart).

## Never

- Do not skip the bounce. The Python process holds the model weights in memory; without a restart, the new files on disk are not used.
- Do not run during an active meeting transcription. Check `/metrics` first; if `meeting.active == true`, wait for it to finish (or accept the in-flight job will be killed by the bounce — its WAV will remain in staging and `recover_orphans` will fail it cleanly on restart).
- Do not push to GitHub without explicit approval.

## Troubleshooting

- If `download-models.sh` fails with HF 401: regenerate token, ensure terms are accepted on both pyannote pages.
- If launchctl fails: check `~/Library/Logs/WisprAlt/server.err.log`.
- If `/readyz/meeting` is 503 after 3 minutes: model load is unusually slow or memory pressure; check `/metrics` for `memory.available_mb`.
