from __future__ import annotations

import argparse
import hashlib
import html
import io
import json
import os
import re
import shutil
import subprocess
import sys
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
MAPA_ETHANOL_URL = "https://www.gov.br/agricultura/pt-br/assuntos/sustentabilidade/agroenergia/acompanhamento-da-producao-sucroalcooleira"
HISUGAR_IMPORT_COST_LIST_URL = "https://www.hisugar.com/home/newListMore?parentId=39&level=3&childId=144&menuTap1"
HISUGAR_SEARCH_URL = "https://www.hisugar.com/home/searchCatetoryAndArticle?keyWord=" + quote_plus("食糖进口成本及利润估算")
HISUGAR_BASE_URL = "https://www.hisugar.com"

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


def fetch_json(url: str, timeout: int = 15) -> tuple[dict, int]:
    req = Request(url, headers={"User-Agent": "Mozilla/5.0 SugarNewsBot/1.0", "Accept": "application/json"})
    with urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "ignore")), resp.status


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
    return (
        record["indicator"],
        record.get("ethanol_type"),
        record.get("stock_type"),
        record.get("reference_period"),
        record.get("source_name"),
    )


def upsert_records(history: dict, records: list[dict]) -> dict:
    existing = {record_key(r): r for r in history.get("records", [])}
    for record in records:
        old = existing.get(record_key(record))
        if old and old.get("file_hash") and old.get("file_hash") != record.get("file_hash"):
            revisions = old.get("revisions") or []
            archived = {k: old.get(k) for k in (
                "stock_total_tonnes", "stock_total_ten_thousand_tonnes", "file_hash",
                "stock_cubic_metres", "stock_ten_thousand_cubic_metres",
                "published_at", "report_updated_at", "fetched_at", "source_url", "report_url"
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


def article_date_from_title(title: str) -> str | None:
    match = re.search(r"(20\d{6})", title or "")
    if not match:
        return None
    raw = match.group(1)
    return f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}"


def hisugar_articles() -> tuple[list[dict], list[dict]]:
    logs = []
    articles = []
    for url, source in (
        (HISUGAR_IMPORT_COST_LIST_URL, "HiSugar import-cost list page"),
        (HISUGAR_SEARCH_URL, "HiSugar import-cost search API"),
    ):
        log = {"source": source, "url": url, "requestedAt": beijing_now().isoformat(timespec="seconds")}
        try:
            if url == HISUGAR_SEARCH_URL:
                payload, status = fetch_json(url, timeout=20)
                log["httpStatus"] = status
                for group in payload.get("data") or []:
                    for item in group.get("list") or []:
                        title = item.get("title") or ""
                        if "食糖进口成本及利润估算" not in title:
                            continue
                        article_id = str(item.get("id") or "")
                        link = item.get("url") or f"/home/articleContent?id={article_id}"
                        articles.append({
                            "article_id": article_id,
                            "article_title": title,
                            "article_published_at": item.get("publishTime") or item.get("createTime"),
                            "title_date": article_date_from_title(title),
                            "source_url": urljoin(HISUGAR_BASE_URL, link),
                        })
                log["candidateCount"] = len(articles)
                log["parsed"] = bool(articles)
            else:
                body, status = fetch_url(url, timeout=20)
                log["httpStatus"] = status
                log["containsColumnName"] = "食糖进口成本" in body
                log["parsed"] = True
        except Exception as exc:
            log["error"] = str(exc)
        logs.append(log)
    unique = {article["article_id"]: article for article in articles if article.get("article_id")}
    rows = sorted(unique.values(), key=lambda row: (row.get("title_date") or "", row.get("article_published_at") or ""), reverse=True)
    return rows, logs


def normalize_ocr_text(text: str) -> str:
    table = str.maketrans({
        "．": ".",
        "。": ".",
        "－": "-",
        "—": "-",
        "–": "-",
        "−": "-",
        "，": ",",
        "（": "(",
        "）": ")",
        "Ｌ": "L",
        "ｌ": "l",
    })
    return text.translate(table)


def run_tesseract_ocr(image_path: Path) -> str | None:
    exe = shutil.which("tesseract")
    if not exe:
        return None
    for lang in ("chi_sim+eng", "eng"):
        result = subprocess.run(
            [exe, str(image_path), "stdout", "-l", lang, "--psm", "6"],
            text=True,
            capture_output=True,
            timeout=45,
            encoding="utf-8",
            errors="ignore",
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout
    return None


def run_windows_ocr(image_path: Path) -> str | None:
    if os.name != "nt" or not shutil.which("powershell"):
        return None
    script = r'''
param([string]$Path)
Add-Type -AssemblyName System.Runtime.WindowsRuntime
[Windows.Storage.StorageFile, Windows.Storage, ContentType=WindowsRuntime] | Out-Null
[Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics.Imaging, ContentType=WindowsRuntime] | Out-Null
[Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType=WindowsRuntime] | Out-Null
[Windows.Globalization.Language, Windows.Globalization, ContentType=WindowsRuntime] | Out-Null
function AwaitOperation($operation, [type]$resultType) {
  $method = [System.WindowsRuntimeSystemExtensions].GetMethods() |
    Where-Object { $_.Name -eq 'AsTask' -and $_.IsGenericMethodDefinition -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' } |
    Select-Object -First 1
  $task = $method.MakeGenericMethod($resultType).Invoke($null, @($operation))
  $task.Wait()
  $task.Result
}
$file = AwaitOperation ([Windows.Storage.StorageFile]::GetFileFromPathAsync($Path)) ([Windows.Storage.StorageFile])
$stream = AwaitOperation ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
$decoder = AwaitOperation ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
$bitmap = AwaitOperation ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
$lang = [Windows.Globalization.Language]::new('zh-Hans-CN')
$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage($lang)
$result = AwaitOperation ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
$result.Text
'''
    with NamedTemporaryFile("w", encoding="utf-8", delete=False, suffix=".ps1") as tmp:
        tmp.write(script)
        script_path = Path(tmp.name)
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script_path), str(image_path)],
            text=True,
            capture_output=True,
            timeout=45,
            encoding="utf-8",
            errors="ignore",
        )
        return result.stdout if result.returncode == 0 and result.stdout.strip() else None
    finally:
        try:
            script_path.unlink()
        except OSError:
            pass


