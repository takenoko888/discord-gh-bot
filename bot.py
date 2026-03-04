"""
Discord Bot that executes gh (GitHub CLI) commands.
Only members with the allowed role can run commands.

Usage:
  /gh <command>  — e.g. /gh repo list --limit 5

Deploy: Koyeb (Worker or Web Service)
  - Set DISCORD_TOKEN, ALLOWED_ROLE_NAME, GH_TOKEN in Koyeb env vars
  - GH_TOKEN is used by gh CLI for authentication automatically
"""

import os
import asyncio
import shlex
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import discord
from discord import app_commands
from dotenv import load_dotenv

load_dotenv()

DISCORD_TOKEN: str = os.environ["DISCORD_TOKEN"]
ALLOWED_ROLE_NAME: str = os.environ.get("ALLOWED_ROLE_NAME", "gh-bot")

# Subcommands blocked for safety (e.g. would expose credentials)
BLOCKED_SUBCOMMANDS = {"auth", "config"}

MAX_OUTPUT_LENGTH = 1900  # Discord message hard-limit is 2000


# ── Bot setup ──────────────────────────────────────────────────────────────────

class GhBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        await self.tree.sync()
        print("Slash commands synced.")

    async def on_ready(self):
        print(f"Logged in as {self.user}  (ID: {self.user.id})")


client = GhBot()


# ── Helpers ────────────────────────────────────────────────────────────────────

def has_allowed_role(interaction: discord.Interaction) -> bool:
    if isinstance(interaction.user, discord.Member):
        return any(r.name == ALLOWED_ROLE_NAME for r in interaction.user.roles)
    return False


def validate_args(args: list[str]) -> tuple[bool, str]:
    if not args:
        return False, "コマンドが空です。"
    if args[0] in BLOCKED_SUBCOMMANDS:
        return False, f"`gh {args[0]}` は安全のため禁止されています。"
    return True, ""


async def run_gh(args: list[str]) -> tuple[str, int]:
    """Execute `gh <args>` and return (stdout+stderr combined, return-code)."""
    proc = await asyncio.create_subprocess_exec(
        "gh", *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
    return stdout.decode("utf-8", errors="replace").strip(), proc.returncode


def truncate(text: str) -> str:
    if len(text) <= MAX_OUTPUT_LENGTH:
        return text
    return text[:MAX_OUTPUT_LENGTH] + "\n…(出力が長すぎるため省略)"


# ── Slash command ──────────────────────────────────────────────────────────────

@client.tree.command(name="gh", description="gh コマンドを実行します（例: repo list --limit 5）")
@app_commands.describe(command="gh に渡す引数（例: repo list --limit 5）")
async def gh_command(interaction: discord.Interaction, command: str):
    # ① Permission check
    if not has_allowed_role(interaction):
        await interaction.response.send_message(
            f"❌ このコマンドを実行するには **{ALLOWED_ROLE_NAME}** ロールが必要です。",
            ephemeral=True,
        )
        return

    # ② Parse
    try:
        args = shlex.split(command)
    except ValueError as e:
        await interaction.response.send_message(f"❌ コマンドの解析失敗: {e}", ephemeral=True)
        return

    # ③ Validate
    ok, reason = validate_args(args)
    if not ok:
        await interaction.response.send_message(f"❌ {reason}", ephemeral=True)
        return

    # ④ Execute
    await interaction.response.defer()
    try:
        output, returncode = await run_gh(args)
    except asyncio.TimeoutError:
        await interaction.followup.send("❌ コマンドがタイムアウトしました（30秒）。")
        return
    except FileNotFoundError:
        await interaction.followup.send(
            "❌ `gh` コマンドが見つかりません。GitHub CLI をインストールしてください。\n"
            "https://cli.github.com/"
        )
        return
    except Exception as e:
        await interaction.followup.send(f"❌ 予期しないエラー: {e}")
        return

    # ⑤ Reply
    success = returncode == 0
    display_output = truncate(output) if output else "(出力なし)"

    embed = discord.Embed(
        title=f"{'✅' if success else '❌'}  gh {command}",
        description=f"```\n{display_output}\n```",
        color=discord.Color.green() if success else discord.Color.red(),
    )
    embed.set_footer(text=f"終了コード: {returncode}  |  実行者: {interaction.user}")
    await interaction.followup.send(embed=embed)


# ── Health check HTTP server (for Koyeb Web Service health checks) ─────────────

class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, *args):
        pass  # suppress request logs


def _start_health_server():
    port = int(os.environ.get("PORT", 8000))
    server = HTTPServer(("0.0.0.0", port), _HealthHandler)
    print(f"Health check server listening on port {port}")
    server.serve_forever()


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Start health check server in a background thread
    t = threading.Thread(target=_start_health_server, daemon=True)
    t.start()

    client.run(DISCORD_TOKEN)
