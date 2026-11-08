# Install

WisprAlt installs, runs and is maintained on each Mac with one command,
`wispralt`. A Mac can take one of three roles:

- **host**: runs the local transcription server (Apple Silicon only). It keeps
  every transcript on this Mac and serves the other Macs in Local mode.
- **client**: the menubar app. Online mode uses your own OpenAI key; Local mode
  sends audio to a host.
- **both**: a host plus the app on the same Mac, already connected to each other.

## 1. Quick start

```bash
git clone <repo-url> wispralt
cd wispralt
./install.sh            # asks which role, then sets everything up
```

`install.sh` does only what has to happen before Python exists: it installs
Homebrew if it is missing (that step asks for your password), then `ffmpeg`
and `uv`, then installs the `wispralt` CLI from this clone with
`uv tool install`. It then runs `wispralt setup` and passes every argument
through. It never uses `sudo` and is safe to run again.

If `wispralt` is not on your `PATH` afterwards, run `uv tool update-shell` and
open a new terminal.

## 2. Requirements

| | host | client |
|---|---|---|
| macOS | 14 (Sonoma) or newer | 14 or newer |
| Chip | Apple Silicon (MLX) | any |
| Disk | 6 GB free (models are about 4 GB) | about 200 MB |
| Tools | Homebrew, `ffmpeg`, `uv` (installed for you) | the same; plus, to build the app, full Xcode and an Apple Development identity (Xcode > Settings > Accounts). With a signed DMG (`--dmg`) neither is needed. |

The host runs as a per-user LaunchAgent, so it runs only while you are logged
in. `wispralt doctor` says so and checks everything else in this table.

## 3. Scripted or agent install (no prompts)

```bash
./install.sh --role host --non-interactive \
  --admin-label owner --token-file ~/wispralt-admin.key --no-open
```

- `--non-interactive` never prompts. A missing required answer (only `--role`)
  exits 2 and names the flag. Retention defaults to 0 (keep forever) and the
  admin label to `owner`.
- The first admin key is printed only to a terminal. Without one, pass
  `--token-file PATH`: a **new** file created 0600 (an existing file is
  refused; a failed write removes it). Without either, setup stops with exit 2 before it downloads or
  creates anything. Delete the file once the key is stored.
- Homebrew's own installer needs a password, so a non-interactive run on a Mac
  without Homebrew stops (exit 2) and says how to install it.
- Check the result with `wispralt doctor --json` (exit 0 = healthy, 3 = a check
  failed; each failing check has a one-line fix).
- Exit codes everywhere: 0 ok, 1 operation failed, 2 usage error or missing
  answer, 3 doctor found failures.

