from __future__ import annotations

import argparse
import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile
from urllib.parse import quote_plus
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PUBLIC_ROOT = PROJECT_ROOT / "public" / "sugar-news"
METRICS_ROOT = PUBLIC_ROOT / "data" / "brazil_metrics"
HISTORY_PATH = METRICS_ROOT / "history.json"
try:
    SHANGHAI = ZoneInfo("Asia/Shanghai")
except Exception:
    SHANGHAI = timezone(timedelta(hours=8), name="Asia/Shanghai")

ANP_OPEN_DATA_URL = "https://www.gov.br/anp/pt-br/centrais-de-conteudo/dados-abertos/arquivos"
ANP_DYNAMIC_PANELS_URL = "https://www.gov.br/anp/pt-br/centrais-de-conteudo/paineis-dinamicos-da-anp/paineis-dinamicos-sobre-combustiveis"
ANP_ETHANOL_PRODUCTION_CSV_VIEW = "https://www.gov.br/anp/pt-br/centrais-de-conteudo/dados-abertos/arquivos/arquivos-painel-de-produtores-de-derivados-producao-de-biocombustiveis/f-etanol-producao.csv/view"

PREMIUM_QUERIES = (
    "Brazil VHP sugar premium latest",
    "Brazil raw sugar premium FOB Santos",
    "Brazil sugar basis ICE No.11",
    "Brazil VHP sugar discount",
    "Santos sugar premium",
    "Paranagua sugar premium",
    "Brazilian sugar physical premium",
    "premio acucar VHP Brasil",
    "premio acucar Santos",
    "acucar VHP FOB Santos premio",
)
ANP_SUGAR_STOCK_QUERIES = (
    "site:gov.br/anp estoque acucar",
    "site:gov.br/anp acucar estoque dados abertos ANP",
    "site:gov.br/anp estoque de acucar Brasil ANP",
)
ANP_ETHANOL_STOCK_QUERIES = (
    "estoque de etanol ANP",
    "estoque de etanol hidratado",
    "estoque de etanol anidro",
    "dados abertos ANP etanol estoque",
    "armazenamento de etanol Brasil ANP",
    "estoque mensal etanol ANP",
    "painel dinamico etanol ANP estoque",
)


def beijing_now() -> datetime:
    fixed = os.getenv("SUGAR_NEWS_NOW")
    if fixed:
        return datetime.fromisoformat(fixed).astimezone(SHANGHAI)
    return datetime.now(SHANGHAI)


def fetch_url(url: str, timeout: int = 8) -> tuple[str, int]:
    req = Request(url, headers={"User-Agent": "Mozilla/5.0 SugarNewsBot/1.0"})
    with urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "ignore"), resp.status


def google_news_rss(query: str) -> str:
    return "https://news.google.com/rss/search?q=" + quote_plus(query) + "&hl=en-US&gl=US&ceid=US:en"


def load_history() -> dict:
    if HISTORY_PATH.exists():
        with HISTORY_PATH.open("r", encoding="utf-8") as f:
            return json.load(f)
    return {"version": 1, "records": [], "lastUpdatedAt": None}


def atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile("w", encoding="utf-8", delete=False, dir=str(path.parent), suffix=".tmp") as tmp:
        json.dump(data, tmp, ensure_ascii=False, indent=2)
        tmp_name = tmp.name
    Path(tmp_name).replace(path)


def round2(value: float | None) -> float | None:
    return None if value is None else round(float(value), 2)


def yoy_fields(current: float | None, previous: float | None) -> dict:
    if current is None or previous is None:
        return {
            "previous_year_value": previous,
            "year_on_year_change": None,
            "year_on_year_change_percent": None,
            "yoy_status": "insufficient",
        }
    change = current - previous
    pct = None if previous == 0 else change / previous * 100
    return {
        "previous_year_value": round2(previous),
        "year_on_year_change": round2(change),
        "year_on_year_change_percent": round2(pct),
        "yoy_status": "ok" if pct is not None else "no_percent_zero_base",
    }


def cubic_meters_to_wan_liters(value: float | None) -> float | None:
    return None if value is None else round(value * 1000 / 10000, 4)


