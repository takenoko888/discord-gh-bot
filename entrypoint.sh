#!/bin/bash
set -e

# Authenticate gh CLI with the token
if [ -n "$GH_TOKEN" ]; then
    echo "=== Authenticating gh CLI ==="
    echo "$GH_TOKEN" | gh auth login --with-token
    gh auth status
fi

# Download Copilot CLI
echo "=== Downloading Copilot CLI ==="
gh copilot -- --version 2>&1 || echo "Warning: Copilot CLI download failed"

echo "=== Starting bot ==="
exec python bot.py
