#!/bin/bash

# Install gh-copilot extension if not already installed
if ! gh extension list 2>/dev/null | grep -q "gh-copilot"; then
    echo "=== Installing gh-copilot extension ==="
    gh extension install github/gh-copilot 2>&1 || echo "Warning: gh-copilot install failed"
fi

# Always start the bot (gh uses GH_TOKEN env var for auth automatically)
echo "=== Starting bot ==="
exec python bot.py
