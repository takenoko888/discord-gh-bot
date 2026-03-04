"""
Discord Bot — GitHub CLI + AI assistant with conversation memory.

Commands:
  /gh <command>      — Run gh CLI commands
  /git <command>     — Run git commands
  /copilot <prompt>  — Chat with AI (remembers conversation per channel)
  /create <prompt>   — AI generates code and pushes to GitHub
  /model <name>      — Switch AI model
  /reset             — Clear conversation history
  /history           — Show conversation summary

Deploy: Koyeb — set DISCORD_TOKEN, GH_TOKEN, ALLOWED_ROLE_NAME
"""

import os
import re
import json
import asyncio
import shlex
import base64
import threading
import time
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, HTTPServer

import aiohttp
import discord
from discord import app_commands
from dotenv import load_dotenv

load_dotenv()

DISCORD_TOKEN: str = os.environ["DISCORD_TOKEN"]
GH_TOKEN: str = os.environ.get("GH_TOKEN", "")
ALLOWED_ROLE_NAME: str = os.environ.get("ALLOWED_ROLE_NAME", "gh-bot")

AVAILABLE_MODELS = [
    "gpt-4o", "gpt-4o-mini", "gpt-4.1",
    "claude-sonnet-4.6", "claude-haiku-4.5",
    "Mistral-large-2411",
]
DEFAULT_MODEL = os.environ.get("COPILOT_MODEL", "gpt-4o")

BLOCKED_SUBCOMMANDS = {"auth", "config"}
BLOCKED_GIT_PATTERNS = [
    lambda args: args[0] == "reset" and "--hard" in args,
    lambda args: args[0] == "push" and ("--force" in args or "-f" in args),
    lambda args: args[0] == "clean" and ("-fd" in args or ("-f" in args and "-d" in args)),
]

MAX_OUTPUT_LENGTH = 1900
MAX_HISTORY = 30  # max messages to remember per channel
HISTORY_TTL = 3600  # seconds before auto-clearing idle conversations
GH_TIMEOUT = 30
GIT_TIMEOUT = 60
COPILOT_TIMEOUT = 90
MODELS_API_URL = "https://models.inference.ai.azure.com/chat/completions"
GITHUB_API = "https://api.github.com"

SYSTEM_PROMPT = (
    "あなたはDiscord上で動作する優秀なプログラミングアシスタントです。日本語で回答してください。\n"
    "ユーザーとの会話を覚えています。前の発言を踏まえて自然に会話してください。\n"
    "コードを生成する場合は ```言語名 で囲んでください。\n"
    "ユーザーが「GitHubに挙げて」「pushして」「Gistに保存して」などと言った場合は、"
    "直前に生成したコードを対象として、以下のJSON行を回答の末尾に追加してください:\n"
    '<!--PUSH:{"filename":"推奨ファイル名","description":"簡潔な説明"}-->\n'
    "この指示タグは自動処理されるため、ユーザーに見せる必要はありません。"
)


# ── Conversation store ─────────────────────────────────────────────────────────

class ConversationStore:
    def __init__(self):
        self._history: dict[int, list[dict]] = defaultdict(list)
        self._models: dict[int, str] = {}
        self._last_code: dict[int, tuple[str, str]] = {}  # channel_id -> (code, filename)
        self._timestamps: dict[int, float] = {}

    def get_model(self, channel_id: int) -> str:
        return self._models.get(channel_id, DEFAULT_MODEL)

    def set_model(self, channel_id: int, model: str):
        self._models[channel_id] = model

    def add_message(self, channel_id: int, role: str, content: str):
        self._history[channel_id].append({"role": role, "content": content})
        if len(self._history[channel_id]) > MAX_HISTORY:
            self._history[channel_id] = self._history[channel_id][-MAX_HISTORY:]
        self._timestamps[channel_id] = time.time()

    def get_messages(self, channel_id: int) -> list[dict]:
        if channel_id in self._timestamps:
            if time.time() - self._timestamps[channel_id] > HISTORY_TTL:
                self.clear(channel_id)
        return [{"role": "system", "content": SYSTEM_PROMPT}] + self._history[channel_id]

    def set_last_code(self, channel_id: int, code: str, filename: str):
        self._last_code[channel_id] = (code, filename)

    def get_last_code(self, channel_id: int) -> tuple[str, str] | None:
        return self._last_code.get(channel_id)

    def clear(self, channel_id: int):
        self._history.pop(channel_id, None)
        self._last_code.pop(channel_id, None)
        self._timestamps.pop(channel_id, None)

    def summary(self, channel_id: int) -> str:
        msgs = self._history.get(channel_id, [])
        model = self.get_model(channel_id)
        return f"モデル: `{model}` | 履歴: {len(msgs)} メッセージ"


