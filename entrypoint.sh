#!/bin/bash

# Authenticate gh CLI with the token (non-fatal)
if [ -n "$GH_TOKEN" ]; then
    echo "=== Authenticating gh CLI ==="
    echo "$GH_TOKEN" | gh auth login --with-token 2>&1 || echo "Warning: gh auth login failed"
    gh auth status 2>&1 || true
fi

# Download Copilot CLI (non-fatal)
echo "=== Downloading Copilot CLI ==="
gh copilot -- --version 2>&1 || echo "Warning: Copilot CLI download failed (copilot commands will not work)"

# Always start the bot
echo "=== Starting bot ==="
exec python bot.py
