"""
Discord Bot — AI Agent with full GitHub automation.

The AI autonomously reads files, writes code, pushes to GitHub, runs commands,
all from a single natural language instruction.

Commands:
  /copilot <instruction>  — AI agent (auto-executes tools as needed)
  /gh <command>           — Run gh CLI directly
  /git <command>          — Run git CLI directly
  /model <name>           — Switch AI model
  /reset                  — Clear conversation history
  /history                — Show conversation state
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

作業が完了したら、結果をわかりやすく報告してください。
コードを見せる場合は```で囲んでください。"""

# ── Tool definitions (OpenAI function calling format) ──────────────────────────

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "GitHubリポジトリからファイルの内容を読み取る",
            "parameters": {
                "type": "object",
                "properties": {
                    "repo": {"type": "string", "description": "リポジトリ（例: takenoko888/discord-gh-bot）"},
                    "path": {"type": "string", "description": "ファイルパス（例: bot.py）"},
                },
                "required": ["repo", "path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "GitHubリポジトリのディレクトリ内のファイル一覧を取得する",
            "parameters": {
                "type": "object",
                "properties": {
                    "repo": {"type": "string", "description": "リポジトリ（例: takenoko888/discord-gh-bot）"},
                    "path": {"type": "string", "description": "ディレクトリパス（ルートは空文字）", "default": ""},
                },
                "required": ["repo"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "push_file",
            "description": "GitHubリポジトリにファイルを作成または更新する",
            "parameters": {
                "type": "object",
                "properties": {
                    "repo": {"type": "string", "description": "リポジトリ（例: takenoko888/discord-gh-bot）"},
                    "path": {"type": "string", "description": "ファイルパス（例: hello.py）"},
                    "content": {"type": "string", "description": "ファイルの内容"},
                    "message": {"type": "string", "description": "コミットメッセージ"},
                },
                "required": ["repo", "path", "content", "message"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_gist",
            "description": "GitHub Gistを作成してURLを返す",
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {"type": "string", "description": "ファイル名（例: calc.py）"},
                    "content": {"type": "string", "description": "ファイルの内容"},
                    "description": {"type": "string", "description": "Gistの説明"},
                },
                "required": ["filename", "content", "description"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_repo",
            "description": "新しいGitHubリポジトリを作成する",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "リポジトリ名"},
                    "description": {"type": "string", "description": "リポジトリの説明", "default": ""},
                    "private": {"type": "boolean", "description": "プライベートにするか", "default": False},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_gh",
            "description": "GitHub CLI (gh) コマンドを実行する。gh auth/config は禁止。",
            "parameters": {
                "type": "object",
                "properties": {
                    "args": {"type": "string", "description": "ghに渡す引数（例: repo list --limit 5）"},
                },
                "required": ["args"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_git",
            "description": "gitコマンドを実行する。git reset --hard, push --force は禁止。",
            "parameters": {
                "type": "object",
                "properties": {
                    "args": {"type": "string", "description": "gitに渡す引数（例: status）"},
                },
                "required": ["args"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_repo",
            "description": "GitHubリポジトリ内のコードを検索する",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "検索クエリ"},
                    "repo": {"type": "string", "description": "リポジトリ（例: takenoko888/discord-gh-bot）"},
                },
                "required": ["query", "repo"],
            },
        },
    },
]


# ── Conversation store ─────────────────────────────────────────────────────────

class ConversationStore:
    def __init__(self):
        self._history: dict[int, list[dict]] = defaultdict(list)
        self._models: dict[int, str] = {}
        self._timestamps: dict[int, float] = {}

    def get_model(self, ch: int) -> str:
        return self._models.get(ch, DEFAULT_MODEL)

    def set_model(self, ch: int, model: str):
        self._models[ch] = model

    def add(self, ch: int, msg: dict):
        self._history[ch].append(msg)
        if len(self._history[ch]) > MAX_HISTORY:
            self._history[ch] = self._history[ch][-MAX_HISTORY:]
        self._timestamps[ch] = time.time()

    def get_messages(self, ch: int) -> list[dict]:
        if ch in self._timestamps and time.time() - self._timestamps[ch] > HISTORY_TTL:
            self.clear(ch)
        return [{"role": "system", "content": SYSTEM_PROMPT}] + self._history[ch]

    def clear(self, ch: int):
        self._history.pop(ch, None)
        self._timestamps.pop(ch, None)

    def summary(self, ch: int) -> str:
        return f"model: {self.get_model(ch)} | 履歴: {len(self._history.get(ch, []))}件"


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

    async def on_ready(self):
        for guild in self.guilds:
            try:
                self.tree.copy_global_to(guild=guild)
                await self.tree.sync(guild=guild)
                print(f"Synced: {guild.name}")
            except Exception as e:
                print(f"Sync failed {guild.name}: {e}")
        print(f"Logged in as {self.user}")


client = GhBot()


# ── Tool implementations ──────────────────────────────────────────────────────

async def _gh_api(method: str, endpoint: str, body: dict | None = None) -> dict:
    headers = {"Authorization": f"Bearer {GH_TOKEN}", "Accept": "application/vnd.github+json"}
    async with aiohttp.ClientSession() as s:
        if method == "GET":
            async with s.get(f"{GITHUB_API}{endpoint}", headers=headers) as r:
                return {"status": r.status, "data": await r.json()}
        elif method == "PUT":
            async with s.put(f"{GITHUB_API}{endpoint}", headers=headers, json=body) as r:
                return {"status": r.status, "data": await r.json()}
        elif method == "POST":
            async with s.post(f"{GITHUB_API}{endpoint}", headers=headers, json=body) as r:
                return {"status": r.status, "data": await r.json()}
    return {"status": 500, "data": {"message": "Unknown method"}}


async def tool_read_file(repo: str, path: str) -> str:
    result = await _gh_api("GET", f"/repos/{repo}/contents/{path}")
    if result["status"] != 200:
        return f"エラー ({result['status']}): {result['data'].get('message', '')}"
    data = result["data"]
    if isinstance(data, list):
        return f"これはディレクトリです。list_filesを使ってください。"
    content = base64.b64decode(data.get("content", "")).decode("utf-8", errors="replace")
    return f"ファイル: {path} ({data.get('size', '?')} bytes)\n\n{content}"


async def tool_list_files(repo: str, path: str = "") -> str:
    endpoint = f"/repos/{repo}/contents/{path}" if path else f"/repos/{repo}/contents/"
    result = await _gh_api("GET", endpoint)
    if result["status"] != 200:
        return f"エラー ({result['status']}): {result['data'].get('message', '')}"
    data = result["data"]
    if not isinstance(data, list):
        return f"これはファイルです: {data.get('name', '?')}"
    lines = []
    for item in data:
        icon = "📁" if item["type"] == "dir" else "📄"
        size = f" ({item.get('size', 0)}B)" if item["type"] == "file" else ""
        lines.append(f"{icon} {item['path']}{size}")
    return "\n".join(lines) if lines else "(空のディレクトリ)"


async def tool_push_file(repo: str, path: str, content: str, message: str) -> str:
    # Get existing sha if file exists
    existing = await _gh_api("GET", f"/repos/{repo}/contents/{path}")
    sha = existing["data"].get("sha") if existing["status"] == 200 else None

    body = {"message": message, "content": base64.b64encode(content.encode()).decode("ascii")}
    if sha:
        body["sha"] = sha

    result = await _gh_api("PUT", f"/repos/{repo}/contents/{path}", body)
    if result["status"] in (200, 201):
        url = result["data"].get("content", {}).get("html_url", f"https://github.com/{repo}")
        action = "更新" if sha else "作成"
        return f"✅ {path} を{action}しました: {url}"
    return f"エラー ({result['status']}): {result['data'].get('message', '')}"


async def tool_create_gist(filename: str, content: str, description: str) -> str:
    body = {"description": description, "public": True, "files": {filename: {"content": content}}}
    result = await _gh_api("POST", "/gists", body)
    if result["status"] == 201:
        return f"✅ Gist作成: {result['data'].get('html_url', '')}"
    return f"エラー ({result['status']}): {result['data'].get('message', '')}"


async def tool_create_repo(name: str, description: str = "", private: bool = False) -> str:
    body = {"name": name, "description": description, "private": private, "auto_init": True}
    result = await _gh_api("POST", "/user/repos", body)
    if result["status"] == 201:
        return f"✅ リポジトリ作成: {result['data'].get('html_url', '')}"
    return f"エラー ({result['status']}): {result['data'].get('message', '')}"


async def tool_run_gh(args_str: str) -> str:
    try:
        args = shlex.split(args_str)
    except ValueError as e:
        return f"引数の解析エラー: {e}"
    if args and args[0] in BLOCKED_SUBCOMMANDS:
        return f"gh {args[0]} は安全のため禁止されています。"
    try:
        proc = await asyncio.create_subprocess_exec(
            "gh", *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=GH_TIMEOUT)
        output = stdout.decode("utf-8", errors="replace").strip()
        return output if output else "(出力なし)"
    except asyncio.TimeoutError:
        return "タイムアウト"
    except Exception as e:
        return f"実行エラー: {e}"


async def tool_run_git(args_str: str) -> str:
    try:
        args = shlex.split(args_str)
    except ValueError as e:
        return f"引数の解析エラー: {e}"
    if args:
        for pred in BLOCKED_GIT_PATTERNS:
            if pred(args):
                return f"git {' '.join(args)} は安全のため禁止されています。"
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            cwd=os.environ.get("GIT_WORK_DIR", "."))
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=GIT_TIMEOUT)
        output = stdout.decode("utf-8", errors="replace").strip()
        return output if output else "(出力なし)"
    except asyncio.TimeoutError:
        return "タイムアウト"
    except Exception as e:
        return f"実行エラー: {e}"


async def tool_search_repo(query: str, repo: str) -> str:
    headers = {"Authorization": f"Bearer {GH_TOKEN}", "Accept": "application/vnd.github+json"}
    search_url = f"{GITHUB_API}/search/code?q={query}+repo:{repo}"
    async with aiohttp.ClientSession() as s:
        async with s.get(search_url, headers=headers) as r:
            if r.status != 200:
                data = await r.json()
                return f"検索エラー ({r.status}): {data.get('message', '')}"
            data = await r.json()
            items = data.get("items", [])
            if not items:
                return f"「{query}」に一致する結果はありません。"
            lines = [f"検索結果: {data.get('total_count', 0)}件"]
            for item in items[:10]:
                lines.append(f"📄 {item['path']} ({item.get('html_url', '')})")
            return "\n".join(lines)


TOOL_DISPATCH = {
    "read_file": lambda p: tool_read_file(p["repo"], p.get("path", "")),
    "list_files": lambda p: tool_list_files(p["repo"], p.get("path", "")),
    "push_file": lambda p: tool_push_file(p["repo"], p["path"], p["content"], p["message"]),
    "create_gist": lambda p: tool_create_gist(p["filename"], p["content"], p["description"]),
    "create_repo": lambda p: tool_create_repo(p["name"], p.get("description", ""), p.get("private", False)),
    "run_gh": lambda p: tool_run_gh(p["args"]),
    "run_git": lambda p: tool_run_git(p["args"]),
    "search_repo": lambda p: tool_search_repo(p["query"], p["repo"]),
}


# ── Agent loop ─────────────────────────────────────────────────────────────────

async def agent_loop(ch: int, user_msg: str, progress_callback=None) -> str:
    """Run the AI agent loop: call AI → execute tools → repeat until done."""
    store.add(ch, {"role": "user", "content": user_msg})
    model = store.get_model(ch)

    tool_log = []

    for round_num in range(MAX_TOOL_ROUNDS):
        messages = store.get_messages(ch)

        # Call AI with tools
        headers = {"Authorization": f"Bearer {GH_TOKEN}", "Content-Type": "application/json"}
        body = {"model": model, "messages": messages, "tools": TOOLS, "tool_choice": "auto"}

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    MODELS_API_URL, headers=headers, json=body,
                    timeout=aiohttp.ClientTimeout(total=AGENT_TIMEOUT),
                ) as resp:
                    data = await resp.json()
                    if resp.status != 200:
                        err = data.get("error", {}).get("message", json.dumps(data, ensure_ascii=False))
                        return f"API エラー ({resp.status}): {err}"
        except asyncio.TimeoutError:
            return "タイムアウトしました。"
        except Exception as e:
            return f"リクエスト失敗: {e}"

        choice = data["choices"][0]
        msg = choice["message"]
        finish_reason = choice.get("finish_reason", "")

        # If no tool calls, we're done
        tool_calls = msg.get("tool_calls")
        if not tool_calls or finish_reason == "stop":
            final = msg.get("content", "")
            if final:
                store.add(ch, {"role": "assistant", "content": final})
            if tool_log:
                log_text = "\n".join(tool_log)
                return f"{final}\n\n---\n📋 **実行ログ:**\n{log_text}"
            return final or "(応答なし)"

        # Store assistant message with tool calls
        store.add(ch, {"role": "assistant", "content": msg.get("content") or "", "tool_calls": tool_calls})

        # Execute each tool call
        for tc in tool_calls:
            fn_name = tc["function"]["name"]
            try:
                fn_args = json.loads(tc["function"]["arguments"])
            except json.JSONDecodeError:
                fn_args = {}

            # Progress update
            args_summary = ", ".join(f"{k}={repr(v)[:40]}" for k, v in fn_args.items())
            tool_log.append(f"🔧 `{fn_name}({args_summary})`")

            if progress_callback:
                await progress_callback(f"🔧 実行中: `{fn_name}` (ステップ {round_num + 1})")

            # Execute
            if fn_name in TOOL_DISPATCH:
                try:
                    result = await TOOL_DISPATCH[fn_name](fn_args)
                    # Truncate very long results to avoid token limit
                    if len(result) > 8000:
                        result = result[:8000] + "\n…(結果を8000文字に省略)"
                except Exception as e:
                    result = f"ツール実行エラー: {e}"
            else:
                result = f"不明なツール: {fn_name}"

            tool_log.append(f"  → {result[:100]}{'…' if len(result) > 100 else ''}")

            store.add(ch, {"role": "tool", "tool_call_id": tc["id"], "content": result})

    return "⚠️ 最大ステップ数に達しました。`/copilot 続けて` で継続できます。"


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
    return ALLOWED_ROLE_NAME in role_names, f"roles={role_names}"


