"""GitHub Issues tools."""

import aiohttp

from config import GH_TOKEN, GITHUB_API
from tools.github import _gh_api


async def tool_create_issue(repo: str, title: str, body: str = "", labels: list[str] | None = None) -> str:
    req_body: dict = {"title": title, "body": body}
    if labels:
        req_body["labels"] = labels
    result = await _gh_api("POST", f"/repos/{repo}/issues", req_body)
    if result["status"] == 201:
        d = result["data"]
        return f"✅ Issue #{d['number']} 作成: {d['html_url']}"
    return f"エラー ({result['status']}): {result['data'].get('message', '')}"


async def tool_list_issues(repo: str, state: str = "open") -> str:
    result = await _gh_api("GET", f"/repos/{repo}/issues?state={state}&per_page=15")
    if result["status"] != 200:
        return f"エラー ({result['status']}): {result['data'].get('message', '')}"
    items = result["data"]
    if not items:
        return f"Issue はありません（state={state}）"
    lines = []
    for i in items:
        pr_tag = " [PR]" if i.get("pull_request") else ""
        labels = " ".join(f"`{l['name']}`" for l in i.get("labels", []))
        lines.append(f"#{i['number']} {i['title']}{pr_tag} {labels}")
    return "\n".join(lines)


async def tool_comment_issue(repo: str, issue_number: int, body: str) -> str:
    result = await _gh_api("POST", f"/repos/{repo}/issues/{issue_number}/comments", {"body": body})
    if result["status"] == 201:
        return f"✅ #{issue_number} にコメント投稿: {result['data']['html_url']}"
    return f"エラー ({result['status']}): {result['data'].get('message', '')}"


async def tool_close_issue(repo: str, issue_number: int) -> str:
    headers = {"Authorization": f"Bearer {GH_TOKEN}", "Accept": "application/vnd.github+json"}
    async with aiohttp.ClientSession() as s:
        async with s.patch(
            f"{GITHUB_API}/repos/{repo}/issues/{issue_number}",
            headers=headers, json={"state": "closed"}
        ) as r:
            data = await r.json()
            if r.status == 200:
                return f"✅ Issue #{issue_number} をクローズしました"
            return f"エラー ({r.status}): {data.get('message', '')}"
