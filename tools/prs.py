"""GitHub Pull Request tools."""

import aiohttp

from config import GH_TOKEN, GITHUB_API
from tools.github import _gh_api


async def tool_create_pr(repo: str, title: str, head: str, base: str = "main", body: str = "") -> str:
    req_body = {"title": title, "head": head, "base": base, "body": body}
    result = await _gh_api("POST", f"/repos/{repo}/pulls", req_body)
    if result["status"] == 201:
        d = result["data"]
        return f"✅ PR #{d['number']} 作成: {d['html_url']}"
    return f"エラー ({result['status']}): {result['data'].get('message', '')}"


async def tool_list_prs(repo: str, state: str = "open") -> str:
    result = await _gh_api("GET", f"/repos/{repo}/pulls?state={state}&per_page=15")
    if result["status"] != 200:
        return f"エラー ({result['status']}): {result['data'].get('message', '')}"
    items = result["data"]
    if not items:
        return f"PR はありません（state={state}）"
    lines = []
    for pr in items:
        lines.append(f"#{pr['number']} {pr['title']} ({pr['head']['ref']} → {pr['base']['ref']})")
    return "\n".join(lines)


async def tool_get_pr_diff(repo: str, pr_number: int) -> str:
    headers = {"Authorization": f"Bearer {GH_TOKEN}", "Accept": "application/vnd.github.diff"}
    async with aiohttp.ClientSession() as s:
        async with s.get(f"{GITHUB_API}/repos/{repo}/pulls/{pr_number}", headers=headers) as r:
            if r.status != 200:
                return f"エラー ({r.status})"
            diff = await r.text()
            if len(diff) > 8000:
                diff = diff[:8000] + "\n…(差分を8000文字に省略)"
            return diff if diff else "(差分なし)"


async def tool_merge_pr(repo: str, pr_number: int, merge_method: str = "merge") -> str:
    result = await _gh_api("PUT", f"/repos/{repo}/pulls/{pr_number}/merge", {"merge_method": merge_method})
    if result["status"] == 200:
        return f"✅ PR #{pr_number} をマージしました"
    return f"エラー ({result['status']}): {result['data'].get('message', '')}"