store = ConversationStore()


# ── Bot setup ──────────────────────────────────────────────────────────────────

class GhBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.members = True
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        await self.tree.sync()
        print("Global slash commands synced.")

    async def on_ready(self):
        for guild in self.guilds:
            try:
                self.tree.copy_global_to(guild=guild)
                await self.tree.sync(guild=guild)
                print(f"Guild commands synced: {guild.name} ({guild.id})")
            except Exception as e:
                print(f"Failed to sync guild {guild.name}: {e}")
        print(f"Logged in as {self.user}  (ID: {self.user.id})")


client = GhBot()


# ── Helpers ────────────────────────────────────────────────────────────────────

async def has_allowed_role(interaction: discord.Interaction) -> tuple[bool, str]:
    member = interaction.user if isinstance(interaction.user, discord.Member) else interaction.member
    if member is None and interaction.guild is not None:
        try:
            member = await interaction.guild.fetch_member(interaction.user.id)
        except Exception:
            pass
    if member is None:
        return False, "member=None"
    role_names = [r.name for r in member.roles]
    found = ALLOWED_ROLE_NAME in role_names
    return found, f"roles={role_names}, looking_for='{ALLOWED_ROLE_NAME}'"


def validate_args(args: list[str]) -> tuple[bool, str]:
    if not args:
        return False, "コマンドが空です。"
    if args[0] in BLOCKED_SUBCOMMANDS:
        return False, f"`gh {args[0]}` は安全のため禁止されています。"
    return True, ""


def validate_git_args(args: list[str]) -> tuple[bool, str]:
    if not args:
        return False, "コマンドが空です。"
    for pred in BLOCKED_GIT_PATTERNS:
        if pred(args):
            return False, f"`git {' '.join(args)}` は安全のため禁止されています。"
    return True, ""


async def run_command(program: str, args: list[str], timeout: int, cwd: str | None = None) -> tuple[str, int]:
    proc = await asyncio.create_subprocess_exec(
        program, *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, cwd=cwd,
    )
    stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    return stdout.decode("utf-8", errors="replace").strip(), proc.returncode


def truncate(text: str, limit: int = MAX_OUTPUT_LENGTH) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n…(省略)"


def extract_code_blocks(text: str) -> list[tuple[str, str]]:
    """Extract (language, code) pairs from markdown code blocks."""
    pattern = r"```(\w*)\n(.*?)```"
    return re.findall(pattern, text, re.DOTALL)


def extract_push_directive(text: str) -> dict | None:
    match = re.search(r"<!--PUSH:(.*?)-->", text)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass
    return None


def clean_response(text: str) -> str:
    return re.sub(r"<!--PUSH:.*?-->", "", text).strip()


# ── AI API call ────────────────────────────────────────────────────────────────

async def call_ai(messages: list[dict], model: str) -> tuple[str, int]:
    if not GH_TOKEN:
        return "GH_TOKEN が設定されていません。", 1

    headers = {"Authorization": f"Bearer {GH_TOKEN}", "Content-Type": "application/json"}
    body = {"model": model, "messages": messages}

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                MODELS_API_URL, headers=headers, json=body,
                timeout=aiohttp.ClientTimeout(total=COPILOT_TIMEOUT),
            ) as resp:
                data = await resp.json()
                if resp.status == 200:
                    return data["choices"][0]["message"]["content"], 0
                error_msg = data.get("error", {}).get("message", json.dumps(data, ensure_ascii=False))
                return f"API エラー ({resp.status}): {error_msg}", 1
    except asyncio.TimeoutError:
        return "タイムアウトしました。", 1
    except Exception as e:
        return f"リクエスト失敗: {e}", 1