def ocr_image(image_bytes: bytes, suffix: str = ".png") -> tuple[str | None, str]:
    with NamedTemporaryFile("wb", delete=False, suffix=suffix) as tmp:
        tmp.write(image_bytes)
        image_path = Path(tmp.name)
    try:
        text = run_tesseract_ocr(image_path)
        if text:
            return text, "tesseract"
        text = run_windows_ocr(image_path)
        if text:
            return text, "windows_ocr"
        return None, "unavailable"
    finally:
        try:
            image_path.unlink()
        except OSError:
            pass


def premium_value_from_token(token: str) -> float | None:
    raw = re.sub(r"\s+", "", normalize_ocr_text(token))
    raw = raw.replace("L", "1").replace("l", "1")
    negative = raw.startswith("-") or raw.startswith(".")
    digits = re.findall(r"\d+", raw)
    if len(digits) >= 2:
        value = float(f"{int(digits[0])}.{digits[1]}")
    elif len(digits) == 1:
        value = float(digits[0])
    else:
        return None
    return -value if negative else value


def parse_hisugar_ocr_rows(text: str, article: dict, image_url: str, backend: str) -> list[dict]:
    normalized = normalize_ocr_text(text)
    dates = re.findall(r"20\d{6}", normalized)
    compact = re.sub(r"\s+", "", normalized)
    marker = compact.find("进口升贴水")
    if marker < 0:
        return []
    tail = compact[marker:]
    end_match = re.search(r"\(元/吨\)|元/吨|巴西配额内|配额内", tail)
    segment = tail[:end_match.start()] if end_match else tail[:260]
    tokens = re.findall(r"[-.]?\d[.,.]\d{1,3}", segment)
    values = [premium_value_from_token(token) for token in tokens]
    values = [value for value in values if value is not None and abs(value) < 20]
    if len(values) < len(dates):
        spaced_segment = normalized[normalized.find("进口升贴水"):]
        tokens = re.findall(r"[-.．]?\s*\d\s*[.．]\s*\d{1,3}", spaced_segment)
        values = [premium_value_from_token(token) for token in tokens]
        values = [value for value in values if value is not None and abs(value) < 20]
    rows = []
    for raw_date, value in zip(dates[-len(values):], values[-len(dates):]):
        data_date = f"{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:8]}"
        rows.append({
            "indicator": "brazil_sugar_premium",
            "status": "ok",
            "source_name": "广西糖网食糖进口成本及利润估算表",
            "dataset_name": "HiSugar 食糖进口成本及利润估算表",
            "product": "巴西糖进口升贴水",
            "port": "进口成本估算",
            "pricing_basis": "HiSugar import cost estimate",
            "futures_contract": None,
            "data_date": data_date,
            "import_premium_discount_cents_per_lb": value,
            "premium_discount_cents_per_lb": value,
            "unit": "美分/磅",
            "article_id": article.get("article_id"),
            "article_title": article.get("article_title"),
            "article_published_at": article.get("article_published_at"),
            "source_url": article.get("source_url"),
            "image_url": image_url,
            "fetched_at": beijing_now().isoformat(timespec="seconds"),
            "ocr_backend": backend,
        })
    return rows


