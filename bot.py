"""
Discord Bot that executes gh, git, and gh copilot commands.
Only members with the allowed role can run commands.

Usage:
  /gh <command>      — e.g. /gh repo list --limit 5
  /git <command>     — e.g. /git push origin main
  /copilot <prompt>  — e.g. /copilot PythonでHello Worldを書いて

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

# Git: block dangerous operations (reset --hard, push --force, clean -fd)
BLOCKED_GIT_PATTERNS = [
    lambda args: args[0] == "reset" and "--hard" in args,
    lambda args: args[0] == "push" and ("--force" in args or "-f" in args),
    lambda args: args[0] == "clean" and ("-fd" in args or ("-f" in args and "-d" in args)),
]

MAX_OUTPUT_LENGTH = 1900  # Discord message hard-limit is 2000
GH_TIMEOUT = 30
GIT_TIMEOUT = 60
COPILOT_TIMEOUT = 90  # Copilot can take a while


# ── Bot setup ──────────────────────────────────────────────────────────────────

class GhBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.members = True  # ロール判定に必要
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
    member = interaction.user if isinstance(interaction.user, discord.Member) else interaction.member
    if member is None:
        return False
    return any(r.name == ALLOWED_ROLE_NAME for r in member.roles)


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
    stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=GH_TIMEOUT)
    return stdout.decode("utf-8", errors="replace").strip(), proc.returncode


async def run_git(args: list[str]) -> tuple[str, int]:
    """Execute `git <args>` and return (stdout+stderr combined, return-code)."""
    proc = await asyncio.create_subprocess_exec(
        "git", *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        cwd=os.environ.get("GIT_WORK_DIR", "."),
    )
    stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=GIT_TIMEOUT)
    return stdout.decode("utf-8", errors="replace").strip(), proc.returncode


async def run_copilot(prompt: str) -> tuple[str, int]:
    """Execute `gh copilot -p "<prompt>" -s` and return (output, return-code)."""
    proc = await asyncio.create_subprocess_exec(
        "gh", "copilot", "-p", prompt, "-s",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=COPILOT_TIMEOUT)
    return stdout.decode("utf-8", errors="replace").strip(), proc.returncode


def validate_git_args(args: list[str]) -> tuple[bool, str]:
    if not args:
        return False, "コマンドが空です。"
    for pred in BLOCKED_GIT_PATTERNS:
        if pred(args):
            return False, f"`git {' '.join(args)}` は安全のため禁止されています。"
    return True, ""


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
        await interaction.followup.send(f"❌ コマンドがタイムアウトしました（{GH_TIMEOUT}秒）。")
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


@client.tree.command(name="git", description="git コマンドを実行します（例: push origin main）")
@app_commands.describe(command="git に渡す引数（例: push origin main）")
async def git_command(interaction: discord.Interaction, command: str):
    if not has_allowed_role(interaction):
        await interaction.response.send_message(
            f"❌ このコマンドを実行するには **{ALLOWED_ROLE_NAME}** ロールが必要です。",
            ephemeral=True,
        )
        return

    try:
        args = shlex.split(command)
    except ValueError as e:
        await interaction.response.send_message(f"❌ コマンドの解析失敗: {e}", ephemeral=True)
        return

    ok, reason = validate_git_args(args)
    if not ok:
        await interaction.response.send_message(f"❌ {reason}", ephemeral=True)
        return

    await interaction.response.defer()
    try:
        output, returncode = await run_git(args)
    except asyncio.TimeoutError:
        await interaction.followup.send(f"❌ コマンドがタイムアウトしました（{GIT_TIMEOUT}秒）。")
        return
    except FileNotFoundError:
        await interaction.followup.send("❌ `git` コマンドが見つかりません。")
        return
    except Exception as e:
        await interaction.followup.send(f"❌ 予期しないエラー: {e}")
        return

    success = returncode == 0
    display_output = truncate(output) if output else "(出力なし)"

    embed = discord.Embed(
        title=f"{'✅' if success else '❌'}  git {command}",
        description=f"```\n{display_output}\n```",
        color=discord.Color.green() if success else discord.Color.red(),
    )
    embed.set_footer(text=f"終了コード: {returncode}  |  実行者: {interaction.user}")
    await interaction.followup.send(embed=embed)


@client.tree.command(name="copilot", description="GitHub Copilot に質問します（AIと会話・コード生成）")
@app_commands.describe(prompt="質問や依頼（例: PythonでHello Worldを書いて）")
async def copilot_command(interaction: discord.Interaction, prompt: str):
    if not has_allowed_role(interaction):
        await interaction.response.send_message(
            f"❌ このコマンドを実行するには **{ALLOWED_ROLE_NAME}** ロールが必要です。",
            ephemeral=True,
        )
        return

    if not prompt.strip():
        await interaction.response.send_message("❌ 質問を入力してください。", ephemeral=True)
        return

    await interaction.response.defer()
    try:
        output, returncode = await run_copilot(prompt.strip())
    except asyncio.TimeoutError:
        await interaction.followup.send(f"❌ Copilot がタイムアウトしました（{COPILOT_TIMEOUT}秒）。")
        return
    except FileNotFoundError:
        await interaction.followup.send(
            "❌ `gh` コマンドが見つかりません。GitHub CLI をインストールしてください。"
        )
        return
    except Exception as e:
        await interaction.followup.send(f"❌ 予期しないエラー: {e}")
        return

    display_output = truncate(output) if output else "(応答なし)"

    embed = discord.Embed(
        title="🤖 Copilot",
        description=f"**Q:** {prompt[:100]}{'…' if len(prompt) > 100 else ''}\n\n```\n{display_output}\n```",
        color=discord.Color.blue(),
    )
    embed.set_footer(text=f"実行者: {interaction.user}")
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
