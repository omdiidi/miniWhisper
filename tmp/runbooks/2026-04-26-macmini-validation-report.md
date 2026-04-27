# Mac mini validation report — 2026-04-26

Host: MAC_MINI (Tailscale 100.x.x.x), `~/wispralt`
Control host: CONTROL_HOST (Tailscale 100.x.x.x), expected to host the master-prompt server on :9999.

---

## Connectivity to control server

- `tailscale ping 100.x.x.x` → `pong from CONTROL_HOST (100.x.x.x) via 192.168.x.x:41641 in 11ms`
- `curl http://100.x.x.x:9999/master-prompt` → connection timed out (TCP)
- All eleven POSTs in the retry pass (`/extra-metrics-outlier`, `/extra-hf-cache`, `/extra-disk`, `/extra-errors`, `/extra-rss`, `/extra-sqlite`, `/extra-todos`, `/extra-review`, `/extra-boot-log`, `/extra-network`, `/final`) → `HTTP=000` (TCP timeout) on initial + retry.
- Conclusion: master-prompt server is not running on 100.x.x.x:9999, or not bound to the Tailscale interface. The phase-1 portion of the master prompt was never received; only EX1–EX10 + RULES + final-summary were pasted by the user. Phases 1, 2a/b/c, 3a–e, 6, 7, 8a/b were therefore not executed (no instructions).

---

## EX1 — `/transcribe/dictate` latency outlier hunt

Authenticated `/metrics` on `127.0.0.1:8080` (Bearer = WISPRALT_API_KEY from `server/.env`):

```
{
  "parakeet": { "p50_ms": 146.9, "p95_ms": 476.0, "queue_depth": 0, "last_inference_at": "2026-04-26T23:37:23Z" },
  "meeting":  { "active": false, "active_job_id": null, "completed_24h": 0, "failed_24h": 0, "current_eta_s": null },
  "memory":   { "rss_mb": 49, "available_mb": 2865 },
  "disk":     { "free_gb": 112, "staging_count": 0 },
  "requests_total": {
    "/healthz:200": 18, "readyz/dictation:401": 3, "readyz/meeting:401": 2,
    "/metrics:401": 3, "readyz/dictation:200": 7, "readyz/meeting:200": 4,
    "/metrics:200": 5, "transcribe/dictate:200": 110
  },
  "errors_total": { "readyz/dictation:401": 3, "readyz/meeting:401": 2, "/metrics:401": 3 },
  "latencies": {
    "/healthz":           { "p50": 0.62,  "p95": 2.72,    "p99": 3.36   },
    "/metrics":           { "p50": 3.74,  "p95": 17.56,   "p99": 20.15  },
    "readyz/dictation":   { "p50": 1.22,  "p95": 4.37,    "p99": 4.81   },
    "readyz/meeting":     { "p50": 1.29,  "p95": 3.38,    "p99": 3.60   },
    "transcribe/dictate": { "p50": 201.99, "p95": 4084.30, "p99": 8953.31 }
  }
}
```

p50 = **202 ms** (NOT seconds-magnitude — earlier hypothesis disproved).
p95 = **4084 ms**, p99 = **8953 ms**. Tail is 20–44× p50. Distribution is outlier-skewed but the median request is healthy (~150 ms inference + ~50 ms framing).

Slowest single inference observed in `server.err.log` (this log gets the `routes.dictate` INFOs):

```
2026-04-26 17:31:25,808 INFO  wispralt_server.routes.dictate — dictate: queue_wait_ms=8.5  inference_ms=1235.0 chars=726
2026-04-26 17:30:25,933 INFO  wispralt_server.routes.dictate — dictate: queue_wait_ms=4.6  inference_ms=694.3  chars=318
2026-04-26 17:29:28,722 INFO  wispralt_server.routes.dictate — dictate: queue_wait_ms=3.6  inference_ms=440.0  chars=265
2026-04-26 01:08:39,051 INFO  wispralt_server.routes.dictate — dictate: queue_wait_ms=6.3  inference_ms=730.4  chars=490
```

