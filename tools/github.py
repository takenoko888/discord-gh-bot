"""GitHub file/repo/gist tools."""

import base64
import aiohttp

from config import GH_TOKEN, GITHUB_API


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
        return "これはディレクトリです。list_filesを使ってください。"
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


async def tool_push_multiple_files(repo: str, files: list[dict], message: str, branch: str = "main") -> str:
    """Push multiple files in a single commit using the Git Trees API."""
    # 1. Get the latest commit SHA
    ref_result = await _gh_api("GET", f"/repos/{repo}/git/ref/heads/{branch}")
    if ref_result["status"] != 200:
        return f"エラー: ブランチ '{branch}' が見つかりません"
    latest_sha = ref_result["data"]["object"]["sha"]

    # 2. Get the base tree
    commit_result = await _gh_api("GET", f"/repos/{repo}/git/commits/{latest_sha}")
    if commit_result["status"] != 200:
        return "エラー: コミット情報の取得に失敗"
    base_tree_sha = commit_result["data"]["tree"]["sha"]

    # 3. Create blobs for each file
    tree_items = []
    for f in files:
        blob_result = await _gh_api("POST", f"/repos/{repo}/git/blobs", {
            "content": f["content"], "encoding": "utf-8"
        })
        if blob_result["status"] != 201:
            return f"エラー: {f['path']} の blob 作成に失敗"
        tree_items.append({
            "path": f["path"],
            "mode": "100644",
            "type": "blob",
            "sha": blob_result["data"]["sha"],
        })

    # 4. Create new tree
    tree_result = await _gh_api("POST", f"/repos/{repo}/git/trees", {
        "base_tree": base_tree_sha, "tree": tree_items
    })
    if tree_result["status"] != 201:
        return "エラー: ツリーの作成に失敗"

    # 5. Create commit
    new_commit = await _gh_api("POST", f"/repos/{repo}/git/commits", {
        "message": message,
        "tree": tree_result["data"]["sha"],
        "parents": [latest_sha],
    })
    if new_commit["status"] != 201:
        return "エラー: コミットの作成に失敗"

    # 6. Update branch ref
    headers = {"Authorization": f"Bearer {GH_TOKEN}", "Accept": "application/vnd.github+json"}
    async with aiohttp.ClientSession() as s:
        async with s.patch(
            f"{GITHUB_API}/repos/{repo}/git/refs/heads/{branch}",
            headers=headers, json={"sha": new_commit["data"]["sha"]}
        ) as r:
            if r.status != 200:
                return "エラー: ブランチの更新に失敗"

    file_list = ", ".join(f["path"] for f in files)
    return f"✅ {len(files)}ファイルを1コミットでpush: {file_list}\nhttps://github.com/{repo}"