async def check_role(interaction: discord.Interaction) -> bool:
    allowed, debug = await has_allowed_role(interaction)
    if not allowed:
        await interaction.followup.send(f"❌ **{ALLOWED_ROLE_NAME}** ロールが必要です。\n`{debug}`")
    return allowed


def truncate(text: str, limit: int = MAX_OUTPUT_LENGTH) -> str:
    return text if len(text) <= limit else text[:limit] + "\n…(省略)"


# ── Slash commands ─────────────────────────────────────────────────────────────

@client.tree.command(name="copilot", description="AIエージェント — 指示1つで自動実行（GitHub読み書き・コード生成・push）")
@app_commands.describe(prompt="指示（例: takenoko888/discord-gh-botのbot.pyを読んで改善してpushして）")
async def copilot_command(interaction: discord.Interaction, prompt: str):
    await interaction.response.defer()
    if not await check_role(interaction):
        return

    ch = interaction.channel_id

    async def progress(msg):
        try:
            await interaction.edit_original_response(content=msg)
        except Exception:
            pass

    await progress("🤔 考え中...")

    result = await agent_loop(ch, prompt, progress_callback=progress)

    # Split long responses into multiple messages
    if len(result) <= 4000:
        embed = discord.Embed(description=result, color=discord.Color.blue())
        embed.set_footer(text=f"{store.summary(ch)} | 実行者: {interaction.user}")
        await interaction.edit_original_response(content=None, embed=embed)
    else:
        # Send as multiple messages for very long responses
        chunks = [result[i:i+1900] for i in range(0, len(result), 1900)]
        await interaction.edit_original_response(content=chunks[0])
        for chunk in chunks[1:]:
            await interaction.followup.send(chunk)