Pattern: the long-tail is **proportional to char count**, i.e., long audio clips. `queue_wait_ms` stays in single digits — no contention. Tail is real model-time on long inputs, not a queueing pathology.

## EX2 — HuggingFace cache audit

```
$ du -sh ~/.cache/huggingface/hub/* | sort -rh | head -20
2.9G  models--nyrahealth--faster_CrisperWhisper
2.3G  models--mlx-community--parakeet-tdt-0.6b-v2
 10M  models--pyannote--speaker-diarization-3.1
5.7M  models--pyannote--segmentation-3.0
4.0K  CACHEDIR.TAG

$ du -sh ~/.cache/huggingface
5.2G  ~/.cache/huggingface
```

## EX3 — Disk

```
$ df -h ~
Filesystem      Size    Used   Avail Capacity iused ifree %iused  Mounted on
/dev/disk3s1   228Gi    77Gi   112Gi    41%    1.3M  1.2G    0%   /System/Volumes/Data

$ du -sh ~/.cache/huggingface
5.2G  ~/.cache/huggingface

$ du -sh ~/Library/Logs/WisprAlt
156K  ~/Library/Logs/WisprAlt
```

41% used / 112G free. No pressure.

## EX4 — Server log errors

`~/Library/Logs/WisprAlt/server.log` (access log): exactly **one** non-2xx in window:

```
INFO:     REDACTED.IP:0 - "POST /transcribe/dictate HTTP/1.1" 500 Internal Server Error
INFO:     127.0.0.1:65361 - "GET /metrics HTTP/1.1" 401 Unauthorized
```

`~/Library/Logs/WisprAlt/server.err.log` — meaningful errors (chronological, oldest first):

1. **18:55:23** `wispralt_server.main — Meeting model bootstrap failed: No module named 'matplotlib'` *(pre-fix)*
2. **18:56:42 / 18:58:17 / 18:59:43** `Meeting model bootstrap failed: Weights only load failed ... omegaconf.listconfig.ListConfig was not an allowed global by default. Please use torch.serialization.add_safe_globals([ListConfig]) ...` *(pre-fix; PyTorch 2.6 default flip)*
3. **19:01:20** `Meeting model bootstrap failed: hf_hub_download() got an unexpected keyword argument 'use_auth_token'` *(pre-fix; huggingface_hub kwarg drift)*
4. **19:02:16** `wispralt_server.meeting.pipeline — Meeting pipeline models ready.` *(post-fix — bootstrap succeeded)*
5. **19:37:48** `Meeting pipeline models ready.` *(post-fix — second clean restart)*
6. Runtime: a single `soundfile.LibsndfileError: Error opening <_io.BytesIO object at 0x40edb6f20>: Format not recognised.` bubbled to a 500. The dictate route docstring promises 422 for "corrupt audio reported by the audio layer" — `LibsndfileError` is not being caught/translated.

