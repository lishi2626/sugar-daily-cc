from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from tempfile import NamedTemporaryFile
from urllib.parse import quote_plus
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PUBLIC_ROOT = PROJECT_ROOT / "public" / "sugar-news"
METRICS_ROOT = PUBLIC_ROOT / "data" / "india_metrics"
HISTORY_PATH = METRICS_ROOT / "history.json"
try:
    SHANGHAI = ZoneInfo("Asia/Shanghai")
except Exception:
    SHANGHAI = timezone(timedelta(hours=8), name="Asia/Shanghai")

FCA_URL = "https://fcainfoweb.nic.in/"
CHINIMANDI_SEARCH = "https://www.chinimandi.com/?s="
INVENTORY_SEARCH_QUERIES = (
    "India sugar closing stock latest",
    "India sugar ending stocks current season",
    "India sugar carryover stock September",
    "ISMA sugar closing stock",
    "ICRA India sugar closing inventory",
    "India sugar balance sheet closing stocks",
    "India sugar pipeline stocks",
)


def beijing_now() -> datetime:
    fixed = os.getenv("SUGAR_NEWS_NOW")
    if fixed:
        return datetime.fromisoformat(fixed).astimezone(SHANGHAI)
    return datetime.now(SHANGHAI)


def fetch_url(url: str, timeout: int = 25) -> tuple[str, int]:
    req = Request(url, headers={"User-Agent": "Mozilla/5.0 SugarNewsBot/1.0"})
    with urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8", "ignore")
        return body, resp.status


class TableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[dict] = []
        self._table_stack: list[dict] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None
        self._capture_depth = 0

    def handle_starttag(self, tag, attrs):
        attrs_dict = dict(attrs)
        if tag == "table":
            self._table_stack.append({"id": attrs_dict.get("id", ""), "caption": "", "rows": []})
        elif tag == "caption" and self._table_stack:
            self._capture_depth = 1
            self._cell = []
        elif tag == "tr" and self._table_stack:
            self._row = []
        elif tag in {"td", "th"} and self._row is not None:
            self._cell = []

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag):
        if tag in {"td", "th"} and self._row is not None and self._cell is not None:
            text = re.sub(r"\s+", " ", html.unescape("".join(self._cell))).strip()
            self._row.append(text)
            self._cell = None
        elif tag == "caption" and self._table_stack and self._cell is not None:
            self._table_stack[-1]["caption"] = re.sub(r"\s+", " ", "".join(self._cell)).strip()
            self._cell = None
        elif tag == "tr" and self._table_stack and self._row is not None:
            if any(self._row):
                self._table_stack[-1]["rows"].append(self._row)
            self._row = None
        elif tag == "table" and self._table_stack:
            self.tables.append(self._table_stack.pop())


def number(text: str | int | float | None) -> float | None:
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return float(text)
    match = re.search(r"-?\d+(?:,\d{3})*(?:\.\d+)?|-?\d+(?:\.\d+)?", str(text))
    if not match:
        return None
    return float(match.group(0).replace(",", ""))


def round2(value: float | None) -> float | None:
    return None if value is None else round(value, 2)


def inr_per_quintal_to_kg(value: float | None) -> float | None:
    return None if value is None else round(value / 100, 4)


def parse_fca_prices(target_date: str, history: dict) -> tuple[dict | None, dict]:
    log = {"source": "FCA price monitoring", "url": FCA_URL, "requestedAt": beijing_now().isoformat(timespec="seconds")}
    try:
        body, status = fetch_url(FCA_URL)
        log["httpStatus"] = status
    except Exception as exc:
        log["error"] = str(exc)
        return None, log
    parser = TableParser()
    parser.feed(body)
    retail_date = re.search(r"All India Average Retail Price.*?As on\s*<[^>]+>([^<]+)", body, re.I | re.S)
    wholesale_date = re.search(r"All India Average Wholesale Price.*?As on\s*<[^>]+>([^<]+)", body, re.I | re.S)
    data_date = None
    if retail_date:
        data_date = datetime.strptime(retail_date.group(1).strip(), "%d/%m/%Y").date().isoformat()
    elif wholesale_date:
        data_date = datetime.strptime(wholesale_date.group(1).strip(), "%d/%m/%Y").date().isoformat()

    retail_price = None
    wholesale_price = None
    for table in parser.tables:
        caption = table.get("caption", "")
        table_id = table.get("id", "")
        for row in table.get("rows", []):
            if len(row) >= 2 and row[0].strip().lower() == "sugar":
                if "Retail" in caption or "Retail" in table_id:
                    retail_price = number(row[1])
                if "Wholesale" in caption or "Wholesale" in table_id:
                    wholesale_price = number(row[1])
    if retail_price is None and wholesale_price is None:
        log["error"] = "Sugar row not found in FCA retail/wholesale tables"
        return None, log
    previous = latest_record(history, "india_domestic_price", exclude_date=data_date)
    previous_wholesale = previous.get("wholesale_price_inr_per_quintal") if previous else None
    change_value = None
    change_percent = None
    if wholesale_price is not None and previous_wholesale is not None:
        change_value = wholesale_price - float(previous_wholesale)
        if previous_wholesale:
            change_percent = change_value / float(previous_wholesale) * 100
    record = {
        "indicator": "india_domestic_price",
        "data_date": data_date or target_date,
        "wholesale_price_inr_per_quintal": round2(wholesale_price),
        "wholesale_price_inr_per_kg": inr_per_quintal_to_kg(wholesale_price),
        "retail_price_inr_per_kg": round2(retail_price),
        "previous_value": round2(previous_wholesale),
        "change_value": round2(change_value),
        "change_percent": round2(change_percent),
        "source_name": "Department of Consumer Affairs Price Monitoring",
        "source_url": FCA_URL,
        "fetched_at": beijing_now().isoformat(timespec="seconds"),
        "status": "ok",
    }
    log.update({"parsed": True, "dataDate": record["data_date"], "retailPrice": retail_price, "wholesalePrice": wholesale_price})
    return record, log


