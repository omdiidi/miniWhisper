#!/usr/bin/env bash
# server-launchd.sh — Manage the WisprAlt FastAPI server as a macOS LaunchAgent.
#
# Subcommands:
#   install         Generate the .plist, create log directories, and load the agent.
#   start           Load the agent (same as install if not yet installed).
#   stop            Unload the agent (service stops; plist stays in place).
#   status          Report whether the agent is loaded and the process PID.
#   uninstall       Unload and remove the plist file.
#   bootstrap-test  Simulate reboot-survival: bootout → bootstrap → poll /healthz.
#
# LaunchAgent label: co.wispralt.server
# Plist path:        ~/Library/LaunchAgents/co.wispralt.server.plist
# Log path:          ~/Library/Logs/WisprAlt/server.{log,err.log}
#
# Security: no secrets are written to the plist.
# The .env file (mode 0600) is loaded by Pydantic Settings at uvicorn startup.
#
# Usage: ./scripts/server-launchd.sh <install|start|stop|status|uninstall|bootstrap-test>

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

LABEL="co.wispralt.server"
PLIST_DIR="$HOME/Library/LaunchAgents"
PLIST_PATH="$PLIST_DIR/$LABEL.plist"
LOG_DIR="$HOME/Library/Logs/WisprAlt"
LOG_OUT="$LOG_DIR/server.log"
LOG_ERR="$LOG_DIR/server.err.log"
UVICORN="$REPO_ROOT/server/.venv/bin/uvicorn"

# ── Helpers ───────────────────────────────────────────────────────────────────
is_loaded() {
    launchctl list 2>/dev/null | grep -q "$LABEL"
}

require_uvicorn() {
    if [[ ! -f "$UVICORN" ]]; then
        echo "ERROR: uvicorn not found at $UVICORN" >&2
        echo "  Run setup-server.sh (or: cd server && uv venv && uv sync) first." >&2
        exit 1
    fi
}

