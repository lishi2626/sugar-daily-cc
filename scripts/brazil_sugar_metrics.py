from __future__ import annotations

import argparse
import hashlib
import html
import io
import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile
from urllib.parse import quote_plus, urljoin
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from PyPDF2 import PdfReader


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PUBLIC_ROOT = PROJECT_ROOT / "public" / "sugar-news"
METRICS_ROOT = PUBLIC_ROOT / "data" / "brazil_metrics"
HISTORY_PATH = METRICS_ROOT / "history.json"
try:
    SHANGHAI = ZoneInfo("Asia/Shanghai")
except Exception:
    SHANGHAI = timezone(timedelta(hours=8), name="Asia/Shanghai")

MAPA_PRODUCTION_URL = "https://www.gov.br/agricultura/pt-br/assuntos/sustentabilidade/agroenergia/producao"
MAPA_PREVIOUS_SEASONS_URL = "https://www.gov.br/agricultura/pt-br/assuntos/sustentabilidade/agroenergia/producao-e-estoques-de-acucar-por-tipo-safras-anteriores"
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


def fetch_bytes(url: str, timeout: int = 20) -> tuple[bytes, int]:
    req = Request(url, headers={"User-Agent": "Mozilla/5.0 SugarNewsBot/1.0"})
    with urlopen(req, timeout=timeout) as resp:
        return resp.read(), resp.status


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
        old = existing.get(record_key(record))
        if old and old.get("file_hash") and old.get("file_hash") != record.get("file_hash"):
            revisions = old.get("revisions") or []
            archived = {k: old.get(k) for k in (
                "stock_total_tonnes", "stock_total_ten_thousand_tonnes", "file_hash",
                "published_at", "fetched_at", "source_url"
            )}
            revisions.append(archived)
            record["revisions"] = revisions
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


