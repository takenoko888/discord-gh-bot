"""Git CLI and branch management tools."""

import os
import asyncio
import shlex
import aiohttp

from config import GH_TOKEN, GITHUB_API, BLOCKED_SUBCOMMANDS, BLOCKED_GIT_PATTERNS, GH_TIMEOUT, GIT_TIMEOUT
from tools.github import _gh_api


async def tool_run_gh(args_str: str) -> str:
    try:
        args = shlex.split(args_str)
    except ValueError as e:
        return f"引数の解析エラー: {e}"
    if args and args[0] in BLOCKED_SUBCOMMANDS:
        return f"gh {args[0]} は安全のため禁止されています。"
    try:
        env = os.environ.copy()
        # Ensure all token variants are set for gh copilot and other tools
        token = GH_TOKEN or env.get("GITHUB_TOKEN") or env.get("COPILOT_GITHUB_TOKEN") or ""
        if token:
            env["GH_TOKEN"] = token
            env["GITHUB_TOKEN"] = token
            env["COPILOT_GITHUB_TOKEN"] = token
        proc = await asyncio.create_subprocess_exec(
            "gh", *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            env=env)
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


async def tool_create_branch(repo: str, branch_name: str, from_branch: str = "main") -> str:
    ref_result = await _gh_api("GET", f"/repos/{repo}/git/ref/heads/{from_branch}")
    if ref_result["status"] != 200:
        return f"エラー: ブランチ '{from_branch}' が見つかりません"
    sha = ref_result["data"]["object"]["sha"]
    result = await _gh_api("POST", f"/repos/{repo}/git/refs", {"ref": f"refs/heads/{branch_name}", "sha": sha})
    if result["status"] == 201:
        return f"✅ ブランチ '{branch_name}' を作成しました（{from_branch} から分岐）"
    return f"エラー ({result['status']}): {result['data'].get('message', '')}"
