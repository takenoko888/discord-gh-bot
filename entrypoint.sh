#!/bin/bash

# Install gh-copilot extension manually (gh extension install fails due to built-in name conflict)
EXT_DIR="$HOME/.local/share/gh/extensions/gh-copilot"

install_copilot() {
    rm -rf "$EXT_DIR"
    mkdir -p "$EXT_DIR"
    ARCH=$(uname -m)
    [ "$ARCH" = "aarch64" ] && ARCH_NAME="arm64" || ARCH_NAME="amd64"

    RELEASE_JSON=$(curl -s -H "Authorization: Bearer $GH_TOKEN" \
        "https://api.github.com/repos/github/gh-copilot/releases/latest")

    ASSET_NAME=$(echo "$RELEASE_JSON" | python3 -c "
import sys, json
data = json.load(sys.stdin)
for asset in data.get('assets', []):
    n = asset['name'].lower()
    if 'linux' in n and '$ARCH_NAME' in n:
        print(asset['name']); break
" 2>/dev/null)

    DOWNLOAD_URL=$(echo "$RELEASE_JSON" | python3 -c "
import sys, json
data = json.load(sys.stdin)
for asset in data.get('assets', []):
    n = asset['name'].lower()
    if 'linux' in n and '$ARCH_NAME' in n:
        print(asset['browser_download_url']); break
" 2>/dev/null)

    echo "Asset: $ASSET_NAME  URL: $DOWNLOAD_URL"

    if [ -z "$DOWNLOAD_URL" ]; then
        echo "⚠️ No release found. API: $(echo "$RELEASE_JSON" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('message',''))" 2>/dev/null)"
        return 1
    fi

    TMPFILE=$(mktemp)
    curl -sL -H "Authorization: Bearer $GH_TOKEN" "$DOWNLOAD_URL" -o "$TMPFILE"

    if echo "$ASSET_NAME" | grep -q "\.tar\.gz$"; then
        tar -xzf "$TMPFILE" -C "$EXT_DIR"
        # Move the extracted binary to the expected name
        BIN=$(find "$EXT_DIR" -maxdepth 2 -type f -executable | head -1)
        [ -n "$BIN" ] && mv "$BIN" "$EXT_DIR/gh-copilot" 2>/dev/null || true
    else
        mv "$TMPFILE" "$EXT_DIR/gh-copilot"
    fi
    rm -f "$TMPFILE"
    chmod +x "$EXT_DIR/gh-copilot"
    echo "File type: $(file "$EXT_DIR/gh-copilot" | cut -d: -f2)"
}

if ! "$EXT_DIR/gh-copilot" --version >/dev/null 2>&1; then
    echo "=== Installing gh-copilot extension ==="
    install_copilot && echo "✅ gh-copilot installed" || echo "⚠️ gh-copilot install failed"
fi

# Always start the bot (gh uses GH_TOKEN env var for auth automatically)
echo "=== Starting bot ==="
exec python bot.py