def wan_cubic_meters_to_wan_liters(value: float | None) -> float | None:
    return None if value is None else round(value * 1000, 4)


def yi_liters_to_wan_cubic_meters(value: float | None) -> float | None:
    return None if value is None else round(value * 10, 4)


def latest_record(history: dict, indicator: str) -> dict | None:
    rows = [r for r in history.get("records", []) if r.get("indicator") == indicator and r.get("status") == "ok"]
    rows.sort(key=lambda r: (r.get("data_date") or r.get("reference_period") or "", r.get("fetched_at") or ""), reverse=True)
    return rows[0] if rows else None


def record_key(record: dict) -> tuple:
    if record["indicator"] == "brazil_sugar_premium":
        return (
            record["indicator"],
            record.get("product"),
            record.get("port"),
            record.get("futures_contract"),
            record.get("data_date"),
            record.get("source_name"),
        )
    if record["indicator"] == "brazil_sugar_stock":
        return (record["indicator"], record.get("product"), record.get("reference_period"), record.get("source_name"))
    return (record["indicator"], record.get("ethanol_type"), record.get("reference_period"), record.get("source_name"))


def upsert_records(history: dict, records: list[dict]) -> dict:
    existing = {record_key(r): r for r in history.get("records", [])}
    for record in records:
        existing[record_key(record)] = record
    history["records"] = sorted(
        existing.values(),
        key=lambda r: (r.get("indicator", ""), r.get("data_date") or r.get("reference_period") or "", r.get("source_name") or ""),
    )
    history["lastUpdatedAt"] = beijing_now().isoformat(timespec="seconds")
    return history


def discover_premium(target_date: str) -> tuple[dict | None, list[dict]]:
    logs = []
    for query in PREMIUM_QUERIES:
        url = google_news_rss(f"{query} {target_date}")
        log = {
            "source": "Google News RSS premium discovery",
            "query": query,
            "url": url,
            "requestedAt": beijing_now().isoformat(timespec="seconds"),
        }
        try:
            body, status = fetch_url(url)
            log["httpStatus"] = status
            log["candidateCount"] = len(re.findall(r"<item>", body))
            log["parsed"] = False
            log["reason"] = (
                "Brazil VHP premium requires verifiable source-page product, port, "
                "FOB basis, ICE No.11 contract and cents/lb unit before publication."
            )
        except Exception as exc:
            log["error"] = str(exc)
        logs.append(log)
    return None, logs


def inspect_anp_sugar_stock() -> tuple[dict | None, list[dict]]:
    logs = []
    for url in (ANP_OPEN_DATA_URL, ANP_DYNAMIC_PANELS_URL):
        log = {
            "source": "ANP official site sugar-stock inspection",
            "url": url,
            "requestedAt": beijing_now().isoformat(timespec="seconds"),
        }
        try:
            body, status = fetch_url(url)
            log["httpStatus"] = status
            lower = body.lower()
            log["containsSugarTerms"] = len(re.findall(r"acucar|açúcar|sugar", lower))
            log["containsStockTerms"] = len(re.findall(r"estoque|stock", lower))
            log["parsed"] = False
            log["reason"] = (
                "ANP page did not expose a verifiable food-sugar stock dataset; "
                "ethanol, syrup, cane, production and sales data must not be relabeled as sugar stock."
            )
        except Exception as exc:
            log["error"] = str(exc)
        logs.append(log)
    for query in ANP_SUGAR_STOCK_QUERIES:
        url = google_news_rss(query)
        log = {
            "source": "ANP sugar-stock search",
            "query": query,
            "url": url,
            "requestedAt": beijing_now().isoformat(timespec="seconds"),
        }
        try:
            body, status = fetch_url(url)
            log["httpStatus"] = status
            log["candidateCount"] = len(re.findall(r"<item>", body))
            log["parsed"] = False
            log["reason"] = "No candidate is published as sugar stock without ANP field and dataset confirmation."
        except Exception as exc:
            log["error"] = str(exc)
        logs.append(log)
    return None, logs