@client.tree.command(name="gh", description="gh コマンドを直接実行")
@app_commands.describe(command="引数（例: repo list --limit 5）")
async def gh_command(interaction: discord.Interaction, command: str):
    await interaction.response.defer()
    if not await check_role(interaction):
        return
    output = await tool_run_gh(command)
    embed = discord.Embed(
        title=f"gh {command}",
        description=f"```\n{truncate(output)}\n```",
        color=discord.Color.green(),
    )
    await interaction.followup.send(embed=embed)


@client.tree.command(name="git", description="git コマンドを直接実行")
@app_commands.describe(command="引数（例: status）")
async def git_command(interaction: discord.Interaction, command: str):
    await interaction.response.defer()
    if not await check_role(interaction):
        return
    output = await tool_run_git(command)
    embed = discord.Embed(
        title=f"git {command}",
        description=f"```\n{truncate(output)}\n```",
        color=discord.Color.green(),
    )
    await interaction.followup.send(embed=embed)


@client.tree.command(name="model", description="AIモデルを切り替え")
@app_commands.describe(name="モデル名")
@app_commands.choices(name=[app_commands.Choice(name=m, value=m) for m in AVAILABLE_MODELS])
async def model_command(interaction: discord.Interaction, name: app_commands.Choice[str]):
    await interaction.response.defer()
    if not await check_role(interaction):
        return
    ch = interaction.channel_id
    old = store.get_model(ch)
    store.set_model(ch, name.value)
    await interaction.followup.send(f"✅ モデル変更: `{old}` → `{name.value}`")


