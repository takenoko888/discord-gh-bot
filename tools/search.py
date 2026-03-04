"""Web search tool using Google Custom Search API."""

import aiohttp
from config import GOOGLE_API_KEY, GOOGLE_CSE_ID

SEARCH_URL = "https://www.googleapis.com/customsearch/v1"


async def tool_web_search(query: str, num: int = 5) -> str:
    """Search the web using Google Custom Search and return summarized results."""
    if not GOOGLE_API_KEY or not GOOGLE_CSE_ID:
        return "❌ GOOGLE_API_KEY または GOOGLE_CSE_ID が設定されていません。"

    params = {
        "key": GOOGLE_API_KEY,
        "cx": GOOGLE_CSE_ID,
        "q": query,
        "num": min(num, 10),
        "lr": "lang_ja",
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(SEARCH_URL, params=params, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    return f"❌ 検索エラー ({resp.status}): {text[:200]}"
                data = await resp.json()
    except Exception as e:
        return f"❌ 検索失敗: {e}"

    items = data.get("items", [])
    if not items:
        return f"「{query}」の検索結果が見つかりませんでした。"

    lines = [f"🔍 **「{query}」の検索結果**\n"]
    for i, item in enumerate(items, 1):
        title = item.get("title", "")
        link = item.get("link", "")
        snippet = item.get("snippet", "").replace("\n", " ")
        lines.append(f"**{i}. {title}**")
        lines.append(f"　{snippet}")
        lines.append(f"　<{link}>\n")

    return "\n".join(lines)
