#!/bin/bash

# Install GitHub Copilot CLI (required for gh copilot command)
# gh copilot looks for 'copilot' binary in PATH or ~/.local/share/gh/copilot/copilot
COPILOT_DIR="$HOME/.local/share/gh/copilot"
COPILOT_BIN="$COPILOT_DIR/copilot"

if [ ! -f "$COPILOT_BIN" ]; then
    echo "=== Installing GitHub Copilot CLI ==="
    mkdir -p "$COPILOT_DIR"
    ARCH=$(uname -m)
    [ "$ARCH" = "aarch64" ] && ARCH_NAME="arm64" || ARCH_NAME="x64"
    ARCHIVE_URL="https://github.com/github/copilot-cli/releases/latest/download/copilot-linux-${ARCH_NAME}.tar.gz"
    echo "Downloading: $ARCHIVE_URL"
    TMPFILE=$(mktemp)
    if curl -sL "$ARCHIVE_URL" -o "$TMPFILE" && [ -s "$TMPFILE" ]; then
        tar -xzf "$TMPFILE" -C "$COPILOT_DIR"
        rm -f "$TMPFILE"
        chmod +x "$COPILOT_BIN" 2>/dev/null || true
        [ -f "$COPILOT_BIN" ] && echo "✅ Copilot CLI installed" || { echo "⚠️ Binary not found, contents:"; ls "$COPILOT_DIR"; }
    else
        echo "⚠️ Download failed (status: $?)"
        rm -f "$TMPFILE"
    fi
fi

echo "=== Starting bot ==="
exec python bot.py
