# Google Chat Bridge 設定指南

`gchat_bridge.py` 透過 **Cloud Pub/Sub pull** 接收 Google Chat 事件——只有 outbound
連線，機器在 NAT / 防火牆後方也能跑，不需要公開 HTTPS endpoint。回覆透過 Chat API
（`spaces.messages.create`）以 app 身分送出。

> 注意：Google Chat 只在「使用者 DM 這個 app」或「在 space 中 @提及 app」時
> 發送 MESSAGE 事件，不像 Discord bot 能看到頻道內所有訊息。

## 1. Google Cloud 專案

1. 建立（或選用）一個 GCP 專案，啟用計費帳戶。
2. 啟用兩個 API：**Google Chat API** 與 **Cloud Pub/Sub API**。

## 2. Pub/Sub topic 與 subscription

```bash
gcloud pubsub topics create chat-events

# Google Chat 用這個系統帳號發佈事件，授予 Publisher：
gcloud pubsub topics add-iam-policy-binding chat-events \
  --member='serviceAccount:chat-api-push@system.gserviceaccount.com' \
  --role='roles/pubsub.publisher'

gcloud pubsub subscriptions create chat-events-sub --topic=chat-events
```

## 3. Service account（bridge 的身分）

```bash
gcloud iam service-accounts create gchat-bridge
gcloud pubsub subscriptions add-iam-policy-binding chat-events-sub \
  --member='serviceAccount:gchat-bridge@PROJECT_ID.iam.gserviceaccount.com' \
  --role='roles/pubsub.subscriber'
gcloud iam service-accounts keys create key.json \
  --iam-account=gchat-bridge@PROJECT_ID.iam.gserviceaccount.com
```

`key.json` 的路徑填入 `.env.gchat` 的 `GOOGLE_APPLICATION_CREDENTIALS`。
以 app 身分回覆（scope `chat.bot`）**不需要** domain-wide delegation，
也不需要管理員逐一核准 OAuth scope。

## 4. Chat API 設定（Cloud Console → Chat API → Configuration）

- App name（≤25 字元）、Avatar URL、Description（≤40 字元）
- 啟用 **Interactive features**，勾選 *Receive 1:1 messages* 與
  *Join spaces and group conversations*
- **Connection settings** 選 **Cloud Pub/Sub**，貼上 topic 完整名稱
  `projects/PROJECT_ID/topics/chat-events`
  （若出現「Build this Chat app as a Google Workspace add-on」選項請取消勾選）
- **Visibility**：開發/自用階段直接填最多 5 位使用者或組織內的 Google Group，
  不需任何 Google 審查。要全網域安裝才需上架 Workspace Marketplace
  （選 Private 限本網域，同樣不需審查）。

Workspace 組織的前提：Admin console → Apps → Google Workspace → Google Chat →
Chat apps 的「Allow users to install Chat apps」需為開啟。

## 5. 啟動 bridge

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-gchat.txt
cp .env.gchat.example .env.gchat   # 填入專案/subscription/金鑰路徑
python3 gchat_bridge.py --env .env.gchat
```

在 Google Chat 搜尋你的 app 名稱開 DM，或把 app 加進 space 後 @提及它。

## 選配：讓 Claude 讀 Chat 歷史（Chat MCP server）

Google 提供代管的 **Chat MCP server**（`https://chatmcp.googleapis.com/mcp/v1`，
Developer Preview），讓 AI 客戶端以「使用者本人」的 OAuth 身分讀取 Chat：
`list_messages`、`search_messages`、`search_conversations`（另有 `send_message`）。

它**不能**取代這個 bridge——MCP 是 on-demand 工具，收不到任何事件；但可以補上
「新 session 沒有前文」的缺口：bridge 預設（`GCHAT_MCP_CONTEXT_HINT=1`）會在每個
新 session 的第一輪注入提示，告訴後端目前的 space/thread resource name，若後端
掛了 Chat MCP server 就會先撈回最近的對話再回答；沒掛也會正常回答（提示是條件式的）。

在跑 bridge 的機器上為 Claude Code 加上這個 MCP server：

```bash
claude mcp add --transport http google-chat https://chatmcp.googleapis.com/mcp/v1
```

首次使用會走 OAuth 授權（需先在同一個 GCP 專案啟用 Chat MCP API 並設定 OAuth
consent screen 與 OAuth client，scope 為 `chat.spaces.readonly`、
`chat.memberships.readonly`、`chat.messages.readonly`、`chat.messages.create`、
`chat.users.readstate.readonly`）。詳見官方指南：
https://developers.google.com/workspace/chat/api/guides/configure-mcp-server

注意：MCP 的 `send_message` 以使用者本人名義發言。bridge 注入的提示已明確禁止
後端用它回覆——回覆一律由 bridge 以 app 身分（`chat.bot`）送出。

## 平台差異備忘

| 面向 | Discord (bot.py) | Google Chat (gchat_bridge.py) |
|---|---|---|
| 連線 | WebSocket gateway | Pub/Sub StreamingPull（同樣 NAT 友善） |
| 收訊範圍 | 頻道所有訊息 | 只有 DM 與 @提及 |
| 遞送保證 | 一次 | at-least-once（bridge 內建以 message name 去重） |
| 訊息上限 | 2,000 字元 | 32,000 bytes（`GCHAT_CHUNK` 預設 8,000 字元） |
| 速率 | 5 msg/5s/channel | 1 write/s/space（`GCHAT_SEND_INTERVAL` 節流） |
| 格式 | Markdown | Chat 專屬語法（bridge 內建 Markdown 轉換） |
| Session 單位 | SESSION_SCOPE（預設 thread） | GCHAT_SESSION_SCOPE=auto：threaded space 用 thread、DM/flat space 用 space |

若之後需要「不 @ 也回應」的行為，需另外用 Workspace Events API 訂閱該 space 的
訊息事件（同樣送進 Pub/Sub，但訂閱會過期需定期 renew，且需 user auth）——
目前版本刻意不做，@mention 模式在共享 space 反而較安全。