def chinimandi_candidate_urls(target_date: str) -> list[str]:
    dt = datetime.strptime(target_date, "%Y-%m-%d")
    ddmmyyyy = dt.strftime("%d/%m/%Y")
    search_terms = (
        f"Daily Sugar Market Update {ddmmyyyy}",
        "Uttar Pradesh sugar ex-mill price today",
        "UP M/30 sugar ex-mill rate",
        "Muzaffarnagar M-grade sugar price",
        "site:chinimandi.com Daily Sugar Market Update",
    )
    urls = []
    slug_date = dt.strftime("%d-%m-%Y")
    urls.append(f"https://www.chinimandi.com/daily-sugar-market-update-by-vizzie-{slug_date}/")
    urls.extend(CHINIMANDI_SEARCH + quote_plus(term) for term in search_terms)
    return urls


def parse_price_range(text: str) -> tuple[float | None, float | None]:
    cleaned = html.unescape(text).replace("₹", "").replace("Rs.", "").replace("Rs", "")
    nums = [number(x) for x in re.findall(r"\d[\d,]*(?:\.\d+)?", cleaned)]
    nums = [x for x in nums if x is not None]
    if not nums:
        return None, None
    if len(nums) == 1:
        return nums[0], nums[0]
    return nums[0], nums[1]


def parse_chinimandi_up_exmill(target_date: str, history: dict) -> tuple[dict | None, list[dict]]:
    logs = []
    for url in chinimandi_candidate_urls(target_date):
        log = {"source": "ChiniMandi", "url": url, "requestedAt": beijing_now().isoformat(timespec="seconds")}
        try:
            body, status = fetch_url(url)
            log["httpStatus"] = status
        except Exception as exc:
            log["error"] = str(exc)
            logs.append(log)
            continue
        if "Ex-mill Sugar Prices" not in body or "Uttar Pradesh" not in body:
            log["parsed"] = False
            log["reason"] = "ex-mill Uttar Pradesh table not found"
            logs.append(log)
            continue
        date_match = re.search(r"Ex-mill Sugar Prices as on\s+([A-Za-z]+),?\s*(\d{1,2})\s+(\d{4})", body, re.I)
        data_date = target_date
        if date_match:
            data_date = datetime.strptime(" ".join(date_match.groups()), "%B %d %Y").date().isoformat()
        parser = TableParser()
        parser.feed(body)
        for table in parser.tables:
            rows = table.get("rows", [])
            for row in rows:
                if row and row[0].strip().lower() == "uttar pradesh":
                    m30_cell = row[2] if len(row) >= 3 else row[-1]
                    low, high = parse_price_range(m30_cell)
                    if low is None:
                        continue
                    previous = latest_record(history, "up_ex_mill_price", exclude_date=data_date)
                    prev_min = previous.get("up_ex_mill_min_inr_per_quintal") if previous else None
                    prev_max = previous.get("up_ex_mill_max_inr_per_quintal") if previous else None
                    midpoint = (low + high) / 2
                    prev_midpoint = (float(prev_min) + float(prev_max)) / 2 if prev_min is not None and prev_max is not None else None
                    change_value = midpoint - prev_midpoint if prev_midpoint is not None else None
                    record = {
                        "indicator": "up_ex_mill_price",
                        "data_date": data_date,
                        "up_ex_mill_min_inr_per_quintal": round2(low),
                        "up_ex_mill_max_inr_per_quintal": round2(high),
                        "up_ex_mill_min_inr_per_kg": inr_per_quintal_to_kg(low),
                        "up_ex_mill_max_inr_per_kg": inr_per_quintal_to_kg(high),
                        "previous_min": round2(prev_min),
                        "previous_max": round2(prev_max),
                        "change_value": round2(change_value),
                        "change_direction": "up" if change_value and change_value > 0 else "down" if change_value and change_value < 0 else "flat" if change_value == 0 else "unknown",
                        "gst_status": "excluding GST" if re.search(r"excluding GST", body, re.I) else "unknown",
                        "source_name": "ChiniMandi",
                        "source_url": url,
                        "fetched_at": beijing_now().isoformat(timespec="seconds"),
                        "status": "ok",
                    }
                    log.update({"parsed": True, "dataDate": data_date, "min": low, "max": high})
                    logs.append(log)
                    return record, logs
        log["parsed"] = False
        log["reason"] = "Uttar Pradesh row not parsed"
        logs.append(log)
    return None, logs


