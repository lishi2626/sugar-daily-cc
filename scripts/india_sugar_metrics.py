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
    "Department of Food and Public Distribution sugar stock",
    "Department of Food & Public Distribution sugar stock",
    "India sugar balance sheet",
    "India sugar closing stock",
    "India sugar opening stock",
    "sugar stock position India",
    "site:dfpd.gov.in sugar stock",
    "site:gov.in India sugar closing stock",
    "ISMA sugar closing stock",
    "ISMA sugar balance sheet",
    "ISMA sugar production estimate",
    "ISMA opening stock and closing stock",
    "site:ismaindia.com closing stock sugar",
    "NFCSF India sugar closing stock",
    "NFCSF sugar balance sheet",
    "NFCSF sugar production estimate",
    "National Federation Cooperative Sugar Factories closing stock",
    "site:coopsugar.org closing stock",
)
MARKET_FORECAST_SEARCH_QUERIES = (
    "ICRA India sugar closing inventory",
    "India sugar carryover stock market forecast",
    "India sugar ending stocks agency forecast",
    "India sugar balance sheet closing stocks rating agency",
)
AUTHORIZED_STOCK_SOURCES = ("Government of India", "Department of Food and Public Distribution", "ISMA", "NFCSF")
AUTHORIZED_STOCK_PRIORITY = {
    "Government of India": 0,
    "Department of Food and Public Distribution": 0,
    "ISMA": 1,
    "NFCSF": 2,
}


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


def percent_change(current: float | None, previous: float | None) -> float | None:
    if current is None or previous is None or previous == 0:
        return None
    return (current - previous) / previous * 100


def history_indicators(indicator: str) -> set[str]:
    if indicator in {"india_wholesale_price", "india_retail_price"}:
        return {indicator, "india_domestic_price"}
    return {indicator}


def history_value(record: dict | None, indicator: str, value_kind: str) -> float | None:
    if not record:
        return None
    if indicator == "india_wholesale_price":
        if value_kind == "price":
            return number(record.get("price_inr_per_quintal") or record.get("wholesale_price_inr_per_quintal"))
    if indicator == "india_retail_price":
        if value_kind == "price":
            return number(record.get("price_inr_per_kg") or record.get("retail_price_inr_per_kg"))
    return number(record.get(value_kind))


def latest_record_before(history: dict, indicator: str, data_date: str | None) -> dict | None:
    allowed = history_indicators(indicator)
    rows = [
        r for r in history.get("records", [])
        if r.get("indicator") in allowed and r.get("status") == "ok"
    ]
    if data_date:
        rows = [r for r in rows if (r.get("data_date") or r.get("forecast_date") or "") < data_date]
    rows.sort(key=lambda r: (r.get("data_date") or r.get("forecast_date") or "", r.get("fetched_at") or ""), reverse=True)
    return rows[0] if rows else None


def comparable_yoy_record(history: dict, indicator: str, data_date: str | None, max_days: int = 3) -> dict | None:
    if not data_date:
        return None
    try:
        target = datetime.fromisoformat(data_date).date().replace(year=int(data_date[:4]) - 1)
    except Exception:
        return None
    allowed = history_indicators(indicator)
    rows = [
        r for r in history.get("records", [])
        if r.get("indicator") in allowed and r.get("status") == "ok" and r.get("data_date")
    ]
    best = None
    best_delta = None
    for row in rows:
        try:
            row_date = datetime.fromisoformat(row["data_date"]).date()
        except Exception:
            continue
        delta = abs((row_date - target).days)
        if delta <= max_days and (best_delta is None or delta < best_delta):
            best = row
            best_delta = delta
    return best