# ── GitHub helpers ─────────────────────────────────────────────────────────────

async def push_to_github(repo: str, filepath: str, content: str, message: str) -> str:
    url = f"{GITHUB_API}/repos/{repo}/contents/{filepath}"
    headers = {"Authorization": f"Bearer {GH_TOKEN}", "Accept": "application/vnd.github+json"}

    sha = None
    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers) as resp:
            if resp.status == 200:
                sha = (await resp.json()).get("sha")

        body = {"message": message, "content": base64.b64encode(content.encode()).decode("ascii")}
        if sha:
            body["sha"] = sha

        async with session.put(url, headers=headers, json=body) as resp:
            data = await resp.json()
            if resp.status in (200, 201):
                return data.get("content", {}).get("html_url", f"https://github.com/{repo}")
            raise RuntimeError(f"GitHub API エラー ({resp.status}): {data.get('message', data)}")


async def create_gist(filename: str, content: str, description: str) -> str:
    url = f"{GITHUB_API}/gists"
    headers = {"Authorization": f"Bearer {GH_TOKEN}", "Accept": "application/vnd.github+json"}
    body = {"description": description, "public": True, "files": {filename: {"content": content}}}

    async with aiohttp.ClientSession() as session:
        async with session.post(url, headers=headers, json=body) as resp:
            data = await resp.json()
            if resp.status == 201:
                return data.get("html_url", "")
            raise RuntimeError(f"Gist 作成失敗 ({resp.status}): {data.get('message', data)}")


# ── Permission check decorator ─────────────────────────────────────────────────

async def check_role(interaction: discord.Interaction) -> bool:
    allowed, debug = await has_allowed_role(interaction)
    if not allowed:
        await interaction.followup.send(
            f"❌ **{ALLOWED_ROLE_NAME}** ロールが必要です。\n`{debug}`")
    return allowed


# ── Slash commands ─────────────────────────────────────────────────────────────

@client.tree.command(name="gh", description="gh コマンドを実行します")
@app_commands.describe(command="gh に渡す引数（例: repo list --limit 5）")
async def gh_command(interaction: discord.Interaction, command: str):
    await interaction.response.defer()
    if not await check_role(interaction):
        return

    try:
        args = shlex.split(command)
    except ValueError as e:
        await interaction.followup.send(f"❌ 解析失敗: {e}")
        return

    ok, reason = validate_args(args)
    if not ok:
        await interaction.followup.send(f"❌ {reason}")
        return

    try:
        output, rc = await run_command("gh", args, GH_TIMEOUT)
    except asyncio.TimeoutError:
        await interaction.followup.send(f"❌ タイムアウト（{GH_TIMEOUT}秒）")
        return
    except Exception as e:
        await interaction.followup.send(f"❌ エラー: {e}")
        return

    embed = discord.Embed(
        title=f"{'✅' if rc == 0 else '❌'}  gh {command}",
        description=f"```\n{truncate(output) if output else '(出力なし)'}\n```",
        color=discord.Color.green() if rc == 0 else discord.Color.red(),
    )
    embed.set_footer(text=f"実行者: {interaction.user}")
    await interaction.followup.send(embed=embed)


