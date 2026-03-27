#!/usr/bin/env bash
# hex-events install script
# Idempotent — safe to run multiple times.
# Usage: bash install.sh   OR   ./install.sh
set -euo pipefail

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
HEX_HOME="$HOME/.hex-events"
VENV_DIR="$SCRIPT_DIR/venv"

echo "==> hex-events install"
echo "    source dir : $SCRIPT_DIR"
echo "    hex home   : $HEX_HOME"

# ---------------------------------------------------------------------------
# Step 1: Link ~/.hex-events -> repo dir so daemon's hardcoded paths resolve
# ---------------------------------------------------------------------------
if [ -L "$HEX_HOME" ]; then
    existing_target="$(readlink "$HEX_HOME")"
    if [ "$existing_target" = "$SCRIPT_DIR" ]; then
        echo "==> $HEX_HOME already symlinked to $SCRIPT_DIR"
    else
        echo "WARNING: $HEX_HOME points to $existing_target (not $SCRIPT_DIR)"
        echo "         Updating symlink..."
        rm "$HEX_HOME"
        ln -s "$SCRIPT_DIR" "$HEX_HOME"
        echo "==> Updated: $HEX_HOME -> $SCRIPT_DIR"
    fi
elif [ -d "$HEX_HOME" ]; then
    # Resolve in case it's actually the same physical dir (e.g., install ran before)
    real_hex="$(cd "$HEX_HOME" && pwd -P 2>/dev/null || echo "")"
    real_script="$(cd "$SCRIPT_DIR" && pwd -P 2>/dev/null || echo "")"
    if [ "$real_hex" = "$real_script" ]; then
        echo "==> $HEX_HOME is the install directory"
    else
        echo "==> $HEX_HOME exists as a directory (not this repo)."
        echo "    Daemon will use files from $HEX_HOME."
        echo "    To use this repo instead, remove $HEX_HOME and re-run."
    fi
else
    echo "==> Creating symlink: $HEX_HOME -> $SCRIPT_DIR"
    ln -s "$SCRIPT_DIR" "$HEX_HOME"
fi

# ---------------------------------------------------------------------------
# Step 2: Create Python venv
# ---------------------------------------------------------------------------
if [ ! -d "$VENV_DIR" ]; then
    echo "==> Creating venv at $VENV_DIR..."
    python3 -m venv "$VENV_DIR"
else
    echo "==> venv already exists"
fi

# ---------------------------------------------------------------------------
# Step 3: Install pip dependencies
# ---------------------------------------------------------------------------
echo "==> Installing pip dependencies..."
"$VENV_DIR/bin/pip" install --quiet -r "$SCRIPT_DIR/requirements.txt"
echo "==> Dependencies installed"

# ---------------------------------------------------------------------------
# Step 4: Create required subdirectories
# ---------------------------------------------------------------------------
for dir in recipes policies adapters docs; do
    mkdir -p "$SCRIPT_DIR/$dir"
done
echo "==> Subdirectories ready (recipes/ policies/ adapters/ docs/)"

# ---------------------------------------------------------------------------
# Step 5: Initialize SQLite database
# ---------------------------------------------------------------------------
echo "==> Initializing database..."
cd "$SCRIPT_DIR" && "$VENV_DIR/bin/python3" -c "from db import EventsDB; EventsDB('./events.db')"
echo "==> Database ready"

# ---------------------------------------------------------------------------
# Step 6/7: Platform-specific service setup
# ---------------------------------------------------------------------------
OS="$(uname -s)"

if [ "${HEX_EVENTS_NO_LAUNCHCTL:-0}" = "1" ]; then
    echo "==> HEX_EVENTS_NO_LAUNCHCTL=1 — skipping service installation"
elif [ "$OS" = "Darwin" ]; then
    LAUNCH_AGENTS_DIR="$HOME/Library/LaunchAgents"
    mkdir -p "$LAUNCH_AGENTS_DIR"

    # ---- Daemon plist ----
    DAEMON_PLIST="$LAUNCH_AGENTS_DIR/com.mrap.hex-eventd.plist"
    cat > "$DAEMON_PLIST" << PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.mrap.hex-eventd</string>
    <key>ProgramArguments</key>
    <array>
        <string>${VENV_DIR}/bin/python3</string>
        <string>${SCRIPT_DIR}/hex_eventd.py</string>
    </array>
    <key>KeepAlive</key>
    <true/>
    <key>RunAtLoad</key>
    <true/>
    <key>WorkingDirectory</key>
    <string>${SCRIPT_DIR}</string>
    <key>StandardOutPath</key>
    <string>${SCRIPT_DIR}/daemon.log</string>
    <key>StandardErrorPath</key>
    <string>${SCRIPT_DIR}/daemon.log</string>
