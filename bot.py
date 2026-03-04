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
import json
import asyncio
import shlex
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import aiohttp
import discord
from discord import app_commands
from dotenv import load_dotenv

load_dotenv()

DISCORD_TOKEN: str = os.environ["DISCORD_TOKEN"]
GH_TOKEN: str = os.environ.get("GH_TOKEN", "")
ALLOWED_ROLE_NAME: str = os.environ.get("ALLOWED_ROLE_NAME", "gh-bot")
COPILOT_MODEL: str = os.environ.get("COPILOT_MODEL", "gpt-4o")

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

async def has_allowed_role(interaction: discord.Interaction) -> tuple[bool, str]:
    """Returns (allowed, debug_info)."""
    member = interaction.user if isinstance(interaction.user, discord.Member) else interaction.member

    # If member data is missing, try fetching from the guild
    if member is None and interaction.guild is not None:
        try:
            member = await interaction.guild.fetch_member(interaction.user.id)
        except Exception:
            pass

    if member is None:
        return False, "member=None"

    role_names = [r.name for r in member.roles]
    found = ALLOWED_ROLE_NAME in role_names
    debug = f"user={member}, roles={role_names}, looking_for='{ALLOWED_ROLE_NAME}'"
    print(debug)
    return found, debug


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


MODELS_API_URL = "https://models.inference.ai.azure.com/chat/completions"


async def run_copilot(prompt: str) -> tuple[str, int]:
    """Call GitHub Models API directly (no gh copilot CLI needed)."""
    if not GH_TOKEN:
        return "GH_TOKEN が設定されていません。", 1

    headers = {
        "Authorization": f"Bearer {GH_TOKEN}",
        "Content-Type": "application/json",
    }
    body = {
        "model": COPILOT_MODEL,
        "messages": [
            {"role": "system", "content": "あなたは優秀なプログラミングアシスタントです。日本語で回答してください。"},
            {"role": "user", "content": prompt},
        ],
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                MODELS_API_URL, headers=headers, json=body, timeout=aiohttp.ClientTimeout(total=COPILOT_TIMEOUT)
            ) as resp:
                data = await resp.json()
                if resp.status == 200:
                    content = data.get("choices", [{}])[0].get("message", {}).get("content", "(応答なし)")
                    return content, 0
                else:
                    error_msg = data.get("error", {}).get("message", json.dumps(data, ensure_ascii=False))
                    return f"API エラー ({resp.status}): {error_msg}", 1
    except asyncio.TimeoutError:
        return "タイムアウトしました。", 1
    except Exception as e:
        return f"リクエスト失敗: {e}", 1


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
    await interaction.response.defer()

    allowed, debug = await has_allowed_role(interaction)
    if not allowed:
        await interaction.followup.send(
            f"❌ このコマンドを実行するには **{ALLOWED_ROLE_NAME}** ロールが必要です。\n`{debug}`",
        )
        return

    try:
        args = shlex.split(command)
    except ValueError as e:
        await interaction.followup.send(f"❌ コマンドの解析失敗: {e}")
        return

    ok, reason = validate_args(args)
    if not ok:
        await interaction.followup.send(f"❌ {reason}")
        return
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
    await interaction.response.defer()

    allowed, debug = await has_allowed_role(interaction)
    if not allowed:
        await interaction.followup.send(
            f"❌ このコマンドを実行するには **{ALLOWED_ROLE_NAME}** ロールが必要です。\n`{debug}`",
        )
        return

    try:
        args = shlex.split(command)
    except ValueError as e:
        await interaction.followup.send(f"❌ コマンドの解析失敗: {e}")
        return

    ok, reason = validate_git_args(args)
    if not ok:
        await interaction.followup.send(f"❌ {reason}")
        return
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
    await interaction.response.defer()

    allowed, debug = await has_allowed_role(interaction)
    if not allowed:
        await interaction.followup.send(
            f"❌ このコマンドを実行するには **{ALLOWED_ROLE_NAME}** ロールが必要です。\n`{debug}`",
        )
        return

    if not prompt.strip():
        await interaction.followup.send("❌ 質問を入力してください。")
        return
    output, returncode = await run_copilot(prompt.strip())

    success = returncode == 0
    display_output = truncate(output) if output else "(応答なし)"

    embed = discord.Embed(
        title="🤖 Copilot" if success else "❌ Copilot",
        description=f"**Q:** {prompt[:100]}{'…' if len(prompt) > 100 else ''}\n\n{display_output}",
        color=discord.Color.blue() if success else discord.Color.red(),
    )
    embed.set_footer(text=f"model: {COPILOT_MODEL}  |  実行者: {interaction.user}")
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