@client.tree.command(name="git", description="git コマンドを実行します")
@app_commands.describe(command="git に渡す引数（例: status）")
async def git_command(interaction: discord.Interaction, command: str):
    await interaction.response.defer()
    if not await check_role(interaction):
        return

    try:
        args = shlex.split(command)
    except ValueError as e:
        await interaction.followup.send(f"❌ 解析失敗: {e}")
        return

    ok, reason = validate_git_args(args)
    if not ok:
        await interaction.followup.send(f"❌ {reason}")
        return

    try:
        output, rc = await run_command("git", args, GIT_TIMEOUT, cwd=os.environ.get("GIT_WORK_DIR", "."))
    except asyncio.TimeoutError:
        await interaction.followup.send(f"❌ タイムアウト（{GIT_TIMEOUT}秒）")
        return
    except Exception as e:
        await interaction.followup.send(f"❌ エラー: {e}")
        return

    embed = discord.Embed(
        title=f"{'✅' if rc == 0 else '❌'}  git {command}",
        description=f"```\n{truncate(output) if output else '(出力なし)'}\n```",
        color=discord.Color.green() if rc == 0 else discord.Color.red(),
    )
    embed.set_footer(text=f"実行者: {interaction.user}")
    await interaction.followup.send(embed=embed)


@client.tree.command(name="copilot", description="AIと会話します（会話を記憶します）")
@app_commands.describe(prompt="メッセージ（例: Pythonでソート関数を書いて）")
async def copilot_command(interaction: discord.Interaction, prompt: str):
    await interaction.response.defer()
    if not await check_role(interaction):
        return

    ch = interaction.channel_id
    model = store.get_model(ch)

    store.add_message(ch, "user", prompt)
    messages = store.get_messages(ch)
    output, rc = await call_ai(messages, model)

    if rc != 0:
        store._history[ch].pop()  # remove failed user message
        await interaction.followup.send(f"❌ {output}")
        return

    store.add_message(ch, "assistant", output)

    # Extract code blocks and remember the last one
    code_blocks = extract_code_blocks(output)
    if code_blocks:
        lang, code = code_blocks[-1]
        ext_map = {"python": ".py", "javascript": ".js", "typescript": ".ts", "java": ".java",
                   "go": ".go", "rust": ".rs", "html": ".html", "css": ".css", "sh": ".sh", "bash": ".sh"}
        ext = ext_map.get(lang.lower(), ".txt")
        store.set_last_code(ch, code.strip(), f"code{ext}")

    # Check if AI wants to push
    push_directive = extract_push_directive(output)
    display = clean_response(output)

    # Auto-push if directive found
    push_msg = ""
    if push_directive and store.get_last_code(ch):
        code_to_push, default_fn = store.get_last_code(ch)
        fn = push_directive.get("filename", default_fn)
        desc = push_directive.get("description", prompt[:80])
        try:
            gist_url = await create_gist(fn, code_to_push, desc)
            push_msg = f"\n\n✅ **GitHub に保存しました:** {gist_url}"
        except Exception as e:
            push_msg = f"\n\n⚠️ Push 失敗: {e}"

    display_text = truncate(display + push_msg, 4000)

    embed = discord.Embed(description=display_text, color=discord.Color.blue())
    embed.set_footer(text=f"model: {model} | {store.summary(ch)} | 実行者: {interaction.user}")
    await interaction.followup.send(embed=embed)


