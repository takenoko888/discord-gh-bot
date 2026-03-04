"""Shared configuration and constants."""

import os
from dotenv import load_dotenv

load_dotenv()

DISCORD_TOKEN: str = os.environ["DISCORD_TOKEN"]
GH_TOKEN: str = os.environ.get("GH_TOKEN", "")
ALLOWED_ROLE_NAME: str = os.environ.get("ALLOWED_ROLE_NAME", "gh-bot")

GOOGLE_API_KEY: str = os.environ.get("GOOGLE_API_KEY", "")
GOOGLE_CSE_ID: str = os.environ.get("GOOGLE_CSE_ID", "")

AVAILABLE_MODELS = [
    "gpt-4o", "gpt-4o-mini", "gpt-4.1",
    "claude-sonnet-4.6", "claude-haiku-4.5",
    "Mistral-large-2411",
]
DEFAULT_MODEL = os.environ.get("COPILOT_MODEL", "gpt-4o")

BLOCKED_SUBCOMMANDS = {"auth", "config"}
BLOCKED_GIT_PATTERNS = [
    lambda a: a[0] == "reset" and "--hard" in a,
    lambda a: a[0] == "push" and ("--force" in a or "-f" in a),
    lambda a: a[0] == "clean" and ("-fd" in a or ("-f" in a and "-d" in a)),
]

MAX_OUTPUT_LENGTH = 1900
MAX_HISTORY = 30
HISTORY_TTL = 3600
GH_TIMEOUT = 30
GIT_TIMEOUT = 60
AGENT_TIMEOUT = 120
MAX_TOOL_ROUNDS = 10
MODELS_API_URL = "https://models.inference.ai.azure.com/chat/completions"
GITHUB_API = "https://api.github.com"

SYSTEM_PROMPT = """\
あなたはDiscord上で動作するAIエージェントです。日本語で回答してください。
ユーザーの指示を達成するために、必要なツールを自分で判断して実行してください。
複数のステップが必要な場合は、順番にツールを呼び出して自律的に作業を完了させてください。

例:
- 「このリポジトリのbot.pyを読んで改善して」→ read_file → 分析 → push_file
- 「新しいPythonスクリプトを作ってGistに保存して」→ create_gist
- 「リポジトリ一覧を見せて」→ run_gh

- 「最新ニュースを調べて」→ web_search → 要約
- 「○○について調べてまとめて」→ web_search → 結果を整理して報告

作業が完了したら、結果をわかりやすく報告してください。
コードを見せる場合は```で囲んでください。"""
