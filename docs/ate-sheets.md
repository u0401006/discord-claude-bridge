# Discord agent → gspread → Google Sheets

不需要 GAS、LINE、LIFF 或 HTTP server。Bridge 呼叫既有 Claude Code / Codex CLI，agent 執行本機 Python 工具，工具回傳 JSON。
`openai-adapter.py` 目前沒有 shell tool calling，不適用此整合。

## 安裝

需要 Python 3.10+（建議 3.11）。不更動 bridge 原本的 Python 依賴：

```bash
cd tools/ate_sheets
python3.11 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp config.example.json config.json
```

編輯 `config.json`：

- `spreadsheet_id`：填入 ate-2026 網址 `/d/` 與 `/edit` 之間的 ID。
- `credentials_file`：Google service account JSON 的絕對路徑，放在 repo 與附件允許目錄之外，檔案權限限服務帳號使用。
  也可用 `ATE_CREDENTIALS_FILE` 指定路徑。不把金鑰內容放進 `.env`、對話或 Git。
- `food_sheet` / `log_sheet`：目前為 `資料庫` / `log`。
- `views`：命名讀取範圍。範例 `today!A1:M40`、`today!A6:D40` 來自既有畫面；實際 pivot 放其他位置時改設定即可。
- `state_dir`：本機 SQLite 寫入鎖目錄；同一表格的所有 agent 必須共用同一路徑。

在 Google Cloud Console 啟用 Sheets API，建立或使用既有 service account，取得 JSON 憑證。
把**指定試算表**分享給該帳號的 `client_email`，新增紀錄需編輯者權限。
工具只要求 Sheets OAuth scope，以 ID 開啟表格，不需要 Drive scope 或列出雲端硬碟。
不要設定「知道連結的任何人都可編輯」。不需要公開工作表。

## Bridge 設定

在實際啟動 bot 的 `.env` 加上：

```dotenv
ATE_SHEETS_DIR=/absolute/path/to/discord-claude-bridge/tools/ate_sheets
ALLOWED_USER_IDS=你的Discord使用者ID
ALLOWED_CHANNEL_IDS=你的私人飲食頻道ID
SESSION_SCOPE=user
```

保留既有 `CLAUDE_BIN`、模型、WORKING_DIR 設定。重新啟動 bridge 後生效。
建議使用私人專用頻道；回覆會傳到觸發指令的頻道。這是單一人的工作簿，不會按 Discord 使用者分離資料。
agent 執行環境必須允許執行工具與連到 Google API；本改動不調整既有 sandbox／approval 設定。

啟用後每次 agent 提示包含工具說明路徑；每則 buffered 訊息附帶來源 Discord message ID、user ID、台北日期。
Claude Code 與 Codex CLI 共用同一工具，無需各寫一套營養計算。

## 指令

在工具目錄執行：

```bash
.venv/bin/python ate.py sheets
.venv/bin/python ate.py foods 毛豆
.venv/bin/python ate.py read --view today
.venv/bin/python ate.py read --sheet today --range A6:D40
.venv/bin/python ate.py read --sheet log --range A1:K5 --render FORMULA

# 預覽不寫入；真的要記錄加 --commit。
.venv/bin/python ate.py add --mode food --food 毛豆 --grams 60 --date 2026-09-28 --request-id discord:123:1
.venv/bin/python ate.py add --mode kcal --food 外食便當 --kcal 700 --date 2026-09-28 --request-id discord:124:1
.venv/bin/python ate.py add --mode macros --food 自製餐 --protein 30 --fat 20 --carbs 60 --date 2026-09-28 --request-id discord:125:1
```

三種模式的數值與日期皆先驗證。讀取預設使用 Google 回傳的顯示值，pivot 與公式仍由 Sheets 運算。
`read` 不會重新設定 pivot；固定範圍的 pivot 必須原本就涵蓋新列，否則需另外授權調整。

## 欄位與相容性

食物資料庫固定讀取 B:H：B 品名，C 熱量，D 蛋白質，E 脂肪，F 碳水，G 膳食纖維，H 種類，皆以 100g 為基準。
只搜尋與讀取，不修改食品資料庫。

log 固定 A:K：Date、體重、Fat、Food、g、估計、熱量、蛋白質、脂肪、碳水、膳食纖維。
寫入前核對表頭。不新增工作表、不新增欄位，體重與 Fat 留白。

- `food`：寫入日期、食品、克數，以及 G 欄的動態 FILTER / ARRAYFORMULA，展開 G:K。
  與既有公式一樣按每 100g 換算；資料庫熱量優先，不用 4/9/4 取代。資料庫日後修改時也會跟著重新計算。
  與原公式稍有差異：來源空白營養素保留空白，避免未知被當成零。
- `kcal`：F 與 G 寫入本次估計熱量，H:K 空白。
- `macros`：F、G 為 4P+9F+4C，H:J 為本次營養素克數，K 可選填膳食纖維。

採用 Sheets `appendCells` 原子追加，在整張工作表最後有內容的列之後新增，**不覆蓋預填公式或舊紀錄**。
若 log 事先把公式填到很後面，新紀錄也會從那些公式之後開始；這是避免覆寫的取捨。
全欄引用會包含新列；固定範圍 pivot 不一定會包含。第一次正式使用前請確認追加位置與檢視範圍。
日期寫為真正日期數值並設定日期格式；名稱用明確 stringValue，不能注入公式。

## 重試與限制

每筆 A 欄 note 儲存來源 request-id、內容摘要與雜湊；note 與整列在同一請求寫入。
重試先查 note，再決定是否追加。請保留 notes；不要清除後使用同一 ID 重送。
單主機 agent 共用 SQLite writer lock，可防止同時通過查重。
**這不是跨主機分散式鎖**；不要讓兩台機器同時執行相同表格的寫入。
網路超時回 `write_unknown` 時仍可能已寫入，必須使用完全相同 ID 與參數重試。
不提供任意範圍寫入、刪除或更新舊紀錄，避免 agent 意外覆蓋既有試算表功能。

## 驗證

```bash
# 工具單元測試：使用假的 Sheets adapter，不接觸真實資料
cd tools/ate_sheets
.venv/bin/python -m unittest -v

# repo 根目錄：bridge 與整合測試
python3.11 -m unittest discover -s tests -v
```

首次連線先執行 `sheets`、`read --sheet log --range A1:K5`、`foods 毛豆`，再做不含 `--commit` 的預覽。
最後用**真正要記錄的一筆飲食**加 `--commit`，讀回 log 與 today 確認，不需要添加測試餐點。
目前沒有提供憑證，因此只完成離線測試，尚未驗證正式表格權限、Google 原生公式重算與 Discord 完整流程。

參考：[gspread authentication](https://docs.gspread.org/en/master/oauth2.html)、[Google AppendCellsRequest](https://developers.google.com/workspace/sheets/api/reference/rest/v4/spreadsheets/request#AppendCellsRequest)。