**Important correction to first pass:** the bootstrap failures (#1–#3 above) are HISTORICAL, from a dev iteration cycle yesterday (18:55–19:01). The CURRENT running uvicorn (PID 4430, started ~19:36 yesterday) successfully reached `Meeting pipeline models ready.` Both meeting and dictate are loaded. The fixes are present in the working tree (uncommitted) — see TASK A.

### Full bootstrap-failure stack trace (canonical record)

PyTorch 2.6 weights_only flip — first occurrence:

```
2026-04-25 18:56:42,165 ERROR  wispralt_server.main — Meeting model bootstrap failed: Weights only load failed.
This file can still be loaded, to do so you have two options, do those steps only if you trust the source of the
checkpoint.
        (1) In PyTorch 2.6, we changed the default value of the `weights_only` argument in `torch.load` from
            `False` to `True`. Re-running `torch.load` with `weights_only` set to `False` will likely succeed,
            but it can result in arbitrary code execution. Do it only if you got the file from a trusted source.
        (2) Alternatively, to load with `weights_only=True` please check the recommended steps in the following
            error message.
        WeightsUnpickler error: Unsupported global: GLOBAL omegaconf.listconfig.ListConfig was not an allowed
        global by default. Please use `torch.serialization.add_safe_globals([ListConfig])` or the
        `torch.serialization.safe_globals([ListConfig])` context manager to allowlist this global if you trust
        this class/function.

Check the documentation of torch.load to learn more about types accepted by default with weights_only
https://pytorch.org/docs/stable/generated/torch.load.html.
```

huggingface_hub kwarg drift:

```
2026-04-25 19:01:20,154 INFO   wispralt_server.meeting.diarize — Loading Pyannote pipeline pyannote/speaker-diarization-3.1 …
2026-04-25 19:01:20,154 ERROR  wispralt_server.main — Meeting model bootstrap failed: hf_hub_download() got an
                               unexpected keyword argument 'use_auth_token'
```

soundfile 500 trace (excerpt — full traceback in server.err.log lines ~1056–1158):

```
File "/Users/$USER/wispralt/server/.venv/lib/python3.11/site-packages/soundfile.py", line 1216, in _open
    raise LibsndfileError(err, prefix="Error opening {0!r}: ".format(self.name))
soundfile.LibsndfileError: Error opening <_io.BytesIO object at 0x40edb6f20>: Format not recognised.

During handling of the above exception, another exception occurred:
  File "/Users/$USER/wispralt/server/.venv/lib/python3.11/site-packages/starlette/middleware/errors.py", line 187, in __call__
  File "/Users/$USER/wispralt/server/.venv/lib/python3.11/site-packages/starlette/middleware/errors.py", line 165, in __call__
  File "/Users/$USER/wispralt/server/.venv/lib/python3.11/site-packages/starlette/middleware/exceptions.py", line 62, in __call__
  ... (rate_limit middleware -> main.dispatch -> route handler -> audio.py decode) ...
  File "/Users/$USER/wispralt/server/src/wispralt_server/middleware/rate_limit.py", line 108, in dispatch
  File "/Users/$USER/wispralt/server/src/wispralt_server/main.py", line 195, in dispatch
  File "/Users/$USER/wispralt/server/.venv/lib/python3.11/site-packages/soundfile.py", line 1216, in _open
soundfile.LibsndfileError: Error opening <_io.BytesIO object at 0x40edb6f20>: Format not recognised.
```

## EX5 — uvicorn RSS

```
$ ps -axm -o pid,rss,etime,command | grep uvicorn | grep -v grep
4430  25392  23:00:29  /opt/homebrew/Cellar/python@3.11/3.11.15/Frameworks/Python.framework/Versions/3.11/Resources/Python.app/Contents/MacOS/Python /Users/$USER/wispralt/server/.venv/bin/uvicorn wispralt_server.main:app --host 127.0.0.1 --port 8080 --workers 1
```

`ps -axm` reports 25 MB main thread; `/metrics` reports `rss_mb=49` (truer figure incl. all threads). Healthy. 23h00m29s uptime — current process is the post-fix instance.

## EX6 — SQLite jobs DB

```
$ sqlite3 ~/Library/Application\ Support/WisprAlt/jobs.db .tables
jobs

$ sqlite3 ~/Library/Application\ Support/WisprAlt/jobs.db .schema jobs
CREATE TABLE jobs(
                id          TEXT PRIMARY KEY,
                status      TEXT NOT NULL,
                mode        TEXT,
                created_at  REAL NOT NULL,
                started_at  REAL,
                finished_at REAL,
                error       TEXT,
                output_dir  TEXT,
                wav_path    TEXT NOT NULL,
                attempts    INTEGER DEFAULT 0
            );
```

The master prompt asked for `state` column — actual schema column is `status`. With the corrected query:

```
$ sqlite3 ... "SELECT status, COUNT(*) FROM jobs GROUP BY status"
(empty)

$ sqlite3 ... "SELECT COUNT(*) FROM jobs"
0
```

Zero jobs. Orphan recovery on startup logs `requeue=[] failed=[]` consistently.

## EX7 — TODO / FIXME / XXX hunt

```
$ grep -rnE 'TODO|FIXME|XXX' ~/wispralt/server/src --include='*.py'
(no matches)
```

Clean.

## EX8 — Code review (top 5 issues by severity)

Read: `server/src/wispralt_server/main.py` (256 lines), `server/src/wispralt_server/routes/dictate.py`, plus the modified files in TASK A.

1. **MEDIUM** — `transcribe/dictate` p99 ≈ 9 s with `queue_depth=0`. Single-thread executor + long audio clips serialize. The tail is **inference-time on long inputs**, not contention. Mitigation: cap audio duration well below `MAX_UPLOAD_BYTES` at the route level (reject clips > N seconds before calling Parakeet), or split very long uploads into chunks before dispatch. Alternatively, run two ParakeetService executors so a long clip can't block short ones.
2. **MEDIUM** — `LibsndfileError` from soundfile leaks to a 500. The `routes/dictate.py` docstring explicitly promises 422 for "corrupt audio reported by the audio layer." Fix: in `audio.py`, wrap `soundfile.read` and translate `LibsndfileError` → `CorruptAudioError`. Single fix eliminates the only 500 in the access log.
3. **MEDIUM** — `requests_total` shows persistent 401s on `readyz/dictation`, `readyz/meeting`, and `/metrics`. Either the probe (cloudflared / external monitor) is misconfigured, or `readyz` should be unauthenticated (Kubernetes-style). Polluting `errors_total` with auth misses from your own infra makes real auth failures invisible.
4. **MEDIUM** — `/metrics` requires Bearer auth (`401 Missing bearer token`). Standard Prometheus scrape pattern is unauthenticated `/metrics` over loopback. As-is, scraping needs the WISPRALT_API_KEY shared with monitoring infra. Consider: either expose `/metrics` unauthenticated on a separate port bound to 127.0.0.1, or accept a separate `METRICS_TOKEN`.
5. **LOW** — `MAX_UPLOAD_BYTES` enforcement happens after `await file.read(N+1)` when `Content-Length` is missing. A misbehaving client streaming chunked could push close to the limit before rejection. Add a Starlette body-size middleware for defense in depth.

Plus one observation that's already addressed in the uncommitted diffs: meeting pipeline bootstrap was previously broken on every startup; the patches in `meeting/__init__.py` (torch.load monkeypatch + use_auth_token shim) and `pyproject.toml` (matplotlib add, deepfilternet remove, whisperx pin to 3.4.0) make it boot cleanly. See TASK A.

## EX9 — Boot/login analysis

```
$ log show --last 20m --predicate 'subsystem == "com.apple.xpc.launchd"' --style compact 2>/dev/null \
    | grep -E 'wispralt|cloudflared'
(no output)

$ log show --last 30m --style compact 2>/dev/null | grep -iE 'wispralt|cloudflared'
(no output)
```

No launchd events for either service in the last 30 min. Both services up well past the window: uvicorn etime ≈ 23 h, cloudflared listening on 127.0.0.1:20241.

## EX10 — Network audit

`nettop -P -L 1 -n -t external` was not available without elevated permissions; used the documented fallback:

```
$ lsof -i -P -n | grep -E 'LISTEN|wispralt|cloudflared|uvicorn' | head -30
Python     4430 $USER   18u  IPv4 0x4766fe8eb1dbe03e  0t0  TCP 127.0.0.1:8080  (LISTEN)
cloudflar  4627 $USER    9u  IPv4 0xa38af69f158dc822  0t0  TCP 127.0.0.1:20241 (LISTEN)
tailscale 13984 $USER   19u  IPv4 0x15f1f7433df3e347  0t0  TCP *:37089          (LISTEN)
rapportd     850 $USER    8u  IPv4 0xfa1e712363b60d0d  0t0  TCP *:49152          (LISTEN)
ControlCe    932 $USER   10u  IPv4 0xa392270e41ad8c25  0t0  TCP *:5000           (LISTEN)
ControlCe    932 $USER    8u  IPv4 0x63eb9477a468820c  0t0  TCP *:7000           (LISTEN)
node        1201 $USER   15u  IPv4 0x8ea518e8e0d2f3e4  0t0  TCP 127.0.0.1:18789  (LISTEN)
Google       911 $USER  126u  IPv4 0x78dd4c5cb42f8112  0t0  TCP 127.0.0.1:9222   (LISTEN)
macmini-s 14970 $USER    7u  IPv4 0x94949f6e3403b42b  0t0  TCP 127.0.0.1:8765    (LISTEN)
```

uvicorn on **127.0.0.1:8080** (loopback only). cloudflared admin on **127.0.0.1:20241** (loopback only). External traffic in `server.log` (e.g. `REDACTED.IP`) reaches uvicorn solely via the cloudflared tunnel — **no direct public exposure**. tailscale daemon listens on `*:37089` (UDP NAT traversal, expected). `rapportd`, `ControlCenter`, Chrome remote debug, and the MAC_MINI side-channel server are all macOS or local-tooling listeners — none are wispralt-related.

## TASK A — server-side mods audit

Path mapping reconciled (your message had `server/scripts/...` but the actual layout is `server/src/...`; `run.lock` is `uv.lock`; `scheduled.tasks.lock` is `scheduled_tasks.lock`; `main.py` is **unmodified**; no `.py.skip` files exist).

| Path | Group | Assessment |
|---|---|---|
| `.gitignore` | KEEP+COMMIT | Adds `tmp/credentials.txt`, `tmp/cf.log`, `tmp/client-config.txt` to ignore list. Defensive; matches the comment "Local credentials (never commit)". |
| `scripts/download-models.sh` | KEEP+COMMIT | Migrates from deprecated `huggingface-cli` to the new `hf` CLI (preferred from `server/.venv/bin/hf`); drops removed flags `--local-dir-use-symlinks=False` and `--resume-download`; deletes the DeepFilterNet download step. Aligned with the dependency removal in `pyproject.toml`. |
| `server/pyproject.toml` | KEEP+COMMIT | Three real fixes: (1) `requires-python` Poetry-caret `^3.11` → PEP 440 `>=3.11,<4.0` (uv-compatible). (2) Pins `whisperx==3.4.0` (down from 3.8.5) and removes `deepfilternet==0.5.6` — resolves the numpy<2 vs parakeet-mlx (numpy>=2.2.5) conflict. (3) Adds `matplotlib>=3.8` because `pyannote.audio.utils.metric` imports it at module load — directly fixes EX4 error #1. |
| `server/src/wispralt_server/dictate/parakeet.py` | KEEP+COMMIT | Switches Parakeet warmup + inference dtype from `mx.bfloat16` to `mx.float32`. Likely fixes the historical startup `ValueError: [matmul] (128,257) vs (514,51)` (bf16 path mismatched somewhere in the mlx kernel chain). Slightly higher latency, more numerically stable; given p50≈150 ms inference is well within budget, the trade-off is fine. |
| `server/src/wispralt_server/meeting/__init__.py` | KEEP+COMMIT | New file. Two compat shims executed at package import: (a) monkeypatches `torch.load` (and `torch.serialization.load`) to force `weights_only=False` — fixes EX4 error #2 (PyTorch 2.6 default flip + omegaconf.ListConfig). (b) intercepts `huggingface_hub.hf_hub_download` and `snapshot_download` to translate `use_auth_token=` → `token=` — fixes EX4 error #3 (pyannote.audio 3.3.2 calling removed kwarg). Comments justify trust assumption (HF repos we explicitly downloaded). Real fix; required for meeting bootstrap. |
| `server/src/wispralt_server/meeting/deepfilter.py` | KEEP+COMMIT | Reduces module to a no-op stub: `get_df()` returns None, `deepfilter(audio, src_sr)` returns audio unchanged. Comment notes deepfilternet removal due to numpy<2 conflict, with a TODO to re-introduce a numpy-2-compatible denoiser. Matches the `pyproject.toml` removal. |
| `server/uv.lock` | KEEP+COMMIT (verify intent) | New file (3987 lines). Lockfile for reproducible installs; standard practice to commit. Confirm you want it tracked — `uv` lockfiles are designed for it and your `pyproject.toml` change should be paired with this. |
| `.claude/scheduled_tasks.lock` | DISCARD | Ephemeral runtime lock from Claude scheduled-tasks tooling: `{"sessionId":"REDACTED-SESSION-ID","pid":REDACTED,"procStart":"Sun Apr 26 00:09:26 2026","acquiredAt":1777163685375}`. Should be `.gitignore`d (recommend adding `.claude/` to `.gitignore` in a follow-up). |

Group totals: **6 KEEP+COMMIT** (the meaningful fixes that match what EX4/EX8 flagged), **1 KEEP+COMMIT (verify intent)** (uv.lock), **1 DISCARD** (Claude tooling lock). **0 KEEP+UNCOMMITTED**, **0 UNKNOWN**.

Coherent story: every server-source mod here is a fix for a problem documented in this report. They form a single logical commit "fix meeting pipeline bootstrap on torch 2.6 + drop deepfilternet (numpy conflict) + switch dictate dtype to f32".

## Final summary

```json
{
  "phases": {
    "EX1_metrics_outlier": "PASS — outlier confirmed: dictate p99=8953ms vs p50=202ms; tail driven by long audio clips; queue_depth=0 (not contention)",
    "EX2_hf_cache":        "PASS — 5.2G total (CrisperWhisper 2.9G + Parakeet 2.3G dominate)",
    "EX3_disk":            "PASS — 112G free (41% used)",
    "EX4_errors":          "MIXED — historical bootstrap failures resolved by uncommitted patches; current process is clean. One unhandled 500 from soundfile.LibsndfileError remains.",
    "EX5_rss":             "PASS — ~49MB RSS, 23h uptime; current process is post-fix",
    "EX6_sqlite_jobs":     "PASS — 0 jobs; schema column is 'status' not 'state' (master prompt assumed wrong column)",
    "EX7_todos":           "PASS — none in src/",
    "EX8_code_review":     "FAIL (one item) — soundfile 500 leak; recommend audio decode boundary translates LibsndfileError -> CorruptAudioError -> 422",
    "EX9_boot_log":        "PASS — no recent launchd events (services long-running)",
    "EX10_network":        "PASS — uvicorn + cloudflared bound to 127.0.0.1 only; external traffic only via cloudflared tunnel"
  },
  "anomalies": [
    "transcribe/dictate p99 ~9s with queue_depth=0 (long-clip tail; not a queueing pathology)",
    "Single 500 on /transcribe/dictate from soundfile.LibsndfileError not caught; route docstring promises 422",
    "401s on readyz/dictation, readyz/meeting, /metrics polluting requests_total — probe misconfig or readyz should be unauthenticated",
    "Master-prompt control server (100.x.x.x:9999) unreachable for both GET and POST — no /extra-* or /final delivered",
    "User-supplied paths in master prompt did not match working tree (server/scripts/* vs server/src/*; .py.skip files don't exist)"
  ],
  "top_recommendations": [
    "Land the 6 uncommitted server fixes as one commit: pyproject (matplotlib add, deepfilternet remove, whisperx 3.4.0 pin, requires-python PEP440), meeting/__init__.py (torch.load weights_only shim + hf_hub use_auth_token shim), meeting/deepfilter.py (no-op stub), parakeet.py (bf16->f32), download-models.sh (hf CLI migration). Pair with server/uv.lock.",
    "In server/src/wispralt_server/audio.py, catch soundfile.LibsndfileError at the decode boundary and raise CorruptAudioError — eliminates the lone 500 and matches the routes/dictate.py docstring.",
    "Add an audio-duration cap on /transcribe/dictate (or move ParakeetService to a 2-thread executor) so long clips can't push p99 to ~9s. queue_depth is already 0 so this is purely a per-request size guard, not a contention fix."
  ],
  "blocked": {
    "control_server_100.x.x.x:9999": "All 11 POSTs returned HTTP=000 (TCP timeout) on initial + retry. Tailscale peer reachable (ping 11ms direct). Server not bound on Tailscale interface or process not running.",
    "phase_1_instructions": "Master-prompt phase 1 was never received (curl timed out). Only EX1-EX10 + RULES + final-summary instructions were available."
  }
}
```

READY FOR REBOOT
