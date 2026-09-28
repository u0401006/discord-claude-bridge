"""Small, synchronous Sheets adapter for an ACP agent. All output is JSON."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
from zoneinfo import ZoneInfo


class UserError(Exception):
    pass


def number(value, label, *, optional=False, positive=False):
    if value is None or value == "":
        if optional:
            return None
        raise UserError(f"缺少 {label}")
    if isinstance(value, bool):
        raise UserError(f"{label} 必須是數字")
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        raise UserError(f"{label} 必須是數字") from None
    if not result.is_finite() or result < 0 or (positive and result == 0) or result > 1000000:
        raise UserError(f"{label} 數值不合法")
    return result


def numeric(value):
    return None if value is None else float(value)


def food_name(value):
    value = str(value).strip()
    if not value or len(value) > 200 or any(ord(c) < 32 for c in value):
        raise UserError("品名須為 1–200 字，不含換行或控制字元")
    return value


def quote_sheet(name):
    return "'" + name.replace("'", "''") + "'"


def range_name(value):
    # Require a bounded rectangle: prevents an agent accidentally dumping a workbook.
    match = re.fullmatch(r"([A-Z]{1,3})([1-9]\d*)(?::([A-Z]{1,3})([1-9]\d*))?", value.upper())
    if not match:
        raise UserError("範圍須為 A1 或 A1:M40 這類有界範圍")
    def col(s):
        n = 0
        for char in s:
            n = n * 26 + ord(char) - 64
        return n
    c1, r1, c2, r2 = match.groups()
    c2, r2 = c2 or c1, r2 or r1
    if int(r2) < int(r1) or col(c2) < col(c1):
        raise UserError("範圍起訖顛倒")
    if (int(r2) - int(r1) + 1) * (col(c2) - col(c1) + 1) > 10000:
        raise UserError("一次最多讀取 10000 格，請縮小範圍")
    return value.upper()


def cell(value):
    if value is None:
        return {}
    if isinstance(value, (int, float)):
        return {"userEnteredValue": {"numberValue": value}}
    # Explicit stringValue prevents spreadsheet formula injection from food names.
    return {"userEnteredValue": {"stringValue": str(value)}}


def prepare(args, config, catalog):
    """Validate and produce one A:K row. Never invokes a write API."""
    name = food_name(args.food)
    try:
        day = date.fromisoformat(args.date) if args.date else datetime.now(ZoneInfo(config["timezone"])).date()
    except (ValueError, KeyError):
        raise UserError("日期須為 YYYY-MM-DD，並設定有效 timezone") from None
    mode = args.mode
    grams = number(args.grams, "食物克數", optional=True, positive=True)
    kcal = number(args.kcal, "熱量", optional=True)
    p, f, c, fiber = [number(getattr(args, key), label, optional=True) for key, label in
                       [("protein", "蛋白質"), ("fat", "脂肪"), ("carbs", "碳水"), ("fiber", "膳食纖維")]]
    if mode == "food":
        if grams is None:
            raise UserError("資料庫模式需要 --grams")
        if any(x is not None for x in [kcal, p, f, c, fiber]):
            raise UserError("資料庫模式不接受估計營養數字")
        matches = [row for row in catalog if row and str(row[0]).strip() == name]
        if len(matches) != 1:
            raise UserError("資料庫沒有此食品，或存在同名食品；請先搜尋／釐清，不能自動猜測")
        row = list(matches[0]) + [None] * 7
        # Preserve the exact database spelling for FILTER's equality test.
        name = str(row[0])
        base = [number(row[i], label, optional=(i != 1)) for i, label in
                enumerate(["", "熱量", "蛋白質", "脂肪", "碳水", "膳食纖維"]) if i]
        nutrients = [None if n is None else n * grams / 100 for n in base]
        estimate = None
    elif mode == "kcal":
        if kcal is None:
            raise UserError("直接估熱量模式需要 --kcal")
        if grams is not None or any(x is not None for x in [p, f, c, fiber]):
            raise UserError("直接估熱量模式只接受品名與本次熱量")
        nutrients, estimate = [kcal, None, None, None, None], kcal
    elif mode == "macros":
        if any(x is None for x in [p, f, c]):
            raise UserError("營養素模式必須提供蛋白質、脂肪、碳水；已知為零請明填 0")
        if grams is not None or kcal is not None:
            raise UserError("營養素模式不用填重量或總熱量")
        kcal = p * 4 + f * 9 + c * 4
        nutrients, estimate = [kcal, p, f, c, fiber], kcal
    else:
        raise UserError("未知模式")
    values = [(day - date(1899, 12, 30)).days, None, None, name, numeric(grams), numeric(estimate)]
    values += [numeric(x) for x in nutrients]
    cells = [cell(x) for x in values]
    cells[0]["userEnteredFormat"] = {"numberFormat": {"type": "DATE", "pattern": "yyyy/mm/dd"}}
    if mode == "food":
        # One spill formula, matching the user's existing B(name), C:G(nutrition).
        # Keep missing source nutrients blank, rather than turning them into known zeros.
        source = quote_sheet(config["food_sheet"])
        lookup = f'FILTER({source}!C$2:G,{source}!B$2:B=INDIRECT("D"&ROW()))'
        formula = f'=ARRAYFORMULA(IF({lookup}="","",{lookup}/100*INDIRECT("E"&ROW())))'
        cells[6] = {"userEnteredValue": {"formulaValue": formula}}
        cells[7:] = [{} for _ in range(4)]
    summary = {"date": day.isoformat(), "food": name, "mode": mode, "grams": numeric(grams),
               "kcal": numeric(nutrients[0]), "protein": numeric(nutrients[1]), "fat": numeric(nutrients[2]),
               "carbs": numeric(nutrients[3]), "fiber": numeric(nutrients[4])}
    return cells, summary


def config_load(path):
    path = Path(path).resolve()
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        raise UserError("無法讀取設定檔；請從 config.example.json 建立 config.json") from None
    for key in ("spreadsheet_id", "food_sheet", "log_sheet", "timezone"):
        if not isinstance(data.get(key), str) or not data[key]:
            raise UserError(f"缺少設定 {key}")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", data["spreadsheet_id"]):
        raise UserError("spreadsheet_id 格式不正確")
    for key in ("credentials_file", "state_dir"):
        if data.get(key):
            p = Path(data[key]).expanduser()
            data[key] = str(p if p.is_absolute() else path.parent / p)
    data.setdefault("state_dir", str(path.parent / ".state"))
    return data


def connect(config):
    import gspread
    # Only Sheets scope; no Drive listing, no ADC/cloud project required.
    creds = os.environ.get("ATE_CREDENTIALS_FILE") or config.get("credentials_file")
    if not creds or not Path(creds).is_file():
        raise UserError("尚未設定可讀取的 service account JSON 路徑")
    client = gspread.service_account(filename=creds, scopes=["https://www.googleapis.com/auth/spreadsheets"])
    client.set_timeout(30)
    return client.open_by_key(config["spreadsheet_id"])


@contextmanager
def writer_lock(config):
    # Shared across CLI calls AND imports on this host, including two ACP agents.
    root = Path(config["state_dir"])
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    connection = sqlite3.connect(root / "writer.sqlite3", timeout=60)
    try:
        connection.execute("BEGIN IMMEDIATE")
        yield
    finally:
        connection.rollback()
        connection.close()


def find_receipt(book, sheet, request_id):
    data = book.fetch_sheet_metadata(params={"ranges": f"{quote_sheet(sheet)}!A2:A",
        "includeGridData": True,
        "fields": "sheets(data(startRow,rowData(values(note))))"})
    for tab in data.get("sheets", []):
        for grid in tab.get("data", []):
            for index, row in enumerate(grid.get("rowData", [])):
                for value in row.get("values", []):
                    note = value.get("note", "")
                    if note.startswith("ate-sheets:"):
                        try:
                            receipt = json.loads(note[len("ate-sheets:"):])
                        except ValueError:
                            continue
                        if receipt.get("request_id") == request_id:
                            return {**receipt, "row": grid.get("startRow", 1) + index + 1}
    return None


def check_headers(ws):
    rows = list(ws.get("A1:K1"))
    row = rows[0] if rows else []
    if len(row) < 11 or str(row[0]).lower() != "date" or row[3] != "Food" or row[4] != "g" or row[5] != "估計":
        raise UserError("log 欄位與預期 A:K 不符，已停止寫入；請先核對設定與欄位")
    for index, expected in [(6, "熱量"), (7, "蛋白"), (8, "脂肪"), (9, "碳水"), (10, "膳")]:
        if expected not in str(row[index]):
            raise UserError(f"log 第 {index + 1} 欄不是預期營養素，已停止寫入")


def add(book, config, args):
    request_id = args.request_id
    if not re.fullmatch(r"[A-Za-z0-9_:.-]{1,160}", request_id):
        raise UserError("request-id 僅允許英數與 _:.-，最多 160 字")
    # Explicit date is essential: a retry after midnight must still be the same meal.
    if not args.date:
        raise UserError("add 須明確提供 --date YYYY-MM-DD，避免跨日重試記到不同日期")
    fingerprint = {k: getattr(args, k) for k in ("date", "food", "mode", "grams", "kcal", "protein", "fat", "carbs", "fiber")}
    fingerprint["spreadsheet"] = config["spreadsheet_id"]
    fingerprint["sheet"] = config["log_sheet"]
    digest = hashlib.sha256(json.dumps(fingerprint, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    with writer_lock(config):
        found = find_receipt(book, config["log_sheet"], request_id)
        if found:
            if found["digest"] != digest:
                raise UserError("相同 request-id 已被不同內容使用，不能重複寫入")
            return {"ok": True, "written": False, "duplicate": True, "receipt": found}
        catalog = book.worksheet(config["food_sheet"]).get("B2:H", value_render_option="UNFORMATTED_VALUE") if args.mode == "food" else []
        cells, summary = prepare(args, config, catalog)
        ws = book.worksheet(config["log_sheet"])
        check_headers(ws)
        receipt = {"request_id": request_id, "digest": digest, "entry": summary}
        cells[0]["note"] = "ate-sheets:" + json.dumps(receipt, ensure_ascii=False, separators=(",", ":"))
        if not args.commit:
            return {"ok": True, "written": False, "dry_run": True, "entry": summary,
                    "formula": cells[6].get("userEnteredValue", {}).get("formulaValue"),
                    "message": "加上 --commit 才會新增"}
        # Server chooses the last row atomically, including existing formulas. Never
        # overwrites a prior log, body measurement, or a formula prepared ahead of time.
        try:
            book.batch_update({"requests": [{"appendCells": {"sheetId": ws.id,
                "rows": [{"values": cells}], "fields": "userEnteredValue,userEnteredFormat.numberFormat,note"}}]})
        except Exception:
            return {"ok": False, "status": "write_unknown", "request_id": request_id,
                    "message": "未取得寫入確認；請保留相同 request-id 與內容重試，勿換新 ID"}
        try:
            verified = find_receipt(book, config["log_sheet"], request_id)
            if not verified:
                raise RuntimeError("missing receipt")
            r = verified["row"]
            values = list(ws.get(f"A{r}:K{r}", value_render_option="FORMATTED_VALUE"))
            formula_errors = [v for row in values for v in row if isinstance(v, str) and v.startswith(("#REF!", "#N/A", "#VALUE!", "#ERROR!", "#DIV/0!"))]
            return {"ok": True, "written": True, "receipt": verified, "range": f"{quote_sheet(ws.title)}!A{r}:K{r}",
                    "values": values, "formula_errors": formula_errors,
                    "message": "已寫入；公式有錯誤，請檢查" if formula_errors else "已寫入並讀回確認"}
        except Exception:
            return {"ok": True, "written": True, "verified": False, "request_id": request_id,
                    "message": "Google 已接受寫入，但讀回失敗；請使用相同 request-id 重試確認"}


def execute(args, config, book):
    if args.command == "read":
        if args.view:
            try:
                view = config["views"][args.view]
                sheet, span = view["sheet"], view["range"]
            except KeyError:
                raise UserError("未知 view，請在 config.json 的 views 設定") from None
        else:
            sheet, span = args.sheet, args.range
            if not sheet or not span:
                raise UserError("請提供 --view 或 --sheet 加 --range")
        span = range_name(span)
        values = list(book.worksheet(sheet).get(span, value_render_option=args.render))
        return {"ok": True, "sheet": sheet, "range": span, "render": args.render, "values": values}
    if args.command == "foods":
        rows = book.worksheet(config["food_sheet"]).get("B2:H", value_render_option="UNFORMATTED_VALUE")
        results = [list(row) for row in rows if row and args.query.casefold() in str(row[0]).casefold()]
        return {"ok": True, "columns": ["food", "kcal_per_100g", "protein", "fat", "carbs", "fiber", "category"],
                "matches": results[:args.limit], "total": len(results)}
    if args.command == "sheets":
        return {"ok": True, "sheets": [{"title": ws.title, "id": ws.id} for ws in book.worksheets()]}
    return add(book, config, args)


def parser():
    p = argparse.ArgumentParser(description="Discord ACP agent 的飲食 Sheets 工具，輸出 JSON")
    p.add_argument("--config", default=str(Path(__file__).with_name("config.json")))
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("sheets", help="列出工作表")
    r = sub.add_parser("read", help="讀取既有儲存格或 pivot 結果")
    selection = r.add_mutually_exclusive_group()
    selection.add_argument("--view")
    selection.add_argument("--sheet")
    r.add_argument("--range")
    r.add_argument("--render", choices=["FORMATTED_VALUE", "UNFORMATTED_VALUE", "FORMULA"], default="FORMATTED_VALUE")
    f = sub.add_parser("foods", help="搜尋食品，不自動猜測同名或近似食品")
    f.add_argument("query")
    f.add_argument("--limit", type=int, choices=range(1, 101), default=20, metavar="1-100")
    a = sub.add_parser("add", help="新增一筆飲食；預設只預覽")
    a.add_argument("--food", required=True)
    a.add_argument("--mode", required=True, choices=["food", "kcal", "macros"])
    a.add_argument("--date", required=True)
    a.add_argument("--request-id", required=True, help="建議 discord:訊息ID:項目序號")
    for name in ("grams", "kcal", "protein", "fat", "carbs", "fiber"):
        a.add_argument("--" + name)
    a.add_argument("--commit", action="store_true")
    return p


def main():
    args = parser().parse_args()
    try:
        config = config_load(args.config)
        output = execute(args, config, connect(config))
    except UserError as error:
        output = {"ok": False, "error": str(error)}
    except ImportError:
        output = {"ok": False, "error": "缺少依賴，請使用專案 .venv 並安裝 requirements.txt"}
    except Exception as error:
        # SDK exceptions can embed request URLs, server payloads or credentials.
        output = {"ok": False, "error": type(error).__name__,
                  "message": "操作失敗，請檢查憑證、表格分享權限、網路與欄位；不輸出敏感錯誤內容"}
    print(json.dumps(output, ensure_ascii=False, allow_nan=False))
    return 0 if output.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