def rows_from_hisugar_article(article: dict) -> tuple[list[dict], list[dict]]:
    logs = []
    rows = []
    log = {
        "source": "HiSugar import premium article",
        "articleId": article.get("article_id"),
        "url": article.get("source_url"),
        "requestedAt": beijing_now().isoformat(timespec="seconds"),
    }
    try:
        body, status = fetch_url(article["source_url"], timeout=20)
        log["httpStatus"] = status
        image_urls = [
            urljoin(article["source_url"], src)
            for src in re.findall(r'<img[^>]+src=["\']([^"\']+)["\']', body, re.I)
            if "/image/" in src
        ]
        log["imageCount"] = len(image_urls)
        for image_url in image_urls[:2]:
            image_bytes, image_status = fetch_bytes(image_url, timeout=20)
            text, backend = ocr_image(image_bytes, ".png")
            parsed = parse_hisugar_ocr_rows(text or "", article, image_url, backend) if text else []
            logs.append({
                "source": "HiSugar import premium image OCR",
                "articleId": article.get("article_id"),
                "imageUrl": image_url,
                "httpStatus": image_status,
                "ocrBackend": backend,
                "parsedRows": len(parsed),
                "parsedDates": [row["data_date"] for row in parsed],
                "parsed": bool(parsed),
            })
            rows.extend(parsed)
            if parsed:
                break
        log["parsed"] = bool(rows)
        log["parsedRows"] = len(rows)
    except Exception as exc:
        log["error"] = str(exc)
    logs.insert(0, log)
    return rows, logs


def comparable_yoy(rows: list[dict], latest_date: str) -> dict | None:
    target = datetime.fromisoformat(latest_date).date().replace(year=int(latest_date[:4]) - 1)
    candidates = []
    for row in rows:
        date = datetime.fromisoformat(row["data_date"]).date()
        if abs((date - target).days) <= 7:
            candidates.append((abs((date - target).days), date, row))
    if not candidates:
        return None
    candidates.sort(key=lambda item: (item[0], item[1]))
    return candidates[0][2]


