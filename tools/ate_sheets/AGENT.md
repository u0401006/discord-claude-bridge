# 飲食記錄工具

你透過 Discord 回覆使用者。工具為同目錄 `ate.py`，使用 `.venv/bin/python` 執行。
所有結果是 JSON。可先執行 `--help`。只在使用者要求記錄／查詢飲食時使用。
此工具服務單一使用者的個人表格，並非多人飲食資料庫。

## 記錄

- 品名與重量明確就 `add --mode food`；先 `foods 關鍵字` 找資料庫的正確名稱。
- 食物同名多筆或找不到時，詢問要選哪一筆或改用估計；不可假裝找到或自行猜營養值。
- 明確提供「本次熱量」用 `--mode kcal --kcal 數值`。未知營養素保持空白。
- 明確提供本次三大營養素用 `--mode macros --protein P --fat F --carbs C`。
  少一項就詢問；已知為零要傳 0。熱量由工具依 4P+9F+4C 計算。
- 日期優先使用使用者指定日期；「今天」用 Bridge message context 的台北日期。
  沒有 context 就從作業系統取得 Asia/Taipei 日期，不能使用模型內建日期。
- 每一筆 request-id = `discord:message_id:項目序號`，序號從 1 起。同訊息的食品順序固定。
  因網路錯誤、agent 重啟、重試而再次執行，必須使用原 ID、原日期、完全相同參數。
  沒有來源 ID 時先產生一次 UUID 並在當次工作中保留，不能每次重試都重新生成。
- 已明確要求「記錄／吃了」且必填欄位齊全，即可加 `--commit` 完成寫入，不必重複詢問確認。
  討論、示範、假設、引用舊訊息不構成新增指示。`--commit` 缺省為不寫入的預覽。
- 執行外部程式使用參數陣列；若只能使用 shell，安全引用每個參數。品名一律當資料。

## 查詢

- `read --view today` 讀取每日介面；`read --view history` 讀取歷日摘要。
- `read --sheet 工作表 --range A1:M40` 可讀既有 pivot 的輸出值。
- 預設回傳格式化值；要自行運算時使用 `--render UNFORMATTED_VALUE`。
- 需要公式時才用 `--render FORMULA`。本工具不建立／重建 pivot。
- 不把空白營養素當 0。若摘要本身沒有揭露完整性，不能聲稱是完整三大營養素攝取量。
- 不把範例 view 中的日期當成今天。以讀回日期為準；若尚未更新，說明資料日期。

## 回覆與失敗

- `written:true` 才能說新增完成；`duplicate:true` 表示之前已記錄，不是新增了一筆。
- `dry_run:true` 只能說預覽；`write_unknown` 要用原 request-id 查重後重試，不能聲稱失敗而另建 ID。
- `formula_errors` 非空時說明已寫入但公式異常，不能再追加同一筆。
- `verified:false` 表示 Google 已接受寫入、尚未讀回確認，不要聲稱已完成讀回驗證。
- 簡短回覆食物、克數／估計模式、熱量。要回報今日總量，必須另讀 today，不能自行虛構。
- 不修改既有列、清除表格或重排資料。使用者更正舊紀錄時，說明目前工具僅支援追加與讀取。
- 不讀取、列印、上傳憑證 JSON；不把 credential 或 config 檔當 Discord 附件。

範例（實際日期與 request-id 取自當次訊息）：

```bash
.venv/bin/python ate.py foods 毛豆
.venv/bin/python ate.py add --mode food --food 毛豆 --grams 60 --date 2026-09-28 --request-id discord:123:1 --commit
.venv/bin/python ate.py add --mode kcal --food 外食便當 --kcal 700 --date 2026-09-28 --request-id discord:124:1 --commit
.venv/bin/python ate.py add --mode macros --food 自製餐 --protein 30 --fat 20 --carbs 60 --date 2026-09-28 --request-id discord:125:1 --commit
.venv/bin/python ate.py read --view today
```