@client.tree.command(name="reset", description="会話履歴をリセット")
async def reset_command(interaction: discord.Interaction):
    await interaction.response.defer()
    if not await check_role(interaction):
        return
    store.clear(interaction.channel_id)
    await interaction.followup.send("✅ 会話履歴をリセットしました。")


@client.tree.command(name="history", description="会話状態を表示")
async def history_command(interaction: discord.Interaction):
    await interaction.response.defer()
    ch = interaction.channel_id
    msgs = store._history.get(ch, [])
    model = store.get_model(ch)

    lines = [f"**モデル:** `{model}`", f"**履歴:** {len(msgs)}/{MAX_HISTORY} メッセージ"]

    user_msgs = [m for m in msgs if m.get("role") == "user"]
    tool_msgs = [m for m in msgs if m.get("role") == "tool"]
    lines.append(f"**ユーザー発言:** {len(user_msgs)} | **ツール実行:** {len(tool_msgs)}")

    if msgs:
        lines.append("\n**直近:**")
        for m in msgs[-6:]:
            role = m.get("role", "?")
            icons = {"user": "👤", "assistant": "🤖", "tool": "🔧", "system": "⚙️"}
            icon = icons.get(role, "❓")
            content = m.get("content", "")[:80]
            if m.get("tool_calls"):
                names = [tc["function"]["name"] for tc in m["tool_calls"]]
                content = f"ツール呼出: {', '.join(names)}"
            lines.append(f"{icon} {content}")

    embed = discord.Embed(description="\n".join(lines), color=discord.Color.greyple())
    await interaction.followup.send(embed=embed)


# ── Health check ───────────────────────────────────────────────────────────────

class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")
    def log_message(self, *a):
        pass

def _start_health_server():
    port = int(os.environ.get("PORT", 8000))
    HTTPServer(("0.0.0.0", port), _HealthHandler).serve_forever()


if __name__ == "__main__":
    threading.Thread(target=_start_health_server, daemon=True).start()
    client.run(DISCORD_TOKEN)