def discover_premium(target_date: str) -> tuple[dict | None, list[dict]]:
    logs: list[dict] = []
    articles, article_logs = hisugar_articles()
    logs.extend(article_logs)
    if not articles:
        return None, logs

    parsed_rows: list[dict] = []
    recent_articles = articles[:12]
    for article in recent_articles:
        rows, row_logs = rows_from_hisugar_article(article)
        logs.extend(row_logs)
        parsed_rows.extend(rows)
        if parsed_rows and max(row["data_date"] for row in parsed_rows) >= (article.get("title_date") or ""):
            break
    if not parsed_rows:
        return None, logs

    latest = sorted(parsed_rows, key=lambda row: (row["data_date"], row.get("article_published_at") or ""))[-1]
    latest_date = latest["data_date"]
    title_dates = [article.get("title_date") for article in articles if article.get("title_date")]
    target_yoy = f"{int(latest_date[:4]) - 1}{latest_date[4:]}"
    yoy_seed = datetime.fromisoformat(target_yoy).date()
    yoy_articles = [
        article for article in articles
        if article.get("title_date")
        and abs((datetime.fromisoformat(article["title_date"]).date() - yoy_seed).days) <= 10
    ][:8]
    for article in yoy_articles:
        rows, row_logs = rows_from_hisugar_article(article)
        logs.extend(row_logs)
        parsed_rows.extend(rows)

    by_date: dict[str, dict] = {}
    for row in parsed_rows:
        old = by_date.get(row["data_date"])
        if not old or (row.get("article_published_at") or "") > (old.get("article_published_at") or ""):
            by_date[row["data_date"]] = row
    rows = sorted(by_date.values(), key=lambda row: row["data_date"])
    latest = rows[-1]
    previous = next((row for row in reversed(rows[:-1]) if row["data_date"] < latest["data_date"]), None)
    previous_year = comparable_yoy(rows, latest["data_date"])

    current = latest["premium_discount_cents_per_lb"]
    record = dict(latest)
    if previous:
        prev = previous["premium_discount_cents_per_lb"]
        record.update({
            "previous_data_date": previous["data_date"],
            "previous_value": prev,
            "daily_change": current - prev,
            "daily_change_percent": None if prev == 0 else (current - prev) / abs(prev) * 100,
        })
    if previous_year:
        prev_yoy = previous_year["premium_discount_cents_per_lb"]
        sign_crossed = (current > 0 > prev_yoy) or (current < 0 < prev_yoy)
        record.update({
            "previous_year_date": previous_year["data_date"],
            "previous_year_value": prev_yoy,
            "year_on_year_change": current - prev_yoy,
            "year_on_year_change_percent": None if prev_yoy == 0 or sign_crossed else (current - prev_yoy) / abs(prev_yoy) * 100,
            "yoy_status": "not_applicable" if prev_yoy == 0 or sign_crossed else "ok",
        })
    else:
        record.update(yoy_fields(current, None))
        logs.append({
            "source": "HiSugar import premium previous-year matcher",
            "targetDate": target_yoy,
            "availableTitleDates": title_dates[:30],
            "parsed": False,
            "reason": "Missing comparable HiSugar import-cost article rows near prior-year same month-day.",
        })
    return record, logs


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


def season_from_link_text(value: str) -> str | None:
    match = re.search(r"^\s*(20\d{2})\s*[-/]\s*(20\d{2})\s*$", value)
    if not match:
        return None
    return f"{int(match.group(1))}/{int(match.group(2))}"


def season_url_slug(season: str) -> str:
    return season.replace("/", "-")


def report_date_from_url(url: str) -> str | None:
    match = re.search(r"_(\d{2})(\d{2})(\d{2,4})(?:_\d+)?\.pdf$", url, re.I)
    if not match:
        return None
    day, month, year = match.groups()
    full_year = int(year) if len(year) == 4 else int("20" + year)
    date = datetime(full_year, int(month), int(day))
    if date.day == 15:
        date += timedelta(days=1)
    return date.strftime("%Y-%m-%d")


def report_date_from_context(title: str, url: str, body: str) -> tuple[str | None, str]:
    context = title
    escaped_url = re.escape(html.escape(url, quote=True))
    match = re.search(r".{0,260}" + escaped_url + r".{0,260}", body, re.I | re.S)
    if match:
        context = html.unescape(re.sub(r"<.*?>", " ", match.group(0)))
    date_match = re.search(r"Volumes\s+Acumulados\s+até\s*:?\s*(\d{2}/\d{2}/\d{4})", context, re.I)
    if date_match:
        return parse_pt_date(date_match.group(1)), "page_context_volumes_acumulados"
    return report_date_from_url(url), "file_name_period"


def mapa_ethanol_season_pages() -> tuple[dict[str, str], list[dict]]:
    logs = []
    pages: dict[str, str] = {}
    log = {
        "source": "MAPA ethanol season directory",
        "url": MAPA_ETHANOL_URL,
        "requestedAt": beijing_now().isoformat(timespec="seconds"),
    }
    try:
        body, status = fetch_url(MAPA_ETHANOL_URL, timeout=20)
        log["httpStatus"] = status
        for link in parse_page_links(body, MAPA_ETHANOL_URL):
            season = season_from_link_text(link["title"])
            if season and season_url_slug(season) in link["url"] and MAPA_ETHANOL_URL in link["url"]:
                pages[season] = link["url"]
        log["candidateCount"] = len(pages)
        log["parsed"] = bool(pages)
    except Exception as exc:
        log["error"] = str(exc)
    logs.append(log)
    return pages, logs