def parse_fca_prices(target_date: str, history: dict) -> tuple[list[dict], dict]:
    log = {"source": "FCA price monitoring", "url": FCA_URL, "requestedAt": beijing_now().isoformat(timespec="seconds")}
    try:
        body, status = fetch_url(FCA_URL)
        log["httpStatus"] = status
    except Exception as exc:
        log["error"] = str(exc)
        return [], log
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
        return [], log
    date_value = data_date or target_date
    records: list[dict] = []

    if wholesale_price is not None:
        previous = latest_record_before(history, "india_wholesale_price", date_value)
        previous_value = history_value(previous, "india_wholesale_price", "price")
        yoy = comparable_yoy_record(history, "india_wholesale_price", date_value)
        yoy_value = history_value(yoy, "india_wholesale_price", "price")
        records.append({
            "indicator": "india_wholesale_price",
            "data_date": date_value,
            "price_inr_per_quintal": round2(wholesale_price),
            "price_inr_per_kg": inr_per_quintal_to_kg(wholesale_price),
            "previous_data_date": previous.get("data_date") if previous else None,
            "previous_value": round2(previous_value),
            "change_value": round2(wholesale_price - float(previous_value)) if previous_value is not None else None,
            "change_percent": round2(percent_change(wholesale_price, float(previous_value))) if previous_value is not None else None,
            "previous_year_date": yoy.get("data_date") if yoy else None,
            "previous_year_value": round2(yoy_value),
            "year_on_year_change": round2(wholesale_price - float(yoy_value)) if yoy_value is not None else None,
            "year_on_year_change_percent": round2(percent_change(wholesale_price, float(yoy_value))) if yoy_value is not None else None,
            "source_name": "Department of Consumer Affairs Price Monitoring",
            "source_url": FCA_URL,
            "fetched_at": beijing_now().isoformat(timespec="seconds"),
            "status": "ok",
        })

    if retail_price is not None:
        previous = latest_record_before(history, "india_retail_price", date_value)
        previous_value = history_value(previous, "india_retail_price", "price")
        yoy = comparable_yoy_record(history, "india_retail_price", date_value)
        yoy_value = history_value(yoy, "india_retail_price", "price")
        records.append({
            "indicator": "india_retail_price",
            "data_date": date_value,
            "price_inr_per_kg": round2(retail_price),
            "previous_data_date": previous.get("data_date") if previous else None,
            "previous_value": round2(previous_value),
            "change_value": round2(retail_price - float(previous_value)) if previous_value is not None else None,
            "change_percent": round2(percent_change(retail_price, float(previous_value))) if previous_value is not None else None,
            "previous_year_date": yoy.get("data_date") if yoy else None,
            "previous_year_value": round2(yoy_value),
            "year_on_year_change": round2(retail_price - float(yoy_value)) if yoy_value is not None else None,
            "year_on_year_change_percent": round2(percent_change(retail_price, float(yoy_value))) if yoy_value is not None else None,
            "source_name": "Department of Consumer Affairs Price Monitoring",
            "source_url": FCA_URL,
            "fetched_at": beijing_now().isoformat(timespec="seconds"),
            "status": "ok",
        })

    log.update({"parsed": True, "dataDate": date_value, "retailPrice": retail_price, "wholesalePrice": wholesale_price})
    return records, log


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
                    yoy = comparable_yoy_record(history, "up_ex_mill_price", data_date)
                    yoy_low = yoy.get("up_ex_mill_min_inr_per_quintal") if yoy else None
                    yoy_high = yoy.get("up_ex_mill_max_inr_per_quintal") if yoy else None
                    yoy_midpoint = (float(yoy_low) + float(yoy_high)) / 2 if yoy_low is not None and yoy_high is not None else None
                    record = {
                        "indicator": "up_ex_mill_price",
                        "data_date": data_date,
                        "grade": "M/30",
                        "region": "Uttar Pradesh",
                        "quote_type": "ex-mill",
                        "up_ex_mill_min_inr_per_quintal": round2(low),
                        "up_ex_mill_max_inr_per_quintal": round2(high),
                        "up_ex_mill_mid_inr_per_quintal": round2(midpoint),
                        "up_ex_mill_min_inr_per_kg": inr_per_quintal_to_kg(low),
                        "up_ex_mill_max_inr_per_kg": inr_per_quintal_to_kg(high),
                        "up_ex_mill_mid_inr_per_kg": inr_per_quintal_to_kg(midpoint),
                        "previous_data_date": previous.get("data_date") if previous else None,
                        "previous_min": round2(prev_min),
                        "previous_max": round2(prev_max),
                        "previous_mid": round2(prev_midpoint),
                        "change_value": round2(change_value),
                        "change_percent": round2(percent_change(midpoint, prev_midpoint)),
                        "previous_year_date": yoy.get("data_date") if yoy else None,
                        "previous_year_min": round2(yoy_low),
                        "previous_year_max": round2(yoy_high),
                        "previous_year_mid": round2(yoy_midpoint),
                        "year_on_year_change": round2(midpoint - yoy_midpoint) if yoy_midpoint is not None else None,
                        "year_on_year_change_percent": round2(percent_change(midpoint, yoy_midpoint)),
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
            log["sourceTier"] = "authoritative_main_value_only"
            log["allowedMainSources"] = list(AUTHORIZED_STOCK_SOURCES)
            log["reason"] = "Inventory candidates require source-page season, organization and closing-stock verification before publication."
        except Exception as exc:
            log["error"] = str(exc)
        logs.append(log)
    for query in MARKET_FORECAST_SEARCH_QUERIES:
        url = google_news_rss(f"{query} {target_date}")
        log = {"source": "Google News RSS market forecast discovery", "query": query, "url": url, "requestedAt": beijing_now().isoformat(timespec="seconds")}
        try:
            body, status = fetch_url(url)
            log["httpStatus"] = status
            log["candidateCount"] = len(re.findall(r"<item>", body))
            log["parsed"] = False
            log["sourceTier"] = "market_forecast_comparison_only"
            log["reason"] = "Market forecasts are comparison-only and require original report date, season and closing-stock verification before publication."
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
    if record["indicator"] in {"carryover_stock", "market_carryover_forecast"}:
        return (record["indicator"], record.get("season"), record.get("forecast_organization"), record.get("forecast_date"))
    if record["indicator"] == "up_ex_mill_price":
        return (record["indicator"], record.get("grade"), record.get("region"), record.get("data_date"), record.get("source_name"))
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
    wholesale = latest_record(history, "india_wholesale_price")
    retail = latest_record(history, "india_retail_price")
    up_ex = latest_record(history, "up_ex_mill_price")
    stock_records = [
        r for r in history.get("records", [])
        if r.get("indicator") == "carryover_stock"
        and r.get("status") == "ok"
        and (r.get("forecast_organization") or r.get("source_name")) in AUTHORIZED_STOCK_PRIORITY
    ]
    stock_records.sort(key=lambda r: (AUTHORIZED_STOCK_PRIORITY.get(r.get("forecast_organization") or r.get("source_name"), 99), -(int((r.get("forecast_date") or "0000-00-00").replace("-", "")) if re.match(r"\d{4}-\d{2}-\d{2}", r.get("forecast_date") or "") else 0)))
    main_stock = stock_records[0] if stock_records else None
    market_forecasts = [r for r in history.get("records", []) if r.get("indicator") == "market_carryover_forecast" and r.get("status") == "ok"]
    market_forecasts.sort(key=lambda r: (r.get("forecast_date") or "", r.get("fetched_at") or ""), reverse=True)
    return {
        "targetDate": target_date,
        "updatedAt": beijing_now().isoformat(timespec="seconds"),
        "domesticWholesalePrice": wholesale,
        "domesticRetailPrice": retail,
        "domesticSugarPrice": wholesale,
        "upExMillPrice": up_ex,
        "carryoverStock": main_stock,
        "authorizedCarryoverStockAlternatives": stock_records[1:10],
        "carryoverStockForecasts": market_forecasts[:10],
        "fetchLog": logs,
    }


def collect(target_date: str) -> dict:
    history = load_history()
    logs: list[dict] = []
    records: list[dict] = []
    fca_records, log = parse_fca_prices(target_date, history)
    logs.append(log)
    records.extend(fca_records)
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