def parse_page_links(body: str, base_url: str) -> list[dict]:
    links = []
    for href, text in re.findall(r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', body, re.I | re.S):
        label = html.unescape(re.sub(r"\s+", " ", re.sub("<.*?>", " ", text))).strip()
        links.append({"title": label, "url": urljoin(base_url, html.unescape(href))})
    return links


def parse_season(text: str) -> str | None:
    match = re.search(r"SAFRA\s*(\d{4})\s*[-/]\s*(\d{2,4})", text, re.I)
    if not match:
        match = re.search(r"SAFRA(\d{4})(\d{4})", text, re.I)
    if not match:
        return None
    first = int(match.group(1))
    second_raw = match.group(2)
    second = int(second_raw) if len(second_raw) == 4 else int(str(first)[:2] + second_raw)
    return f"{first}/{second}"


def previous_season(season: str) -> str:
    start, end = [int(part) for part in season.split("/")]
    return f"{start - 1}/{end - 1}"


def parse_pt_date(value: str) -> str:
    day, month, year = [int(part) for part in value.split("/")]
    return f"{year:04d}-{month:02d}-{day:02d}"


def date_to_pt(value: str) -> str:
    year, month, day = value.split("-")
    return f"{day}/{month}/{year}"


def published_from_url(url: str) -> str | None:
    match = re.search(r"_(\d{2})(\d{2})(\d{4})\.(?:pdf|xlsx|csv|ods)$", url, re.I)
    if not match:
        return None
    day, month, year = match.groups()
    return f"{year}-{month}-{day}"


def pdf_text(pdf_bytes: bytes) -> str:
    reader = PdfReader(io.BytesIO(pdf_bytes))
    return "\n".join((page.extract_text() or "") for page in reader.pages)


def parse_brazil_number(value: str) -> int:
    return int(value.replace(".", ""))


def stock_rows_from_pdf(text: str, season: str, doc: dict, file_hash: str) -> list[dict]:
    rows = []
    pattern = re.compile(r"BRASIL\s+(.{0,420}?)(?:Resumo|Acumulado)", re.I | re.S)
    for match in pattern.finditer(text):
        chunk = re.sub(r"\s+", " ", match.group(0))
        tail = text[match.end():match.end() + 180]
        date_match = re.search(r"Acumulado até:\s*(\d{2}/\d{2}/\d{4})", chunk + tail, re.I)
        if not date_match:
            continue
        numbers = re.findall(r"\d{1,3}(?:\.\d{3})+", chunk)
        if not numbers:
            continue
        total_tonnes = parse_brazil_number(numbers[-1])
        reference_date = parse_pt_date(date_match.group(1))
        type_values = {
            "raw_numeric_columns": [parse_brazil_number(item) for item in numbers],
            "note": "MAPA PDF text extraction preserves numeric columns; the final BRASIL numeric value is used as TOTAL stock.",
        }
        rows.append({
            "season": season,
            "reference_period": reference_date,
            "reference_date": reference_date,
            "stock_total_tonnes": total_tonnes,
            "stock_total_ten_thousand_tonnes": total_tonnes / 10000,
            "sugar_stock_value": total_tonnes / 10000,
            "stock_unit": "万吨",
            "stock_by_type_tonnes": type_values,
            "product": "食糖",
            "document_number": doc.get("document_number"),
            "document_title": doc.get("title"),
            "source_url": doc.get("url"),
            "published_at": doc.get("published_at"),
            "file_hash": file_hash,
        })
    return rows


def stock_docs_from_page(url: str, source: str) -> tuple[list[dict], list[dict]]:
    logs = []
    docs = []
    log = {"source": source, "url": url, "requestedAt": beijing_now().isoformat(timespec="seconds")}
    try:
        body, status = fetch_url(url, timeout=15)
        log["httpStatus"] = status
        for link in parse_page_links(body, url):
            title_norm = re.sub(r"\s+", " ", link["title"]).strip()
            haystack = f"{title_norm} {link['url']}"
            if "ESTOQUES" not in title_norm.upper() and "ESTOQUES" not in link["url"].upper():
                continue
            if "ACAR" not in link["url"].upper() and "AÇÚCAR" not in title_norm.upper() and "ACUCAR" not in title_norm.upper():
                continue
            season = parse_season(haystack)
            if not season:
                continue
            number = None
            number_match = re.search(r"/(\d{3}(?:\.\d+)?)", link["url"])
            if number_match:
                number = number_match.group(1)
            docs.append({
                "title": title_norm,
                "url": link["url"],
                "season": season,
                "document_number": number,
                "published_at": published_from_url(link["url"]),
            })
        log["candidateCount"] = len(docs)
        log["parsed"] = True
    except Exception as exc:
        log["error"] = str(exc)
    logs.append(log)
    return docs, logs


def fetch_stock_doc_rows(doc: dict) -> tuple[list[dict], list[dict]]:
    logs = []
    log = {
        "source": "MAPA sugar-stock PDF",
        "url": doc.get("url"),
        "season": doc.get("season"),
        "requestedAt": beijing_now().isoformat(timespec="seconds"),
    }
    try:
        pdf, status = fetch_bytes(doc["url"], timeout=25)
        file_hash = hashlib.sha256(pdf).hexdigest()
        text = pdf_text(pdf)
        rows = stock_rows_from_pdf(text, doc["season"], doc, file_hash)
        log["httpStatus"] = status
        log["fileHash"] = file_hash
        log["rowsParsed"] = len(rows)
        log["parsedDates"] = [row["reference_date"] for row in rows]
        log["parsed"] = bool(rows)
    except Exception as exc:
        log["error"] = str(exc)
        rows = []
    logs.append(log)
    return rows, logs


def find_mapa_sugar_stock() -> tuple[dict | None, list[dict]]:
    logs: list[dict] = []
    current_docs, doc_logs = stock_docs_from_page(MAPA_PRODUCTION_URL, "MAPA Agroenergia production page")
    logs.extend(doc_logs)
    all_current_rows = []
    for doc in current_docs:
        rows, row_logs = fetch_stock_doc_rows(doc)
        logs.extend(row_logs)
        all_current_rows.extend(rows)
    if not all_current_rows:
        return None, logs

    latest = sorted(all_current_rows, key=lambda row: row["reference_date"], reverse=True)[0]
    same_season = sorted(
        [row for row in all_current_rows if row["season"] == latest["season"] and row["reference_date"] < latest["reference_date"]],
        key=lambda row: row["reference_date"],
        reverse=True,
    )
    previous_period = same_season[0] if same_season else None

    hist_docs, hist_doc_logs = stock_docs_from_page(MAPA_PREVIOUS_SEASONS_URL, "MAPA previous sugar-stock seasons page")
    logs.extend(hist_doc_logs)
    prior_season = previous_season(latest["season"])
    prior_docs = [doc for doc in hist_docs if doc.get("season") == prior_season]
    prior_rows = []
    for doc in prior_docs:
        rows, row_logs = fetch_stock_doc_rows(doc)
        logs.extend(row_logs)
        prior_rows.extend(rows)
    target_yoy = f"{int(latest['reference_date'][:4]) - 1}{latest['reference_date'][4:]}"
    previous_year = next((row for row in prior_rows if row["reference_date"] == target_yoy), None)
    if not previous_year:
        logs.append({
            "source": "MAPA previous-year stock matcher",
            "targetDate": target_yoy,
            "season": prior_season,
            "parsedDates": [row["reference_date"] for row in prior_rows],
            "parsed": False,
            "reason": "Missing exact same month-day comparable stock date.",
        })

    current = latest["stock_total_ten_thousand_tonnes"]
    record = dict(latest)
    record.update({
        "indicator": "brazil_sugar_stock",
        "status": "ok",
        "source_name": "巴西农业和畜牧业部（MAPA）",
        "dataset_name": "MAPA Agroenergia - Estoques de Açúcar por Tipo",
        "fetched_at": beijing_now().isoformat(timespec="seconds"),
        "original_unit": "tonnes",
    })
    if previous_period:
        previous_value = previous_period["stock_total_ten_thousand_tonnes"]
        record.update({
            "previous_period_date": previous_period["reference_date"],
            "previous_period_stock": previous_value,
            "half_month_change": current - previous_value,
            "half_month_change_percent": None if previous_value == 0 else (current - previous_value) / previous_value * 100,
        })
    if previous_year:
        previous_yoy = previous_year["stock_total_ten_thousand_tonnes"]
        record.update({
            "previous_year_date": previous_year["reference_date"],
            "previous_year_stock": previous_yoy,
            "previous_year_value": previous_yoy,
            "year_on_year_change": current - previous_yoy,
            "year_on_year_change_percent": None if previous_yoy == 0 else (current - previous_yoy) / previous_yoy * 100,
            "yoy_status": "ok" if previous_yoy != 0 else "no_percent_zero_base",
        })
    else:
        record.update(yoy_fields(current, None))
    return record, logs


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
            "MAPA暂未解析到可核实的巴西食糖库存数据。",
            [l for l in logs if "MAPA" in l.get("source", "")],
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

    sugar_stock, sugar_logs = find_mapa_sugar_stock()
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
