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

import asyncio
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import discord
from discord import app_commands

from config import (
    DISCORD_TOKEN, ALLOWED_ROLE_NAME, AVAILABLE_MODELS,
    MAX_OUTPUT_LENGTH, MAX_HISTORY,
)
from store import ConversationStore
from agent import agent_loop
from tools.git_commands import tool_run_gh, tool_run_git

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

    result = await agent_loop(ch, prompt, store, progress_callback=progress)

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


@client.tree.command(name="remind", description="指定分後にメンションで通知")
@app_commands.describe(minutes="何分後に通知するか", message="通知内容（例: PRのレビューをする）")
async def remind_command(interaction: discord.Interaction, minutes: int, message: str):
    await interaction.response.defer()
    if minutes <= 0:
        await interaction.followup.send("❌ 1分以上で指定してください。")
        return
    await interaction.followup.send(f"⏰ **{minutes}分後**に通知します！\n> {message}")

    await asyncio.sleep(minutes * 60)

    try:
        await interaction.channel.send(
            f"⏰ {interaction.user.mention} リマインダー！\n> {message}"
        )
    except Exception as e:
        print(f"remind error: {e}")



@client.tree.command(name="models", description="利用可能なAIモデル一覧をGitHub Modelsカタログから取得して表示")
async def models_command(interaction: discord.Interaction):
    await interaction.response.defer()
    import aiohttp
    from config import GH_TOKEN
    catalog_url = "https://models.github.ai/catalog/models"
    headers = {
        "Authorization": f"Bearer {GH_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(catalog_url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    await interaction.followup.send(f"❌ カタログ取得失敗 ({resp.status})")
                    return
                models = await resp.json()
    except Exception as e:
        await interaction.followup.send(f"❌ エラー: {e}")
        return

    lines = ["## 🤖 利用可能なモデル一覧\n"]
    for m in models:
        mid = m.get("id", "")
        name = m.get("name", "")
        tier = m.get("rate_limit_tier", "")
        lines.append(f"**`{mid}`** — {name} ({tier})")

    text = "\n".join(lines)
    if len(text) > 3900:
        text = text[:3900] + "\n…(省略)"
    embed = discord.Embed(description=text, color=discord.Color.blurple())
    embed.set_footer(text="モデル変更: /model <ID>")
    await interaction.followup.send(embed=embed)


@client.tree.command(name="help", description="コマンド一覧を表示")
async def help_command(interaction: discord.Interaction):
    await interaction.response.defer()
    lines = [
        "## 🤖 GitHub Copilot Bot — コマンド一覧\n",
        "**`/copilot <指示>`**",
        "　AIエージェントが自動でGitHub操作・コード生成・pushを行います。",
        "　例: `takenoko888/my-repo のREADMEを改善してpushして`\n",
        "**`/gh <command>`**",
        "　gh CLIを直接実行します。",
        "　例: `repo list --limit 5` / `issue list`\n",
        "**`/git <command>`**",
        "　git コマンドを直接実行します。",
        "　例: `status` / `log --oneline -10`\n",
        "**`/model <name>`**",
        "　使用するAIモデルを切り替えます。\n",
        "**`/reset`**",
        "　このチャンネルの会話履歴をリセットします。\n",
        "**`/history`**",
        "　現在の会話状態（モデル・履歴数）を表示します。\n",
        "**`/remind <分> <内容>`**",
        "　指定した分数後にメンションで通知します。",
        "　例: `30 PRのレビューをする`\n",
        "**`/models`**",
        "　利用可能なAIモデル一覧を表示します。\n",
        "**`/help`**",
        "　このコマンド一覧を表示します。",
    ]
    embed = discord.Embed(
        description="\n".join(lines),
        color=discord.Color.blurple(),
    )
    await interaction.followup.send(embed=embed)


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
