"""Agent loop, tool schema definitions, and TOOL_DISPATCH."""

import json
import asyncio
import aiohttp

from config import GH_TOKEN, MODELS_API_URL, AGENT_TIMEOUT, MAX_TOOL_ROUNDS
from store import ConversationStore

# Circular import avoided: store instance is passed in or imported at usage site.
# agent_loop takes a ConversationStore instance as a parameter.

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "GitHubリポジトリからファイルの内容を読み取る",
            "parameters": {
                "type": "object",
                "properties": {
                    "repo": {"type": "string", "description": "リポジトリ（例: owner/repo）"},
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
                    "repo": {"type": "string", "description": "リポジトリ（例: owner/repo）"},
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
                    "repo": {"type": "string", "description": "リポジトリ（例: owner/repo）"},
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
                    "repo": {"type": "string", "description": "リポジトリ（例: owner/repo）"},
                },
                "required": ["query", "repo"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_issue",
            "description": "GitHubリポジトリにIssueを作成する",
            "parameters": {
                "type": "object",
                "properties": {
                    "repo": {"type": "string", "description": "リポジトリ"},
                    "title": {"type": "string", "description": "Issueのタイトル"},
                    "body": {"type": "string", "description": "Issueの本文（Markdown可）", "default": ""},
                    "labels": {"type": "array", "items": {"type": "string"}, "description": "ラベル名の配列", "default": []},
                },
                "required": ["repo", "title"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_issues",
            "description": "GitHubリポジトリのIssue一覧を取得する",
            "parameters": {
                "type": "object",
                "properties": {
                    "repo": {"type": "string", "description": "リポジトリ"},
                    "state": {"type": "string", "enum": ["open", "closed", "all"], "description": "状態フィルタ", "default": "open"},
                },
                "required": ["repo"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "comment_issue",
            "description": "IssueまたはPRにコメントを投稿する",
            "parameters": {
                "type": "object",
                "properties": {
                    "repo": {"type": "string", "description": "リポジトリ"},
                    "issue_number": {"type": "integer", "description": "IssueまたはPRの番号"},
                    "body": {"type": "string", "description": "コメント本文"},
                },
                "required": ["repo", "issue_number", "body"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "close_issue",
            "description": "Issueをクローズする",
            "parameters": {
                "type": "object",
                "properties": {
                    "repo": {"type": "string", "description": "リポジトリ"},
                    "issue_number": {"type": "integer", "description": "Issue番号"},
                },
                "required": ["repo", "issue_number"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_pr",
            "description": "プルリクエストを作成する",
            "parameters": {
                "type": "object",
                "properties": {
                    "repo": {"type": "string", "description": "リポジトリ"},
                    "title": {"type": "string", "description": "PRタイトル"},
                    "body": {"type": "string", "description": "PR本文", "default": ""},
                    "head": {"type": "string", "description": "マージ元ブランチ"},
                    "base": {"type": "string", "description": "マージ先ブランチ", "default": "main"},
                },
                "required": ["repo", "title", "head"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_prs",
            "description": "プルリクエスト一覧を取得する",
            "parameters": {
                "type": "object",
                "properties": {
                    "repo": {"type": "string", "description": "リポジトリ"},
                    "state": {"type": "string", "enum": ["open", "closed", "all"], "description": "状態", "default": "open"},
                },
                "required": ["repo"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_pr_diff",
            "description": "PRの差分（変更内容）を取得する。コードレビューに使用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "repo": {"type": "string", "description": "リポジトリ"},
                    "pr_number": {"type": "integer", "description": "PR番号"},
                },
                "required": ["repo", "pr_number"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "merge_pr",
            "description": "プルリクエストをマージする",
            "parameters": {
                "type": "object",
                "properties": {
                    "repo": {"type": "string", "description": "リポジトリ"},
                    "pr_number": {"type": "integer", "description": "PR番号"},
                    "merge_method": {"type": "string", "enum": ["merge", "squash", "rebase"], "description": "マージ方法", "default": "merge"},
                },
                "required": ["repo", "pr_number"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_branch",
            "description": "新しいブランチを作成する",
            "parameters": {
                "type": "object",
                "properties": {
                    "repo": {"type": "string", "description": "リポジトリ"},
                    "branch_name": {"type": "string", "description": "新しいブランチ名"},
                    "from_branch": {"type": "string", "description": "分岐元ブランチ", "default": "main"},
                },
                "required": ["repo", "branch_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "push_multiple_files",
            "description": "複数ファイルを1つのコミットでリポジトリにpushする",
            "parameters": {
                "type": "object",
                "properties": {
                    "repo": {"type": "string", "description": "リポジトリ"},
                    "files": {
                        "type": "array",
                        "description": "ファイルの配列",
                        "items": {
                            "type": "object",
                            "properties": {
                                "path": {"type": "string", "description": "ファイルパス"},
                                "content": {"type": "string", "description": "ファイル内容"},
                            },
                            "required": ["path", "content"],
                        },
                    },
                    "message": {"type": "string", "description": "コミットメッセージ"},
                    "branch": {"type": "string", "description": "ブランチ名", "default": "main"},
                },
                "required": ["repo", "files", "message"],
            },
        },
    },
]


def _make_dispatch() -> dict:
    """Build TOOL_DISPATCH lazily to avoid circular imports at module load time."""
    from tools.github import (
        tool_read_file, tool_list_files, tool_push_file,
        tool_create_gist, tool_create_repo, tool_push_multiple_files,
    )
    from tools.issues import tool_create_issue, tool_list_issues, tool_comment_issue, tool_close_issue
    from tools.prs import tool_create_pr, tool_list_prs, tool_get_pr_diff, tool_merge_pr
    from tools.git_commands import tool_run_gh, tool_run_git, tool_create_branch, tool_search_repo

    return {
        "read_file":           lambda p: tool_read_file(p["repo"], p.get("path", "")),
        "list_files":          lambda p: tool_list_files(p["repo"], p.get("path", "")),
        "push_file":           lambda p: tool_push_file(p["repo"], p["path"], p["content"], p["message"]),
        "create_gist":         lambda p: tool_create_gist(p["filename"], p["content"], p["description"]),
        "create_repo":         lambda p: tool_create_repo(p["name"], p.get("description", ""), p.get("private", False)),
        "run_gh":              lambda p: tool_run_gh(p["args"]),
        "run_git":             lambda p: tool_run_git(p["args"]),
        "search_repo":         lambda p: tool_search_repo(p["query"], p["repo"]),
        "create_issue":        lambda p: tool_create_issue(p["repo"], p["title"], p.get("body", ""), p.get("labels")),
        "list_issues":         lambda p: tool_list_issues(p["repo"], p.get("state", "open")),
        "comment_issue":       lambda p: tool_comment_issue(p["repo"], p["issue_number"], p["body"]),
        "close_issue":         lambda p: tool_close_issue(p["repo"], p["issue_number"]),
        "create_pr":           lambda p: tool_create_pr(p["repo"], p["title"], p["head"], p.get("base", "main"), p.get("body", "")),
        "list_prs":            lambda p: tool_list_prs(p["repo"], p.get("state", "open")),
        "get_pr_diff":         lambda p: tool_get_pr_diff(p["repo"], p["pr_number"]),
        "merge_pr":            lambda p: tool_merge_pr(p["repo"], p["pr_number"], p.get("merge_method", "merge")),
        "create_branch":       lambda p: tool_create_branch(p["repo"], p["branch_name"], p.get("from_branch", "main")),
        "push_multiple_files": lambda p: tool_push_multiple_files(p["repo"], p["files"], p["message"], p.get("branch", "main")),
    }


async def agent_loop(ch: int, user_msg: str, store: ConversationStore, progress_callback=None) -> str:
    """Run the AI agent loop: call AI → execute tools → repeat until done."""
    store.add(ch, {"role": "user", "content": user_msg})
    model = store.get_model(ch)
    tool_dispatch = _make_dispatch()
    tool_log = []

    for round_num in range(MAX_TOOL_ROUNDS):
        messages = store.get_messages(ch)

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

        tool_calls = msg.get("tool_calls")
        if not tool_calls or finish_reason == "stop":
            final = msg.get("content", "")
            if final:
                store.add(ch, {"role": "assistant", "content": final})
            if tool_log:
                log_text = "\n".join(tool_log)
                return f"{final}\n\n---\n📋 **実行ログ:**\n{log_text}"
            return final or "(応答なし)"

        store.add(ch, {"role": "assistant", "content": msg.get("content") or "", "tool_calls": tool_calls})

        for tc in tool_calls:
            fn_name = tc["function"]["name"]
            try:
                fn_args = json.loads(tc["function"]["arguments"])
            except json.JSONDecodeError:
                fn_args = {}

            args_summary = ", ".join(f"{k}={repr(v)[:40]}" for k, v in fn_args.items())
            tool_log.append(f"🔧 `{fn_name}({args_summary})`")

            if progress_callback:
                await progress_callback(f"🔧 実行中: `{fn_name}` (ステップ {round_num + 1})")

            if fn_name in tool_dispatch:
                try:
                    result = await tool_dispatch[fn_name](fn_args)
                    if len(result) > 8000:
                        result = result[:8000] + "\n…(結果を8000文字に省略)"
                except Exception as e:
                    result = f"ツール実行エラー: {e}"
            else:
                result = f"不明なツール: {fn_name}"

            tool_log.append(f"  → {result[:100]}{'…' if len(result) > 100 else ''}")
            store.add(ch, {"role": "tool", "tool_call_id": tc["id"], "content": result})

    return "⚠️ 最大ステップ数に達しました。`/copilot 続けて` で継続できます。"