@client.tree.command(name="create", description="AIがコードを生成してGitHubにpush")
@app_commands.describe(
    prompt="どんなコードを作るか",
    filename="ファイル名（省略可）",
    repo="pushするリポジトリ（省略時はGist）",
)
async def create_command(interaction: discord.Interaction, prompt: str, filename: str = "", repo: str = ""):
    await interaction.response.defer()
    if not await check_role(interaction):
        return
    if not GH_TOKEN:
        await interaction.followup.send("❌ GH_TOKEN が設定されていません。")
        return

    ch = interaction.channel_id
    model = store.get_model(ch)

    gen_prompt = (
        f"以下の依頼に基づいてコードを生成してください。コードのみ返してください（説明不要）。\n\n{prompt}"
    )
    messages = [
        {"role": "system", "content": "あなたはコード生成専門のアシスタントです。コードのみをmarkdownコードブロックで返してください。"},
        {"role": "user", "content": gen_prompt},
    ]
    output, rc = await call_ai(messages, model)
    if rc != 0:
        await interaction.followup.send(f"❌ コード生成失敗: {output}")
        return

    # Extract code
    code_blocks = extract_code_blocks(output)
    if code_blocks:
        lang, code = code_blocks[0]
        ext_map = {"python": ".py", "javascript": ".js", "typescript": ".ts", "java": ".java",
                   "go": ".go", "rust": ".rs", "html": ".html", "css": ".css", "sh": ".sh"}
        ext = ext_map.get(lang.lower(), ".txt")
        final_filename = filename or f"generated{ext}"
        code = code.strip()
    else:
        code = output.strip()
        final_filename = filename or "generated.txt"

    # Push
    try:
        if repo:
            url = await push_to_github(repo, final_filename, code, f"Add {final_filename}: {prompt[:50]}")
            target = f"📁 `{repo}`"
        else:
            url = await create_gist(final_filename, code, prompt[:100])
            target = "📝 Gist"
    except Exception as e:
        embed = discord.Embed(
            title="⚠️ コード生成OK / Push失敗",
            description=f"**エラー:** {e}\n\n```\n{truncate(code)}\n```",
            color=discord.Color.orange(),
        )
        await interaction.followup.send(embed=embed)
        return

    store.set_last_code(ch, code, final_filename)

    embed = discord.Embed(
        title=f"✅ {final_filename}",
        description=f"**依頼:** {prompt[:100]}\n{target} → {url}\n\n```\n{truncate(code, 1200)}\n```",
        color=discord.Color.green(),
    )
    embed.set_footer(text=f"model: {model} | 実行者: {interaction.user}")
    await interaction.followup.send(embed=embed)


@client.tree.command(name="model", description="AIモデルを切り替えます")
@app_commands.describe(name="モデル名")
@app_commands.choices(name=[app_commands.Choice(name=m, value=m) for m in AVAILABLE_MODELS])
async def model_command(interaction: discord.Interaction, name: app_commands.Choice[str]):
    await interaction.response.defer()
    if not await check_role(interaction):
        return

    ch = interaction.channel_id
    old = store.get_model(ch)
    store.set_model(ch, name.value)
    await interaction.followup.send(f"✅ モデルを変更: `{old}` → `{name.value}`")


@client.tree.command(name="reset", description="会話履歴をリセットします")
async def reset_command(interaction: discord.Interaction):
    await interaction.response.defer()
    if not await check_role(interaction):
        return

    store.clear(interaction.channel_id)
    await interaction.followup.send("✅ 会話履歴をリセットしました。")


@client.tree.command(name="history", description="現在の会話状態を表示します")
async def history_command(interaction: discord.Interaction):
    await interaction.response.defer()
    ch = interaction.channel_id
    msgs = store._history.get(ch, [])
    model = store.get_model(ch)
    last_code = store.get_last_code(ch)

    lines = [f"**モデル:** `{model}`", f"**履歴:** {len(msgs)} メッセージ（最大 {MAX_HISTORY}）"]
    if last_code:
        lines.append(f"**最後のコード:** `{last_code[1]}` ({len(last_code[0])} 文字)")
    if msgs:
        lines.append("\n**直近のやりとり:**")
        for m in msgs[-6:]:
            role = "👤" if m["role"] == "user" else "🤖"
            content = m["content"][:80] + ("…" if len(m["content"]) > 80 else "")
            lines.append(f"{role} {content}")

    embed = discord.Embed(description="\n".join(lines), color=discord.Color.greyple())
    await interaction.followup.send(embed=embed)


# ── Health check ───────────────────────────────────────────────────────────────

class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, *args):
        pass


def _start_health_server():
    port = int(os.environ.get("PORT", 8000))
    server = HTTPServer(("0.0.0.0", port), _HealthHandler)
    print(f"Health check server listening on port {port}")
    server.serve_forever()


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    t = threading.Thread(target=_start_health_server, daemon=True)
    t.start()
    client.run(DISCORD_TOKEN)