def inspect_anp_ethanol_stock() -> tuple[dict | None, list[dict]]:
    logs = []
    for url in (ANP_DYNAMIC_PANELS_URL, ANP_ETHANOL_PRODUCTION_CSV_VIEW):
        log = {
            "source": "ANP ethanol-stock inspection",
            "url": url,
            "requestedAt": beijing_now().isoformat(timespec="seconds"),
        }
        try:
            body, status = fetch_url(url)
            log["httpStatus"] = status
            lower = body.lower()
            log["containsEthanol"] = "etanol" in lower
            log["containsStock"] = "estoque" in lower or "stock" in lower
            log["containsTankage"] = "tancagem" in lower
            log["containsProduction"] = "produção" in lower or "producao" in lower
            log["parsed"] = False
            log["reason"] = (
                "ANP source was checked for hydrous/anhydrous stock fields; "
                "production, sales, tankage and shipment values are not published as stock."
            )
        except Exception as exc:
            log["error"] = str(exc)
        logs.append(log)
    for query in ANP_ETHANOL_STOCK_QUERIES:
        url = google_news_rss(query)
        log = {
            "source": "ANP ethanol-stock search",
            "query": query,
            "url": url,
            "requestedAt": beijing_now().isoformat(timespec="seconds"),
        }
        try:
            body, status = fetch_url(url)
            log["httpStatus"] = status
            log["candidateCount"] = len(re.findall(r"<item>", body))
            log["parsed"] = False
            log["reason"] = "Candidate requires ANP dataset and dictionary verification before stock values are published."
        except Exception as exc:
            log["error"] = str(exc)
        logs.append(log)
    return None, logs


def pending(indicator: str, message: str, logs: list[dict]) -> dict:
    return {
        "indicator": indicator,
        "status": "pending",
        "statusText": message,
        "fetched_at": beijing_now().isoformat(timespec="seconds"),
        "fetchLog": logs,
    }


def build_snapshot(history: dict, target_date: str, logs: list[dict]) -> dict:
    premium = latest_record(history, "brazil_sugar_premium")
    sugar_stock = latest_record(history, "brazil_sugar_stock")
    ethanol_stock = latest_record(history, "brazil_ethanol_stock")
    return {
        "targetDate": target_date,
        "updatedAt": beijing_now().isoformat(timespec="seconds"),
        "sugarPremium": premium
        or pending(
            "brazil_sugar_premium",
            "未检索到公开可核验的巴西VHP原糖FOB升贴水数据。",
            [l for l in logs if "premium" in l.get("source", "").lower()],
        ),
        "sugarStock": sugar_stock
        or pending(
            "brazil_sugar_stock",
            "ANP暂未检索到可核实的食糖库存数据。",
            [l for l in logs if "sugar-stock" in l.get("source", "")],
        ),
        "ethanolStock": ethanol_stock
        or pending(
            "brazil_ethanol_stock",
            "ANP暂未解析到字段确认的含水/无水乙醇库存数值。",
            [l for l in logs if "ethanol-stock" in l.get("source", "")],
        ),
        "fetchLog": logs,
    }


def collect(target_date: str) -> dict:
    history = load_history()
    logs: list[dict] = []
    records: list[dict] = []

    premium, premium_logs = discover_premium(target_date)
    logs.extend(premium_logs)
    if premium:
        records.append(premium)

    sugar_stock, sugar_logs = inspect_anp_sugar_stock()
    logs.extend(sugar_logs)
    if sugar_stock:
        records.append(sugar_stock)

    ethanol_stock, ethanol_logs = inspect_anp_ethanol_stock()
    logs.extend(ethanol_logs)
    if ethanol_stock:
        records.append(ethanol_stock)

    if records:
        history = upsert_records(history, records)
    HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(HISTORY_PATH, history)
    snapshot = build_snapshot(history, target_date, logs)
    atomic_write_json(METRICS_ROOT / "latest.json", snapshot)
    return snapshot


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch Brazil sugar premium and stock metrics.")
    parser.add_argument("--date", required=True)
    args = parser.parse_args()
    print(json.dumps(collect(args.date), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