def google_news_rss(query: str) -> str:
    return "https://news.google.com/rss/search?q=" + quote_plus(query) + "&hl=en-US&gl=US&ceid=US:en"


def parse_inventory_from_search(target_date: str, history: dict) -> tuple[list[dict], list[dict]]:
    logs = []
    # Inventory forecasts require explicit season and stock wording; this
    # search stage records candidates, but does not publish unverified numbers.
    for query in INVENTORY_SEARCH_QUERIES:
        url = google_news_rss(f"{query} {target_date}")
        log = {"source": "Google News RSS inventory discovery", "query": query, "url": url, "requestedAt": beijing_now().isoformat(timespec="seconds")}
        try:
            body, status = fetch_url(url)
            log["httpStatus"] = status
            log["candidateCount"] = len(re.findall(r"<item>", body))
            log["parsed"] = False
            log["reason"] = "Inventory candidates require source-page season and closing-stock verification before publication."
        except Exception as exc:
            log["error"] = str(exc)
        logs.append(log)
    return [], logs


def load_history() -> dict:
    if HISTORY_PATH.exists():
        with HISTORY_PATH.open("r", encoding="utf-8") as f:
            return json.load(f)
    return {"version": 1, "records": [], "lastUpdatedAt": None}


def atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile("w", encoding="utf-8", delete=False, dir=str(path.parent), suffix=".tmp") as tmp:
        json.dump(data, tmp, ensure_ascii=False, indent=2)
        temp_name = tmp.name
    Path(temp_name).replace(path)


def record_key(record: dict) -> tuple:
    if record["indicator"] == "carryover_stock":
        return (record["indicator"], record.get("season"), record.get("forecast_organization"), record.get("forecast_date"))
    return (record["indicator"], record.get("data_date"), record.get("source_name"))


def latest_record(history: dict, indicator: str, exclude_date: str | None = None) -> dict | None:
    rows = [r for r in history.get("records", []) if r.get("indicator") == indicator and r.get("status") == "ok"]
    if exclude_date:
        rows = [r for r in rows if r.get("data_date") != exclude_date and r.get("forecast_date") != exclude_date]
    rows.sort(key=lambda r: (r.get("data_date") or r.get("forecast_date") or "", r.get("fetched_at") or ""), reverse=True)
    return rows[0] if rows else None


def upsert_records(history: dict, records: list[dict]) -> dict:
    existing = {record_key(r): r for r in history.get("records", [])}
    for record in records:
        if not record:
            continue
        existing[record_key(record)] = record
    history["records"] = sorted(existing.values(), key=lambda r: (r.get("indicator", ""), r.get("data_date") or r.get("forecast_date") or "", r.get("fetched_at") or ""))
    history["lastUpdatedAt"] = beijing_now().isoformat(timespec="seconds")
    return history


def build_snapshot(history: dict, target_date: str, logs: list[dict]) -> dict:
    domestic = latest_record(history, "india_domestic_price")
    up_ex = latest_record(history, "up_ex_mill_price")
    stock_records = [r for r in history.get("records", []) if r.get("indicator") == "carryover_stock" and r.get("status") == "ok"]
    stock_records.sort(key=lambda r: (r.get("forecast_date") or "", r.get("fetched_at") or ""), reverse=True)
    main_stock = stock_records[0] if stock_records else None
    return {
        "targetDate": target_date,
        "updatedAt": beijing_now().isoformat(timespec="seconds"),
        "domesticSugarPrice": domestic,
        "upExMillPrice": up_ex,
        "carryoverStock": main_stock,
        "carryoverStockForecasts": stock_records[:10],
        "fetchLog": logs,
    }


def collect(target_date: str) -> dict:
    history = load_history()
    logs: list[dict] = []
    records: list[dict] = []
    domestic, log = parse_fca_prices(target_date, history)
    logs.append(log)
    if domestic:
        records.append(domestic)
    up_ex, up_logs = parse_chinimandi_up_exmill(target_date, history)
    logs.extend(up_logs)
    if up_ex:
        records.append(up_ex)
    inventory_records, inventory_logs = parse_inventory_from_search(target_date, history)
    logs.extend(inventory_logs)
    records.extend(inventory_records)
    if records:
        history = upsert_records(history, records)
        atomic_write_json(HISTORY_PATH, history)
    snapshot = build_snapshot(history, target_date, logs)
    atomic_write_json(METRICS_ROOT / "latest.json", snapshot)
    return snapshot


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch India sugar price and stock metrics.")
    parser.add_argument("--date", required=True)
    args = parser.parse_args()
    snapshot = collect(args.date)
    print(json.dumps(snapshot, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
