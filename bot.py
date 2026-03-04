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


GITHUB_API = "https://api.github.com"


async def generate_code(prompt: str) -> tuple[str, str]:
    """Ask AI to generate code. Returns (code, explanation)."""
    system_msg = (
        "あなたはコード生成専門のアシスタントです。"
        "ユーザーの依頼に基づいてコードを生成してください。"
        "回答は以下のJSON形式で返してください（他の文章は不要）:\n"
        '{"code": "生成したコード", "explanation": "コードの説明（日本語）", "filename": "推奨ファイル名"}'
    )
    headers = {
        "Authorization": f"Bearer {GH_TOKEN}",
        "Content-Type": "application/json",
    }
    body = {
        "model": COPILOT_MODEL,
        "messages": [
            {"role": "system", "content": system_msg},
            {"role": "user", "content": prompt},
        ],
    }

    async with aiohttp.ClientSession() as session:
        async with session.post(
            MODELS_API_URL, headers=headers, json=body, timeout=aiohttp.ClientTimeout(total=COPILOT_TIMEOUT)
        ) as resp:
            data = await resp.json()
            if resp.status != 200:
                error_msg = data.get("error", {}).get("message", str(data))
                raise RuntimeError(f"API エラー ({resp.status}): {error_msg}")
            raw = data["choices"][0]["message"]["content"]
            # Strip markdown code fences if present
            cleaned = raw.strip()
            if cleaned.startswith("```"):
                first_newline = cleaned.index("\n")
                cleaned = cleaned[first_newline + 1:]
            if cleaned.endswith("```"):
                cleaned = cleaned[:-3]
            return cleaned.strip(), raw


async def push_to_github(repo: str, filepath: str, content: str, message: str) -> str:
    """Create or update a file in a GitHub repo via the Contents API. Returns the HTML URL."""
    import base64
    url = f"{GITHUB_API}/repos/{repo}/contents/{filepath}"
    headers = {
        "Authorization": f"Bearer {GH_TOKEN}",
        "Accept": "application/vnd.github+json",
    }

    # Check if file already exists (to get its sha for update)
    sha = None
    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers) as resp:
            if resp.status == 200:
                existing = await resp.json()
                sha = existing.get("sha")

        body = {
            "message": message,
            "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
        }
        if sha:
            body["sha"] = sha

        async with session.put(url, headers=headers, json=body) as resp:
            data = await resp.json()
            if resp.status in (200, 201):
                return data.get("content", {}).get("html_url", f"https://github.com/{repo}")
            else:
                error_msg = data.get("message", str(data))
                raise RuntimeError(f"GitHub API エラー ({resp.status}): {error_msg}")


async def create_gist(filename: str, content: str, description: str) -> str:
    """Create a GitHub Gist. Returns the HTML URL."""
    url = f"{GITHUB_API}/gists"
    headers = {
        "Authorization": f"Bearer {GH_TOKEN}",
        "Accept": "application/vnd.github+json",
    }
    body = {
        "description": description,
        "public": True,
        "files": {filename: {"content": content}},
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(url, headers=headers, json=body) as resp:
            data = await resp.json()
            if resp.status == 201:
                return data.get("html_url", "")
            else:
                error_msg = data.get("message", str(data))
                raise RuntimeError(f"Gist 作成失敗 ({resp.status}): {error_msg}")


@client.tree.command(name="create", description="AIにコードを生成させてGitHubに自動push（Gist or リポジトリ）")
@app_commands.describe(
    prompt="どんなコードを作るか（例: Pythonで電卓アプリを作って）",
    filename="ファイル名（例: calc.py）省略時はAIが決定",
    repo="pushするリポジトリ（例: takenoko888/my-app）省略時はGistに作成",
)
async def create_command(
    interaction: discord.Interaction,
    prompt: str,
    filename: str = "",
    repo: str = "",
):
    await interaction.response.defer()

    allowed, debug = await has_allowed_role(interaction)
    if not allowed:
        await interaction.followup.send(
            f"❌ このコマンドを実行するには **{ALLOWED_ROLE_NAME}** ロールが必要です。\n`{debug}`",
        )
        return

    if not GH_TOKEN:
        await interaction.followup.send("❌ GH_TOKEN が設定されていません。")
        return

    # Step 1: Generate code
    try:
        raw_response, _ = await generate_code(prompt)
    except Exception as e:
        await interaction.followup.send(f"❌ コード生成に失敗: {e}")
        return

    # Parse JSON response from AI
    code = raw_response
    explanation = ""
    ai_filename = "main.py"
    try:
        parsed = json.loads(raw_response)
        code = parsed.get("code", raw_response)
        explanation = parsed.get("explanation", "")
        ai_filename = parsed.get("filename", "main.py")
    except json.JSONDecodeError:
        pass

    final_filename = filename or ai_filename

    # Step 2: Push to GitHub
    try:
        if repo:
            url = await push_to_github(repo, final_filename, code, f"Add {final_filename}: {prompt[:50]}")
            target = f"📁 リポジトリ: `{repo}`"
        else:
            url = await create_gist(final_filename, code, prompt[:100])
            target = "📝 Gist"
    except Exception as e:
        # Push failed, still show the generated code
        embed = discord.Embed(
            title="⚠️ コード生成成功 / Push失敗",
            description=f"**依頼:** {prompt[:100]}\n\n**エラー:** {e}\n\n```\n{truncate(code)}\n```",
            color=discord.Color.orange(),
        )
        embed.set_footer(text=f"実行者: {interaction.user}")
        await interaction.followup.send(embed=embed)
        return

    # Step 3: Reply with success
    code_preview = truncate(code) if len(code) > 800 else code
    desc = f"**依頼:** {prompt[:100]}\n"
    if explanation:
        desc += f"**説明:** {explanation[:200]}\n"
    desc += f"\n{target}\n🔗 {url}\n\n```\n{code_preview}\n```"

    embed = discord.Embed(
        title=f"✅ {final_filename} を作成しました",
        description=desc,
        color=discord.Color.green(),
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