def mapa_ethanol_docs_from_season(season: str, season_url: str) -> tuple[list[dict], list[dict]]:
    logs = []
    docs = []
    log = {
        "source": "MAPA ethanol season page",
        "season": season,
        "url": season_url,
        "requestedAt": beijing_now().isoformat(timespec="seconds"),
    }
    try:
        body, status = fetch_url(season_url, timeout=20)
        log["httpStatus"] = status
        for link in parse_page_links(body, season_url):
            if not re.search(r"\.pdf$", link["url"], re.I):
                continue
            if "Acompanhamentodaprodu" not in link["url"]:
                continue
            reference_date, date_source = report_date_from_context(link["title"], link["url"], body)
            if not reference_date:
                continue
            docs.append({
                "title": link["title"] or "PDF",
                "url": link["url"],
                "season": season,
                "reference_date": reference_date,
                "date_source": date_source,
                "source_file_name": link["url"].rsplit("/", 1)[-1],
            })
        docs = sorted({doc["url"]: doc for doc in docs}.values(), key=lambda doc: doc["reference_date"])
        log["candidateCount"] = len(docs)
        log["parsedDates"] = [doc["reference_date"] for doc in docs]
        log["parsed"] = bool(docs)
    except Exception as exc:
        log["error"] = str(exc)
    logs.append(log)
    return docs, logs


def parse_report_updated_at(text: str) -> str | None:
    match = re.search(r"Data:\s*(\d{2}/\d{2}/\d{4})", text)
    if not match:
        return None
    return parse_pt_date(match.group(1))


def parse_mapa_pdf_number(value: str) -> int:
    return int(value.replace(".", ""))


def total_brazil_numbers(text: str) -> list[int]:
    match = re.search(r"TOTAL BRASIL.*?Acompanhamento da produ(?:ç|c)[ãa]o\s+(.{0,900}?)\s+Tot\.", text, re.I | re.S)
    if not match:
        match = re.search(r"TOTAL BRASIL(.{0,1100}?)Tot\.", text, re.I | re.S)
    if not match:
        return []
    raw_numbers = re.findall(r"\d{1,3}(?:\.\d{3})+", match.group(1))
    return [parse_mapa_pdf_number(item) for item in raw_numbers]


def hydrous_physical_stock_from_pdf(text: str) -> tuple[int | None, dict]:
    numbers = total_brazil_numbers(text)
    evidence = {
        "rawNumericColumns": numbers,
        "columnBasis": "TOTAL BRASIL row; product group HIDRATADO; ESTOQUE (m³); E.Físico.",
    }
    if len(numbers) < 7:
        return None, evidence
    # MAPA PDF extraction places the national hydrous E.Físico stock at the
    # seventh numeric value in the final TOTAL BRASIL summary block.
    return numbers[6], evidence


def fetch_ethanol_doc_row(doc: dict) -> tuple[dict | None, list[dict]]:
    logs = []
    row = None
    log = {
        "source": "MAPA hydrous ethanol stock PDF",
        "url": doc.get("url"),
        "season": doc.get("season"),
        "referenceDate": doc.get("reference_date"),
        "requestedAt": beijing_now().isoformat(timespec="seconds"),
    }
    try:
        pdf, status = fetch_bytes(doc["url"], timeout=30)
        file_hash = hashlib.sha256(pdf).hexdigest()
        text = pdf_text(pdf)
        stock, evidence = hydrous_physical_stock_from_pdf(text)
        log["httpStatus"] = status
        log["fileHash"] = file_hash
        log["numericColumnCount"] = len(evidence.get("rawNumericColumns") or [])
        log["parsed"] = stock is not None
        if stock is None:
            log["reason"] = "Could not locate TOTAL BRASIL hydrous ethanol E.Físico stock in MAPA PDF."
        else:
            row = {
                "season": doc["season"],
                "ethanol_type": "hydrous",
                "stock_type": "physical",
                "reference_period": doc["reference_date"],
                "reference_date": doc["reference_date"],
                "report_updated_at": parse_report_updated_at(text),
                "stock_cubic_metres": stock,
                "stock_ten_thousand_cubic_metres": stock / 10000,
                "hydrous_ethanol_stock": stock / 10000,
                "total_ethanol_stock": stock / 10000,
                "stock_unit": "万立方米",
                "source_page_url": doc.get("season_url"),
                "source_url": doc["url"],
                "report_url": doc["url"],
                "source_file_name": doc.get("source_file_name"),
                "reference_date_source": doc.get("date_source"),
                "file_hash": file_hash,
                "parse_evidence": evidence,
            }
            log["stockCubicMetres"] = stock
            log["stockTenThousandCubicMetres"] = stock / 10000
    except Exception as exc:
        log["error"] = str(exc)
    logs.append(log)
    return row, logs


