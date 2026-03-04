#!/bin/bash

# Always start the bot (gh uses GH_TOKEN env var for auth automatically)
echo "=== Starting bot ==="
exec python bot.py
