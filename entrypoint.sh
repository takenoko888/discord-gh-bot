#!/bin/bash
set -e

echo "=== Downloading Copilot CLI ==="
gh copilot -- --version 2>&1 || echo "Warning: Copilot CLI download failed (will retry on first use)"

echo "=== Starting bot ==="
exec python bot.py