</dict>
</plist>
PLIST_EOF

    if launchctl list 2>/dev/null | grep -q "com.mrap.hex-eventd"; then
        echo "==> Daemon LaunchAgent already loaded (skipping launchctl load)"
    else
        launchctl load "$DAEMON_PLIST" && echo "==> Daemon LaunchAgent loaded"
    fi

    # ---- Watchdog plist ----
    WATCHDOG_PLIST="$LAUNCH_AGENTS_DIR/com.mrap.hex-watchdog.plist"
    cat > "$WATCHDOG_PLIST" << PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.mrap.hex-watchdog</string>
    <key>StartInterval</key>
    <integer>60</integer>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>${SCRIPT_DIR}/scripts/hex-watchdog.sh</string>
    </array>
    <key>StandardErrorPath</key>
    <string>${SCRIPT_DIR}/watchdog-stderr.log</string>
    <key>RunAtLoad</key>
    <true/>
</dict>
</plist>
PLIST_EOF

    if launchctl list 2>/dev/null | grep -q "com.mrap.hex-watchdog"; then
        echo "==> Watchdog LaunchAgent already loaded (skipping launchctl load)"
    else
        launchctl load "$WATCHDOG_PLIST" && echo "==> Watchdog LaunchAgent loaded"
    fi

    # ---- fswatch plist (optional) ----
    if command -v fswatch >/dev/null 2>&1; then
        FSWATCH_BIN="$(command -v fswatch)"
        FSWATCH_PLIST="$LAUNCH_AGENTS_DIR/com.mrap.hex-events-fswatch.plist"

        # HEX_WATCH_DIR: the directory fswatch monitors for new capture files.
        # fswatch emits a file.created event for each new file dropped here,
        # allowing hex-events to ingest raw capture data automatically.
        # Override by setting HEX_WATCH_DIR before running install.sh.
        if [ -z "${HEX_WATCH_DIR:-}" ]; then
            HEX_WATCH_DIR="${HOME}/hex/raw/captures/"
            echo "==> HEX_WATCH_DIR not set — defaulting to $HEX_WATCH_DIR"
            echo "    WARNING: This path may not exist. Set HEX_WATCH_DIR to override."
        else
            echo "==> HEX_WATCH_DIR=$HEX_WATCH_DIR"
        fi

        # Note: $file inside the fswatch command must remain a shell variable at
        # runtime, so we escape it in the heredoc with a backslash.
        cat > "$FSWATCH_PLIST" << PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.mrap.hex-events-fswatch</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>-c</string>
        <string>${FSWATCH_BIN} -0 --event Created ${HEX_WATCH_DIR} | while read -d '' file; do ${VENV_DIR}/bin/python3 ${SCRIPT_DIR}/hex_emit.py file.created "{\"path\":\"\$file\"}" fswatch; done</string>
    </array>
    <key>KeepAlive</key>
    <true/>
    <key>RunAtLoad</key>
    <true/>
    <key>StandardOutPath</key>
    <string>${SCRIPT_DIR}/fswatch.log</string>
    <key>StandardErrorPath</key>
    <string>${SCRIPT_DIR}/fswatch.log</string>
</dict>
</plist>
PLIST_EOF

        if launchctl list 2>/dev/null | grep -q "com.mrap.hex-events-fswatch"; then
            echo "==> fswatch LaunchAgent already loaded (skipping)"
        else
            launchctl load "$FSWATCH_PLIST" && echo "==> fswatch LaunchAgent loaded"
        fi
    else
        echo "==> fswatch not found — skipping fswatch LaunchAgent (install via: brew install fswatch)"
    fi

elif [ "$OS" = "Linux" ]; then
    echo "==> systemd setup not yet supported, start manually with:"
    echo "    $VENV_DIR/bin/python3 $SCRIPT_DIR/hex_eventd.py"
else
    echo "==> Unknown OS ($OS) — skipping service installation"
    echo "    Start manually: $VENV_DIR/bin/python3 $SCRIPT_DIR/hex_eventd.py"
fi

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
echo ""
echo "hex-events installed successfully."
echo "  Start daemon : $VENV_DIR/bin/python3 $SCRIPT_DIR/hex_eventd.py"
echo "  Emit event   : $VENV_DIR/bin/python3 $SCRIPT_DIR/hex_emit.py <type> [payload] [source]"
echo "  Query events : $VENV_DIR/bin/python3 $SCRIPT_DIR/hex_events_cli.py status"
