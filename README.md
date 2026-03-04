# Discord gh-bot 

Discord上で `gh` (GitHub CLI) コマンドを実行できるスラッシュコマンドBot。  
**Koyeb** での常時稼働に対応しています。

## 機能

- `/gh <引数>` — GitHub CLI コマンドを実行して結果をEmbedで返す
- `/git <引数>` — git コマンドを実行（push, commit, clone など）
- `/copilot <質問>` — AIエージェント機能：自然言語を使用して指示を伝えることで、AIがGitHub操作やコード生成などを自動で実行
- ロールベースの権限管理（`gh-bot` ロールのみ実行可）
- `gh auth` / `gh config` は安全のためブロック
- `git reset --hard` / `git push --force` / `git clean -fd` は安全のためブロック
- タイムアウト・長い出力は自動省略
- ヘルスチェック用HTTPサーバー内蔵（ポート `8000`）

---

## ローカルセットアップ

### 1. 依存パッケージのインストール

```bash
pip install -r requirements.txt
```

### 2. GitHub CLI のインストール・認証

```bash
# インストール: https://cli.github.com/
gh auth login
```

### 3. Discord Bot の作成

1. [Discord Developer Portal](https://discord.com/developers/applications) でアプリを作成
2. **Bot** タブ → トークンをコピー
3. **OAuth2 → URL Generator** で以下のスコープを選択: `bot` / `applications.commands`
4. Bot Permissions: `Send Messages`, `Use Slash Commands`
5. 生成されたURLでサーバーに招待

### 4. 環境変数の設定

```bash
cp .env.example .env
# .env を編集して値を設定
```

### 5. Discord サーバーにロールを作成

サーバー設定 → ロール → `gh-bot` ロールを作成し、許可するメンバーに付与。

### 6. 起動

```bash
python bot.py
```

---

## GitHubへのプッシュ

```bash
cd "github cli"

git init
git add .
git commit -m "Initial commit: Discord gh-bot"

gh repo create takenoko888/discord-gh-bot --public --source=. --remote=origin --push
```

---

## Koyebへのデプロイ（常時稼働）

### 仕組み

KoyebはGitHubリポジトリを監視し、pushのたびに自動再デプロイします。  
`Dockerfile` を使ってコンテナをビルドし、GitHub CLI (`gh`) を同梱します。  
GitHub CLIの認証は `GH_TOKEN` 環境変数で行います（`gh auth login` 不要）。

### 手順

1. **GitHubにプッシュ**（上記手順）

2. **[koyeb.com](https://www.koyeb.com/) でアカウント作成**（無料プランあり）

3. **新しいサービスを作成**
   - "Create Service" → **GitHub** を選択
   - リポジトリ `takenoko888/discord-gh-bot` を選択
   - **Service type**: `Web Service`
   - **Builder**: `Dockerfile` を選択
   - **Port**: `8000`
   - **Health check path**: `/`

4. **環境変数を設定**（Koyebダッシュボード → Environment）

   | 変数名 | 値 |
   |--------|-----|
   | `DISCORD_TOKEN` | Discord Botトークン |
   | `GH_TOKEN` | GitHubのPersonal Access Token ([作成](https://github.com/settings/tokens)) |
   | `ALLOWED_ROLE_NAME` | `gh-bot`（変更したい場合のみ） |

   > **`GH_TOKEN` に必要なスコープ**: `repo`, `read:org`, `workflow`

5. **Deploy** → 起動完了でBotがオンラインになります

---

## 使い方

```
# GitHub CLI
/gh repo list --limit 5
/gh issue list --repo owner/repo
/gh pr list --state open
/gh run list --limit 3
/gh release list --repo owner/repo

# git
/git status
/git push origin main
/git clone https://github.com/owner/repo.git

# Copilot（AI）
/copilot PythonでHello Worldを書いて
/copilot このエラーの原因を教えて
```

---

## 環境変数一覧

| 変数名 | 説明 | デフォルト |
|--------|------|-----------|
| `DISCORD_TOKEN` | Discord Bot トークン | (必須) |
| `GH_TOKEN` | GitHub Personal Access Token（Koyeb用） | (Koyebでは必須) |
| `ALLOWED_ROLE_NAME` | 実行を許可するロール名 | `gh-bot` |
| `PORT` | ヘルスチェックサーバーのポート | `8000` |
| `GIT_WORK_DIR` | `/git` コマンドの作業ディレクトリ | `.`（カレント） |

> **注意**: `/copilot` は GitHub Copilot のサブスクリプションが必要です。`/git` で push 等を行う場合、Koyeb ではコンテナ再起動でファイルが消えるため、永続化にはボリュームの設定を検討してください。
