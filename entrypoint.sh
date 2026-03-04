#!/bin/bash

# Install gh-copilot extension manually (gh extension install fails due to built-in name conflict)
EXT_DIR="$HOME/.local/share/gh/extensions/gh-copilot"
if [ ! -f "$EXT_DIR/gh-copilot" ]; then
    echo "=== Installing gh-copilot extension ==="
    mkdir -p "$EXT_DIR"
    ARCH=$(uname -m)
    [ "$ARCH" = "aarch64" ] && ARCH_NAME="arm64" || ARCH_NAME="amd64"

    RELEASE_INFO=$(curl -s -H "Authorization: Bearer $GH_TOKEN" \
        "https://api.github.com/repos/github/gh-copilot/releases/latest")

    DOWNLOAD_URL=$(echo "$RELEASE_INFO" | python3 -c "
import sys, json
data = json.load(sys.stdin)
for asset in data.get('assets', []):
    n = asset['name'].lower()
    if 'linux' in n and '$ARCH_NAME' in n:
        print(asset['browser_download_url'])
        break
" 2>/dev/null)

    if [ -n "$DOWNLOAD_URL" ]; then
        curl -sL -H "Authorization: Bearer $GH_TOKEN" "$DOWNLOAD_URL" -o "$EXT_DIR/gh-copilot"
        chmod +x "$EXT_DIR/gh-copilot"
        echo "✅ gh-copilot installed"
    else
        echo "⚠️ gh-copilot install failed (Copilot subscription required or token lacks access)"
    fi
fi

# Always start the bot (gh uses GH_TOKEN env var for auth automatically)
echo "=== Starting bot ==="
exec python bot.py