def find_mapa_hydrous_ethanol_stock() -> tuple[dict | None, list[dict]]:
    logs: list[dict] = []
    pages, page_logs = mapa_ethanol_season_pages()
    logs.extend(page_logs)
    if not pages:
        return None, logs

    current_season = sorted(pages.keys(), reverse=True)[0]
    current_docs, current_logs = mapa_ethanol_docs_from_season(current_season, pages[current_season])
    logs.extend(current_logs)
    rows = []
    for doc in current_docs:
        doc["season_url"] = pages[current_season]
        row, row_logs = fetch_ethanol_doc_row(doc)
        logs.extend(row_logs)
        if row:
            rows.append(row)
    if not rows:
        return None, logs

    rows.sort(key=lambda row: row["reference_date"])
    latest = rows[-1]
    previous_period = next((row for row in reversed(rows[:-1]) if row["reference_date"] < latest["reference_date"]), None)

    prior_season = previous_season(latest["season"])
    previous_year = None
    if prior_season in pages:
        prior_docs, prior_logs = mapa_ethanol_docs_from_season(prior_season, pages[prior_season])
        logs.extend(prior_logs)
        target_yoy = f"{int(latest['reference_date'][:4]) - 1}{latest['reference_date'][4:]}"
        target_docs = [doc for doc in prior_docs if doc["reference_date"] == target_yoy]
        prior_rows = []
        for doc in target_docs:
            doc["season_url"] = pages[prior_season]
            row, row_logs = fetch_ethanol_doc_row(doc)
            logs.extend(row_logs)
            if row:
                prior_rows.append(row)
        previous_year = prior_rows[-1] if prior_rows else None
        if not previous_year:
            logs.append({
                "source": "MAPA hydrous ethanol previous-year matcher",
                "targetDate": target_yoy,
                "season": prior_season,
                "parsedDates": [doc["reference_date"] for doc in prior_docs],
                "parsed": False,
                "reason": "Missing exact same month-day comparable hydrous ethanol stock date.",
            })
    else:
        logs.append({
            "source": "MAPA hydrous ethanol previous-year season matcher",
            "targetSeason": prior_season,
            "availableSeasons": sorted(pages.keys(), reverse=True),
            "parsed": False,
            "reason": "Previous season directory was not found on MAPA ethanol page.",
        })

    current = latest["stock_ten_thousand_cubic_metres"]
    record = dict(latest)
    record.update({
        "indicator": "brazil_ethanol_stock",
        "status": "ok",
        "source_name": "巴西农业和畜牧业部（MAPA）",
        "dataset_name": "MAPA Acompanhamento da Produção Sucroalcooleira",
        "fetched_at": beijing_now().isoformat(timespec="seconds"),
        "original_unit": "m³",
    })
    if previous_period:
        previous_value = previous_period["stock_ten_thousand_cubic_metres"]
        record.update({
            "previous_period_date": previous_period["reference_date"],
            "previous_period_stock": previous_value,
            "half_month_change": current - previous_value,
            "half_month_change_percent": None if previous_value == 0 else (current - previous_value) / previous_value * 100,
        })
    if previous_year:
        previous_yoy = previous_year["stock_ten_thousand_cubic_metres"]
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
            "MAPA暂未解析到字段确认的巴西含水乙醇物理库存数值。",
            [l for l in logs if "ethanol" in l.get("source", "").lower()],
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

    ethanol_stock, ethanol_logs = find_mapa_hydrous_ethanol_stock()
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