For a throwaway test install, set `WISPRALT_ROOT=/some/dir` (every path below
moves under it, including the app's `Applications/` folder) and
`WISPRALT_LABEL=co.wispralt.mytest` (the LaunchAgent label). A re-labelled
install never touches the real host's agents.

## 4. What `setup` does

Host (`--role host` or `both`):

1. Checks the Mac (not root, Apple Silicon, macOS 14+, 6 GB free, `ffmpeg`, `uv`).
2. Installs the Apple Silicon inference stack (`wispralt[host]`) if it is
   missing, then restarts itself in the new code.
3. Writes `~/.config/wispralt/.env` (0600) with `DATA_DIR`, `SERVER_URL`
   (default `http://127.0.0.1:8000`) and `SERVER_LOG_PATH`. Only absent keys are
   added; a value you edited is never overwritten.
4. Downloads the two models into `~/Library/Application Support/WisprAlt/models`
   and verifies each weight file's sha256 against Hugging Face. A rerun with a
   verified cache downloads nothing.
5. Creates the database and the first admin, and hands you its key once. A rerun
   never creates a second admin and never rotates a key.
6. Asks once how long to keep transcripts (0 = forever).
7. Loads the dictation model once with a test tone (`--skip-warmup` skips it).
8. Installs the LaunchAgent `co.wispralt.host` (loopback `127.0.0.1:<port>`),
   waits for `/healthz`, and opens the portal at `/admin` (`--no-open` skips it).
   A mini-era `co.wispralt.server` agent is booted out and its plist moved to
   `~/.config/wispralt/legacy/`.

Client (`--role client` or `both`):

1. Installs the app from `--dmg PATH` (checked with `codesign --verify --deep
   --strict` and `spctl -a -t exec`; the quarantine flag is never removed) or
   builds it from the clone with `scripts/build-client-local.sh`. A rerun skips
   the build when the installed version matches (`--rebuild` forces it).
2. Quits the running app, copies the new one into `/Applications/WisprAlt.app`
   and opens it.
3. With `both`, points the app at the host (`serverURL`) and stores the admin key
   in the Keychain item `co.wispralt` (sent to `security` on stdin, never on a
   command line).
4. The app's mode changes only with `--client-mode local|online`; Online stays
   the default. The OpenAI key is never handled by the CLI: add it in the app's
   Settings.

## 5. The three macOS permissions

macOS will not let any script grant these; the app walks you through each pane
on first launch.

- **Accessibility**: to type the transcript at the cursor in any app.
- **Input Monitoring**: to detect the FN-key hold that starts dictation. On
  macOS 14.4+ quit and reopen the app after granting it.
- **Microphone**: to capture your voice.

## 6. Command reference

Global flags work before or after the command: `--json`, `--non-interactive`,
`--yes`. `wispralt <command> -h` lists every flag.

| Command | What it does |
|---|---|
| `setup [--role host\|client\|both]` | install or repair (safe to rerun). Flags: `--source DIR`, `--data-dir`, `--backup-dir`, `--server-url`, `--port`, `--admin-label`, `--token-file`, `--retention-days N`, `--no-open`, `--skip-warmup`, `--dmg PATH`, `--client-mode online\|local`, `--rebuild` |
| `status [--client]` | service state and pid, URL, version, allocated sizes (data, database, outputs, staging, models, backups with count and newest, `*.pre-restore-*` copies), retention and what it would purge now, admin count |
| `doctor [--warmup] [--client]` | read-only checks with a fix per failure (exit 3 on any failure); `--warmup` also loads the dictation model |
| `start` / `stop` / `restart` | the host service; `stop` waits until the server has released its data lock; `restart` reloads the plist only if it changed |
| `users list\|add\|disable\|enable` | accounts (see [ADMIN.md](ADMIN.md#from-the-command-line)) |
| `keys list\|rotate\|revoke\|rotate-admin` | API keys; `rotate-admin` recovers a lost admin key |
| `retention show` / `retention set DAYS` | `set` prints what the next purge would delete and asks first (`--yes` in scripts) |
| `backup [--dest DIR]` / `backup list` | offline backup: the service stops briefly and restarts |
| `restore BACKUP_DIR` | replaces the host data with a backup; the current data is kept at `<data dir>.pre-restore-<time>` |
| `update [--no-pull] [--no-backup] [--client]` | `git pull --ff-only` in the clone (refused when it has local changes), backup, reinstall, restart, health check (if the backup or reinstall fails, the old service is started again); `--client` also rebuilds the app |
| `uninstall [--client] [--purge-data] [--no-backup]` | see section 10 |

## 7. Where things live

| What | Where |
|---|---|
| CLI | `uv tool` environment (`uv tool dir`), command in `uv tool dir --bin` |
| Host config | `~/.config/wispralt/.env` (0600) and `install-state.json` (what setup created) |
| Host data | `~/Library/Application Support/WisprAlt/host` (0700): `wispralt.db`, `outputs/`, `staging/` |
| Models | `~/Library/Application Support/WisprAlt/models` (`HF_HOME`) |
| Logs | `~/Library/Logs/WisprAlt/` (`wispralt.log`; `server.log` / `server.err.log` hold crash output) |
| Backups | `~/WisprAlt-Backups/` |
| LaunchAgent | `~/Library/LaunchAgents/co.wispralt.host.plist` (no secrets in it) |
| App | `/Applications/WisprAlt.app` |
| App settings | `defaults read co.wispralt.WisprAlt` (`serverURL`, `mode`) |
| App keys | Keychain items `co.wispralt` (Local), `co.wispralt.openai`, `co.wispralt.openrouter` |

## 8. Connecting other Macs

The host listens on `127.0.0.1` only. To reach it from other Macs, publish it
with `tailscale serve` (or the optional Cloudflare tunnel,
`scripts/setup-cloudflared.sh`), set `SERVER_URL` in the host `.env` to that
URL, and run `wispralt restart`. Then on each Mac run `./install.sh --role
client`, and in the app's Settings enter the server URL and a key made with
`wispralt users add NAME` (or the portal's **Add employee**).

## 9. Backups, restore and retention

- `wispralt backup` stops the service, writes a verified snapshot (database,
  outputs, staging and a manifest with hashes and row counts) and restarts it.
- `wispralt restore DIR --yes` checks the backup against its manifest before
  touching anything, keeps the old data aside and restarts the service. `status`
  lists the kept copies; delete them yourself once you are sure
  (`rm -rf "<path>"`). The CLI never deletes a backup or a kept copy.
- Retention is a database setting: 0 keeps everything forever. A daily purge
  removes older dictations and finished file jobs; imported history is never
  purged.

## 10. Updating and uninstalling

`wispralt update` is the whole update: it pulls the clone (fast-forward only),
backs up, reinstalls the CLI, restarts the service and waits for it to be
healthy. If the new version does not come up healthy it exits 1 and prints the
`wispralt restore` command for the backup it just took. If a step after the
reinstall fails (e.g. model verification), it still starts the service and says
whether it came back. Add `--client` to
rebuild and reinstall the app. (The app's own **Update** button still points at
the retired release installer; use `wispralt update --client` until it is
retargeted.)

`wispralt uninstall` removes only what setup recorded: the LaunchAgent, the
models and the logs, and finally the CLI itself. Your data, `.env` and backups
stay.

- `--purge-data` also deletes WisprAlt's data (`wispralt.db*`, `outputs/`,
  `staging/`, `setup-code`) and `.env`, after taking a backup (skip it with
  `--no-backup`; with no database it copies the files to
  `wispralt-<ts>-files`). Other files in the data directory are kept, and the
  directory goes only if it is then empty. It refuses when there is no
  `install-state.json` or when the backup directory is inside the data
  directory.
- `--client` moves the app to the Trash. It never deletes your transcripts
  folder or the Keychain items.

## 11. Troubleshooting

Start with `wispralt doctor` (add `--client` on a client). Each failing line
ends with its fix; [TROUBLESHOOTING.md](TROUBLESHOOTING.md#wispralt-doctor)
explains every check. Two install-specific cases:

- **"Apple cannot check it for malicious software"** on a locally built app:
  right-click `/Applications/WisprAlt.app` in Finder, choose **Open**, then
  **Open Anyway**.
- **The menubar icon does not appear**: quit and reopen the app
  (`pkill -x WisprAlt; open /Applications/WisprAlt.app`). If it persists, attach
  `~/Library/Logs/WisprAlt/` to an issue.