generate_plist() {
    require_uvicorn
    mkdir -p "$PLIST_DIR" "$LOG_DIR"

    # PATH includes Homebrew on Apple Silicon by default; adjust if on Intel
    BREW_PREFIX="$(brew --prefix 2>/dev/null || echo "/opt/homebrew")"
    LAUNCH_PATH="/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$BREW_PREFIX/bin"

    cat > "$PLIST_PATH" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
    "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <!-- Label -->
    <key>Label</key>
    <string>${LABEL}</string>

    <!-- Program and arguments -->
    <key>ProgramArguments</key>
    <array>
        <string>${UVICORN}</string>
        <string>wispralt_server.main:app</string>
        <string>--host</string>
        <string>127.0.0.1</string>
        <string>--port</string>
        <string>8000</string>
        <string>--workers</string>
        <string>1</string>
    </array>

    <!-- Working directory so relative .env paths resolve correctly -->
    <key>WorkingDirectory</key>
    <string>${REPO_ROOT}/server</string>

    <!-- Environment variables: PATH only; secrets are in .env (Pydantic Settings) -->
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>${LAUNCH_PATH}</string>
    </dict>

    <!-- Keep the process alive; only restart on unexpected exits -->
    <key>KeepAlive</key>
    <dict>
        <!-- true = restart unless the process exits successfully (exit code 0) -->
        <key>SuccessfulExit</key>
        <false/>
    </dict>

    <!-- Minimum seconds between restarts — prevents rapid restart loops -->
    <key>ThrottleInterval</key>
    <integer>30</integer>

    <!-- Grace period for SIGTERM before SIGKILL (P5#5) -->
    <key>ExitTimeOut</key>
    <integer>15</integer>

    <!-- Log files -->
    <key>StandardOutPath</key>
    <string>${LOG_OUT}</string>
    <key>StandardErrorPath</key>
    <string>${LOG_ERR}</string>

    <!-- Run as the current user (LaunchAgent, not LaunchDaemon) -->
    <key>RunAtLoad</key>
    <true/>
</dict>
</plist>
PLIST

    echo "Plist written to: $PLIST_PATH"
}

# ── Subcommands ───────────────────────────────────────────────────────────────
CMD="${1:-help}"

case "$CMD" in
    install)
        echo "Installing WisprAlt server LaunchAgent..."
        generate_plist
        if is_loaded; then
            echo "Agent already loaded — reloading with new plist..."
            launchctl bootout "gui/$UID/$LABEL" 2>/dev/null || true
        fi
        launchctl bootstrap "gui/$UID" "$PLIST_PATH"
        echo "LaunchAgent $LABEL loaded (will start immediately)."
        echo ""
        echo "Logs:"
        echo "  stdout: $LOG_OUT"
        echo "  stderr: $LOG_ERR"
        echo ""
        echo "To check status: ./scripts/server-launchd.sh status"
        ;;

    start)
        if is_loaded; then
            echo "Agent is already loaded. Use 'status' to check, or 'stop' + 'start' to restart."
        else
            if [[ ! -f "$PLIST_PATH" ]]; then
                echo "Plist not found — running install first..."
                generate_plist
            fi
            launchctl bootstrap "gui/$UID" "$PLIST_PATH"
            echo "Agent $LABEL started."
        fi
        ;;

    stop)
        if is_loaded; then
            launchctl bootout "gui/$UID/$LABEL"
            echo "Agent $LABEL stopped (plist retained)."
            echo "  Run 'start' or 'install' to restart."
        else
            echo "Agent $LABEL is not currently loaded."
        fi
        ;;

    status)
        echo "LaunchAgent: $LABEL"
        echo "Plist:       $PLIST_PATH"
        echo ""
        if is_loaded; then
            echo "Status: LOADED"
            # Print PID if the process is running
            PID_LINE="$(launchctl list 2>/dev/null | grep "$LABEL" || true)"
            if [[ -n "$PID_LINE" ]]; then
                PID="$(echo "$PID_LINE" | awk '{print $1}')"
                if [[ "$PID" != "-" && -n "$PID" ]]; then
                    echo "PID:    $PID"
                else
                    echo "PID:    not running (may be throttled after a crash)"
                fi
            fi
        else
            echo "Status: NOT LOADED"
        fi
        echo ""
        echo "Recent log (last 20 lines):"
        if [[ -f "$LOG_OUT" ]]; then
            tail -20 "$LOG_OUT" 2>/dev/null || true
        else
            echo "  (no log file yet)"
        fi
        ;;

    uninstall)
        echo "Uninstalling WisprAlt server LaunchAgent..."
        if is_loaded; then
            launchctl bootout "gui/$UID/$LABEL" 2>/dev/null || true
            echo "Agent unloaded."
        fi
        if [[ -f "$PLIST_PATH" ]]; then
            rm -f "$PLIST_PATH"
            echo "Plist removed: $PLIST_PATH"
        else
            echo "Plist not found (already removed)."
        fi
        echo "Uninstall complete. Logs remain at $LOG_DIR"
        echo "  Remove with: rm -rf \"$LOG_DIR\""
        ;;

    help|--help|-h)
        echo "Usage: server-launchd.sh <install|start|stop|status|uninstall|bootstrap-test>"
        echo ""
        echo "  install         Generate plist, create logs dir, load agent"
        echo "  start           Load agent (uses existing plist)"
        echo "  stop            Unload agent (plist stays)"
        echo "  status          Show agent load state and recent log"
        echo "  uninstall       Unload and delete plist"
        echo "  bootstrap-test  Simulate reboot-survival: bootout → bootstrap → poll /healthz"
        ;;

    bootstrap-test)
        # Simulate reboot-survival without an actual reboot.
        launchctl bootout "gui/$UID/$LABEL" 2>/dev/null || true
        launchctl bootstrap "gui/$UID" "$PLIST_PATH"
        echo "Polling /healthz..."
        if curl --max-time 5 --retry 6 --retry-delay 2 \
                -fsS http://127.0.0.1:8000/healthz >/dev/null; then
            echo "server reboot-survival OK"
        else
            echo "server failed to come up — check $LOG_DIR/server.err.log" >&2
            exit 1
        fi
        ;;

    *)
        echo "ERROR: Unknown subcommand '$CMD'" >&2
        echo "Usage: server-launchd.sh <install|start|stop|status|uninstall|bootstrap-test>" >&2
        exit 1
        ;;
esac
