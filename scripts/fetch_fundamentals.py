#!/usr/bin/env python3
from __future__ import annotations
"""
基本面公开数据自动抓取器。
按国家/指标模块抓取，经过白名单、日期、单位和时效校验后存入缓存。

抓取模块:
  - 天气: 巴西中南部/印度MH+UP/泰国东北/中国广西+云南 (Open-Meteo)
  - 宏观: NOAA ENSO状态
  - 其它指标(无稳定公开免费接口): 标记 needs_verification

规则:
  - 不绕过登录、验证码、付费墙或反爬限制
  - 不在白名单的来源不得写入缓存
  - 过期数据不得作为当前事实
  - 异常变动标记 needs_verification
  - 多源冲突标记 conflict

用法:
  py -m scripts.fetch_fundamentals
  py -m scripts.fetch_fundamentals --date 2026-06-13
"""

import argparse
import csv
import hashlib
import html
import io
import json
import logging
import re
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urljoin, urlencode
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

import yaml

# ── 路径 ────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
sys.path.insert(0, str(PROJECT_ROOT))

with open(PROJECT_ROOT / "config.yaml", "r", encoding="utf-8") as f:
    config = yaml.safe_load(f)

TARGET_CONTRACT = config["target_contract"]["code"]

logger = logging.getLogger("fetch_fundamentals")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

CACHE_PATH  = PROJECT_ROOT / "data" / "verified_fundamentals.json"
RAW_DIR     = PROJECT_ROOT / "data" / "raw"
USER_AGENT  = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

# data_type 枚举
DT_ACTUAL   = "actual"
DT_FORECAST = "forecast"
DT_POLICY   = "policy"
DT_WEATHER  = "weather"
DT_MARKET   = "market_expectation"

# status 枚举
ST_FRESH     = "fresh"
ST_UNCHANGED = "unchanged"
ST_CACHED    = "valid_cached"
ST_STALE     = "stale"
ST_CONFLICT  = "conflict"
ST_FAILED    = "failed"
ST_NEEDS_VER = "needs_verification"

# ── 泰国专用状态 ─────────────────────────────────────
ST_NOT_PUBLISHED      = "not_published"
ST_SOURCE_503         = "source_503"
ST_SOURCE_UNAVAILABLE = "source_unavailable"
ST_NO_NEW_DATA        = "no_new_data"

# ── 泰国 official_level 枚举 ──────────────────────────
OL_OCSB_ACTUAL       = "ocsb_actual"
OL_THAI_GOVERNMENT   = "thai_government_release"
OL_OCSB_POLICY       = "ocsb_policy_update"
OL_OFFICIAL_FORECAST = "official_forecast"
OL_NOT_PUBLISHED     = "not_published"

# ── 泰国 source_channel 枚举 ──────────────────────────
SC_OPEN_DATA         = "open_data"
SC_GOVERNMENT_PRD    = "government_prd"
SC_OCSB_MAIN         = "ocsb_main"
SC_OFFICIAL_FACEBOOK = "official_facebook"
SC_REGIONAL_CENTER   = "regional_center"
SC_CACHED            = "cached"

# ── 泰国阶段性指标 ───────────────────────────────────
THAI_STAGE_INDICATORS = {
    "sugarcane_crushed":    "甘蔗压榨量",
    "sugar_production":     "食糖产量",
    "fresh_cane_share":     "鲜蔗占比",
    "burnt_cane_share":     "燃烧甘蔗占比",
    "sugar_yield_per_cane": "每吨甘蔗产糖量",
    "number_of_mills":      "开榨糖厂数",
    "crushing_completed":   "压榨完成",
    "crushing_progress":    "压榨进度",
    "sugarcane_area":       "甘蔗种植面积",
    "sugarcane_yield":      "甘蔗单产",
    "exports":              "食糖出口",
    "policy_update":        "政策更新",
    "season_summary":       "榨季总结",
}

ALL_COUNTRIES = ["巴西", "印度", "泰国", "中国", "宏观"]


def beijing_now() -> datetime:
    return datetime.now(timezone(timedelta(hours=8)))


# ============================================================
# 缓存
# ============================================================

def load_cache() -> dict:
    if not CACHE_PATH.exists():
        return {"_schema_version": "2.0", "_last_updated": "", "records": []}
    with open(CACHE_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def save_cache(cache: dict):
    cache["_last_updated"] = beijing_now().strftime("%Y-%m-%d %H:%M:%S")
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)


def save_raw(target_date: str, source: str, content: str):
    # 用户要求每日数据只维护CSV，不再保存原始网页/PDF文本。
    return


def http_get_text(url: str, timeout: int = 20, encoding: str = "utf-8") -> str | None:
    try:
        req = Request(url, headers={"User-Agent": USER_AGENT})
        with urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
        return raw.decode(encoding, errors="replace")
    except Exception as e:
        logger.info("请求失败 %s: %s", url, e)
        return None


def http_get_bytes(url: str, timeout: int = 30) -> bytes | None:
    try:
        req = Request(url, headers={"User-Agent": USER_AGENT})
        with urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except Exception as e:
        logger.info("下载失败 %s: %s", url, e)
        return None


def safe_filename(value: str) -> str:
    h = hashlib.sha1(value.encode("utf-8")).hexdigest()[:16]
    tail = re.sub(r"[^A-Za-z0-9._-]+", "_", value.rsplit("/", 1)[-1])[:80]
    return f"{h}_{tail or 'download'}"


def clean_html(raw: str) -> str:
    c = re.sub(r"<script[^>]*>.*?</script>", "", raw, flags=re.I | re.S)
    c = re.sub(r"<style[^>]*>.*?</style>", "", c, flags=re.I | re.S)
    c = re.sub(r"<[^>]+>", " ", c)
    c = html.unescape(c)
    return re.sub(r"\s+", " ", c).strip()


def parse_date_anywhere(text: str, default_date: str) -> str:
    for pat in [
        r"(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})",
        r"(\d{1,2})[-/](\d{1,2})[-/](\d{4})",
    ]:
        m = re.search(pat, text)
        if not m:
            continue
        if len(m.group(1)) == 4:
            return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
        return f"{int(m.group(3)):04d}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"
    return default_date


# ============================================================
# 来源校验
# ============================================================

def is_source_whitelisted(country: str, url: str) -> bool:
    """检查域名是否在 config.yaml 的白名单中。"""
    whitelist = config.get("source_whitelist", {}).get(country, [])
    if not whitelist:
        return False
    for allowed in whitelist:
        if allowed in url:
            return True
    return False


# ============================================================
# 时效管理
# ============================================================

def get_freshness_days(data_type: str) -> int:
    mapping = {
        DT_WEATHER:  config["freshness"]["weather_days"],
        DT_ACTUAL:   config["freshness"]["unica_biweekly_days"],
        DT_FORECAST: config["freshness"]["production_data_days"],
        DT_POLICY:   config["freshness"]["policy_days"],
        DT_MARKET:   config["freshness"]["import_profit_days"],
    }
    return mapping.get(data_type, config["freshness"]["market_days"])


def update_record_status(rec: dict, now: datetime):
    """根据时效更新单条记录的状态。"""
    data_type = rec.get("data_type", DT_ACTUAL)
    max_age = get_freshness_days(data_type)
    data_date = rec.get("data_date", "")
    current_status = rec.get("status", "")

    try:
        dd = datetime.strptime(data_date, "%Y-%m-%d")
        age = (now.replace(tzinfo=None) - dd).days
    except (ValueError, TypeError):
        return

    if age > max_age:
        if current_status in (ST_FRESH, ST_UNCHANGED, ST_CACHED):
            rec["status"] = ST_STALE
    elif 0 < age <= max_age:
        if current_status == ST_FRESH:
            rec["status"] = ST_CACHED


# ============================================================
# 天气 (Open-Meteo, 免费无需 Key)
# ============================================================

def fetch_weather_region(lat: float, lon: float, name: str, country: str,
                         target_date: str) -> list[dict]:
    results = []
    url = (
        f"https://api.open-meteo.com/v1/forecast?"
        f"latitude={lat}&longitude={lon}"
        f"&daily=temperature_2m_max,temperature_2m_min,precipitation_sum"
        f"&timezone=Asia/Shanghai&forecast_days=3"
    )

    if not is_source_whitelisted(country, url):
        logger.warning("来源不在白名单: %s <- %s", country, url)
        return results

    try:
        req = Request(url, headers={"User-Agent": USER_AGENT})
        with urlopen(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        data = json.loads(raw)
    except Exception as e:
        logger.info("天气 %s: %s", name, e)
        return results

    save_raw(target_date, f"weather_{country}_{name.replace(' ', '_')[:20]}", raw)

    try:
        daily = data.get("daily", {})
        dates = daily.get("time", [])
        t_maxs = daily.get("temperature_2m_max", [])
        t_mins = daily.get("temperature_2m_min", [])
        precips = daily.get("precipitation_sum", [])

        for i, d in enumerate(dates):
            t_max = t_maxs[i] if i < len(t_maxs) else "N/A"
            t_min = t_mins[i] if i < len(t_mins) else "N/A"
            p = precips[i] if i < len(precips) else "N/A"

            results.append({
                "country": country,
                "indicator": f"天气-{name}",
                "value_or_fact": f"最高{t_max}C, 最低{t_min}C, 降水{p}mm",
                "unit": "C / mm",
                "data_date": d,
                "published_at": target_date,
                "fetched_at": beijing_now().strftime("%Y-%m-%d %H:%M:%S"),
                "source_name": "Open-Meteo",
                "source_url": url,
                "data_type": DT_WEATHER,
                "target_contract": TARGET_CONTRACT,
                "status": ST_FRESH,
                "notes": f"自动抓取{name}",
            })
    except Exception as e:
        logger.warning("天气解析 %s: %s", name, e)

    return results


def fetch_all_weather(target_date: str) -> list[dict]:
    all_results = []
    regions = config.get("fetcher", {}).get("weather", {}).get("regions", {})
    if not config.get("fetcher", {}).get("weather", {}).get("enabled", True):
        return all_results

    country_map = {
        "brazil_cs": "巴西", "india_mh": "印度", "india_up": "印度",
        "thailand_ne": "泰国", "china_gx": "中国", "china_yn": "中国",
    }

    for key, r in regions.items():
        country = country_map.get(key, "宏观")
        recs = fetch_weather_region(r["lat"], r["lon"], r["name"], country, target_date)
        all_results.extend(recs)
        logger.info("天气 %s: %d 条", r["name"], len(recs))

    return all_results


# ============================================================
# 宏观 — NOAA ENSO
# ============================================================

def fetch_noaa_enso(target_date: str) -> list[dict]:
    results = []
    url = config.get("fetcher", {}).get("macro", {}).get("enso_url", "")
    if not url:
        return results

    try:
        req = Request(url, headers={"User-Agent": USER_AGENT})
        with urlopen(req, timeout=20) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        data = json.loads(raw)
    except Exception as e:
        logger.info("NOAA ENSO: %s", e)
        return results

    save_raw(target_date, "NOAA_ENSO", raw[:100000])

    # NOAA ENSO JSON 结构可能不同版本有差异，尽力解析
    try:
        # 尝试提取 Nino 3.4 指数
        def _find_val(obj, key_hint, depth=0):
            if depth > 5:
                return None
            if isinstance(obj, dict):
                for k, v in obj.items():
                    if key_hint.lower() in k.lower():
                        return v
                    r = _find_val(v, key_hint, depth + 1)
                    if r is not None:
                        return r
            if isinstance(obj, list) and obj:
                return _find_val(obj[-1], key_hint, depth + 1)
            return None

        nino34 = _find_val(data, "nino")
        enso_phase = "Neutral"
        if isinstance(nino34, (int, float)):
            if nino34 >= 0.5:
                enso_phase = "El Nino"
            elif nino34 <= -0.5:
                enso_phase = "La Nina"

        results.append({
            "country": "宏观",
            "indicator": "ENSO状态",
            "value_or_fact": f"{enso_phase} (Nino3.4={nino34})" if nino34 is not None else enso_phase,
            "unit": "指数",
            "data_date": target_date,
            "published_at": target_date,
            "fetched_at": beijing_now().strftime("%Y-%m-%d %H:%M:%S"),
            "source_name": "NOAA CPC",
            "source_url": url,
            "data_type": DT_MARKET,
            "target_contract": TARGET_CONTRACT,
            "status": ST_FRESH,
            "notes": "ENSO状态自动获取",
        })
        logger.info("NOAA ENSO: %s", results[0]["value_or_fact"])
    except Exception as e:
        logger.warning("NOAA ENSO解析: %s", e)

    return results


# ============================================================
# 泛糖科技 — 印度/泰国国际资讯
# ============================================================

def _extract_hisugar_article_links(raw: str) -> list[tuple[str, str]]:
    """从泛糖资讯页抽取文章链接和邻近标题。"""
    out = []
    seen = set()
    for m in re.finditer(r'href=["\']([^"\']*?/home/articleContent\?id=(\d{8,24})[^"\']*)["\']', raw, re.I):
        href = html.unescape(m.group(1))
        aid = m.group(2)
        if aid in seen:
            continue
        seen.add(aid)
        context = raw[max(0, m.start() - 220): m.end() + 220]
        title = clean_html(context)
        out.append((urljoin(config["hisugar"]["base_url"], href), title[:120]))

    # 有些页面把链接写在 JS 字符串中，只能先拿 id 再访问正文。
    for aid in re.findall(r'/home/articleContent\?id=(\d{8,24})', raw):
        if aid in seen:
            continue
        seen.add(aid)
        out.append((f'{config["hisugar"]["base_url"]}/home/articleContent?id={aid}', ""))
    return out


def _hisugar_title(raw: str) -> str:
    m = re.search(r"<title[^>]*>(.*?)</title>", raw, re.I | re.S)
    if not m:
        return ""
    return clean_html(m.group(1)).replace("_广西泛糖科技有限公司官网", "").strip()


def fetch_hisugar_international_news(target_date: str) -> list[dict]:
    """
    印度、泰国基本面资讯只从泛糖科技国际新闻栏目获取。
    该栏目页面可能由前端渲染，若列表没有文章链接则不写入替代来源。
    """
    cfg = config.get("hisugar", {}).get("news", {})
    if not cfg.get("enabled", True):
        return []

    url = cfg.get("international_url", "")
    if not url or not is_source_whitelisted("印度", url) or not is_source_whitelisted("泰国", url):
        logger.warning("泛糖国际新闻URL未配置或不在白名单: %s", url)
        return []

    raw = http_get_text(url, timeout=20)
    if not raw:
        return []
    save_raw(target_date, "hisugar_international_list", clean_html(raw)[:12000])

    links = _extract_hisugar_article_links(raw)
    results = []
    latest_by_country: dict[str, dict] = {}
    keyword_map = {
        "印度": cfg.get("india_keywords", ["印度"]),
        "泰国": cfg.get("thailand_keywords", ["泰国"]),
    }

    for article_url, list_title in links[:40]:
        page = http_get_text(article_url, timeout=15)
        if not page:
            continue
        title = _hisugar_title(page) or list_title
        text = clean_html(page)
        haystack = f"{title} {text}"
        published_at = parse_date_anywhere(haystack[:800], target_date)

        for country, keywords in keyword_map.items():
            if country in latest_by_country:
                continue
            if not any(k and k in haystack for k in keywords):
                continue
            if not is_source_whitelisted(country, article_url):
                continue
            fact = text[:360]
            latest_by_country[country] = {
                "country": country,
                "indicator": f"泛糖国际资讯-{country}",
                "value_or_fact": f"{title}。{fact}",
                "unit": "文本",
                "data_date": published_at,
                "published_at": published_at,
                "fetched_at": beijing_now().strftime("%Y-%m-%d %H:%M:%S"),
                "source_name": "泛糖科技",
                "source_url": article_url,
                "data_type": DT_ACTUAL,
                "target_contract": TARGET_CONTRACT,
                "status": ST_FRESH,
                "notes": "泛糖科技国际新闻栏目自动抓取",
            }

    results.extend(latest_by_country.values())
    logger.info("泛糖国际资讯: %d 条", len(results))
    return results


# ============================================================
# 沐甜科技 — 印度/泰国国际资讯
# ============================================================

def _msweet_page_url(base_url: str, page_no: int) -> str:
    if "currentPage=" in base_url:
        return re.sub(r"currentPage=\d+", f"currentPage={page_no}", base_url)
    sep = "&" if "?" in base_url else "?"
    return f"{base_url}{sep}currentPage={page_no}"


def _extract_msweet_list_items(raw: str, list_url: str) -> list[dict]:
    items = []
    seen = set()
    block_pat = re.compile(
        r'<a[^>]+href=["\']([^"\']+)["\'][^>]*title=["\']([^"\']+)["\'][^>]*>.*?</a>'
        r'.{0,1200}?<p[^>]*class=["\']list-text["\'][^>]*>(.*?)</p>'
        r'.{0,500}?<span[^>]*class=["\']list-details["\'][^>]*>(.*?)</span>',
        re.I | re.S,
    )
    for m in block_pat.finditer(raw):
        href = html.unescape(m.group(1))
        if href.startswith("#") or "javascript:" in href.lower():
            continue
        title = html.unescape(clean_html(m.group(2)))
        summary = clean_html(m.group(3))
        published = clean_html(m.group(4))
        url = urljoin(list_url, href)
        if url in seen:
            continue
        seen.add(url)
        items.append({"url": url, "title": title, "summary": summary, "published_at": published[:10]})
    return items


def _extract_msweet_article_text(raw: str) -> str:
    """从沐甜文章详情页提取正文。优先匹配文章正文区域。"""
    # 尝试多种文章正文容器
    for pat in [
        r'<div[^>]+class=["\'][^"\']*(?:article|content|detail|TRS_Editor|nr|text)[^"\']*["\'][^>]*>(.*?)</div>',
        r'<div[^>]+id=["\'][^"\']*(?:article|content|detail|nr|text)[^"\']*["\'][^>]*>(.*?)</div>',
    ]:
        m = re.search(pat, raw, re.I | re.S)
        if m:
            text = clean_html(m.group(1))
            if len(text) > 100:
                return text

    # 兜底: 提取 "沐甜XX日讯" 之后的正文
    full = clean_html(raw)
    m = re.search(r"(沐甜\d+日讯\s*.+)", full)
    if m and len(m.group(1)) > 80:
        return m.group(1)

    # 再兜底: 提取 "您所在的位置" 之后、"上一篇|下一篇" 之前的内容
    m = re.search(r"您所在的位置\s*[:：]?\s*(.+?)(?:上一篇|下一篇|相关文章|责任编辑|$)", full, re.S)
    if m and len(m.group(1)) > 100:
        text = m.group(1)
        # 去掉开头的面包屑导航
        text = re.sub(r"^.*?>\s*", "", text[:200]) + text[200:]
        return text

    return full


def fetch_msweet_international_news(target_date: str) -> list[dict]:
    cfg = config.get("msweet", {})
    if not cfg.get("enabled", False):
        return []

    base_url = cfg.get("international_url", "")
    if not base_url or not is_source_whitelisted("印度", base_url) or not is_source_whitelisted("泰国", base_url):
        logger.warning("沐甜国际URL未配置或不在白名单: %s", base_url)
        return []

    scan_pages = int(cfg.get("scan_pages", 8))
    keyword_map = {
        "印度": cfg.get("india_keywords", ["印度"]),
        "泰国": cfg.get("thailand_keywords", ["泰国"]),
    }
    latest_by_country: dict[str, dict] = {}

    for page_no in range(1, scan_pages + 1):
        list_url = _msweet_page_url(base_url, page_no)
        raw = http_get_text(list_url, timeout=20)
        if not raw:
            continue
        save_raw(target_date, f"msweet_international_p{page_no}", clean_html(raw)[:12000])
        for item in _extract_msweet_list_items(raw, list_url):
            haystack = f"{item['title']} {item['summary']}"
            for country, keywords in keyword_map.items():
                if country in latest_by_country:
                    continue
                if country not in haystack:
                    continue
                if not any(k and k in haystack for k in keywords):
                    continue
                if not is_source_whitelisted(country, item["url"]):
                    continue
                pub = parse_date_anywhere(item.get("published_at", "") or item["summary"], target_date)
                latest_by_country[country] = {
                    "country": country,
                    "indicator": f"沐甜国际资讯-{country}",
                    "value_or_fact": f"{item['title']}。{item['summary']}",
                    "unit": "文本",
                    "data_date": pub,
                    "published_at": pub,
                    "fetched_at": beijing_now().strftime("%Y-%m-%d %H:%M:%S"),
                    "source_name": cfg.get("source_name", "沐甜科技"),
                    "source_url": item["url"],
                    "data_type": DT_ACTUAL,
                    "target_contract": TARGET_CONTRACT,
                    "status": ST_FRESH,
                    "notes": f"沐甜科技国际栏目第{page_no}页自动抓取",
                }
        if len(latest_by_country) == len(keyword_map):
            break

    results = list(latest_by_country.values())
    logger.info("沐甜国际资讯: %d 条", len(results))
    return results


# ============================================================
# UNICA — 巴西双周报告
# ============================================================

def _extract_pdf_text(pdf_bytes: bytes) -> str:
    try:
        from PyPDF2 import PdfReader
        reader = PdfReader(io.BytesIO(pdf_bytes))
        parts = []
        for page in reader.pages[:4]:
            parts.append(page.extract_text() or "")
        return "\n".join(parts)
    except Exception as e:
        logger.info("UNICA PDF文本解析失败: %s", e)
        return ""


def _find_unica_download_url(raw: str, page_url: str) -> str:
    candidates = []
    for m in re.finditer(r'href=["\']([^"\']+)["\']', raw, re.I):
        href = html.unescape(m.group(1))
        context = raw[max(0, m.start() - 120):m.end() + 120].lower()
        if any(k in (href + context).lower() for k in ["download", "pdf", "boletim", "document"]):
            candidates.append(urljoin(page_url, href))
    return candidates[0] if candidates else ""


def _parse_unica_report_date(text: str, target_date: str) -> str:
    months = {
        "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
        "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
    }
    m = re.search(r"until\s+([A-Za-z]+)\s+(\d{1,2}),\s*(\d{4})", text, re.I)
    if m:
        mon = months.get(m.group(1).lower())
        if mon:
            return f"{int(m.group(3)):04d}-{mon:02d}-{int(m.group(2)):02d}"
    return target_date


def _parse_unica_biweekly_period(text: str) -> dict:
    """
    Extract the biweekly period start and end dates from UNICA PDF text.
    Typical patterns:
      - "01/May to 15/May" or "May 1 to May 15"
      - "Period: 01/05 to 15/05"
      - "1st half of May" / "2nd half of May"
      - "until May 15, 2026"
    Returns dict with period_start, period_end, period_type.
    """
    months = {
        "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
        "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
        "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6,
        "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
    }
    result = {"period_start": "", "period_end": "", "period_type": "biweekly", "table_number": 2}

    # Pattern 1: "until Month DD, YYYY" — end of period
    m = re.search(r"until\s+([A-Za-z]+)\s+(\d{1,2}),\s*(\d{4})", text, re.I)
    if m:
        mon = months.get(m.group(1).lower())
        if mon:
            end = f"{int(m.group(3)):04d}-{mon:02d}-{int(m.group(2)):02d}"
            result["period_end"] = end
            # Assume 15-day biweekly period
            end_day = int(m.group(2))
            if end_day >= 28:
                # End of month → start is 16th
                result["period_start"] = f"{int(m.group(3)):04d}-{mon:02d}-16"
            elif end_day <= 15:
                # Mid month → start is 1st
                result["period_start"] = f"{int(m.group(3)):04d}-{mon:02d}-01"
            else:
                result["period_start"] = f"{int(m.group(3)):04d}-{mon:02d}-{max(1, end_day - 14):02d}"

    # Pattern 2: "DD/Mon to DD/Mon" or "Mon DD to Mon DD"
    if not result["period_start"]:
        m = re.search(r"(\d{1,2})\s*/\s*([A-Za-z]{3,})\s+to\s+(\d{1,2})\s*/\s*([A-Za-z]{3,})", text, re.I)
        if m:
            mon_start = months.get(m.group(2).lower())
            mon_end = months.get(m.group(4).lower())
            if mon_start and mon_end:
                year = datetime.now().year
                result["period_start"] = f"{year}-{mon_start:02d}-{int(m.group(1)):02d}"
                result["period_end"] = f"{year}-{mon_end:02d}-{int(m.group(3)):02d}"

    return result


def _parse_unica_table2_values(text: str) -> list[tuple[str, str, str]]:
    """
    解析 Table 2: BI-WEEKLY values 的 Sugar 与 Share %。
    同时提取上年同期数据用于计算同比百分点变化。

    PDF格式: Sugar行每组有 PreviousYear | Current | Var%
    验证: (1800-859)/859 = 109.5% ≈ 109.48% ✓
    """
    normalized = re.sub(r"\s+", " ", text)
    out = []

    # Sugar行: "Sugar ¹ 859 1,800 109.48% 558 1,237 121.75% 301 563 86.75%"
    # 格式: PrevYear_SC Current_SC Var%_SC PrevYear_SP Current_SP Var%_SP ...
    sugar_rows = re.findall(
        r"Sugar\s+¹?\s+([\d,]+)\s+([\d,]+)\s+([\d.]+%)",
        normalized, re.I
    )

    # 第二组是Table 2
    if len(sugar_rows) >= 2:
        row = sugar_rows[1]  # (859, 1800, 109.48%)
        prev_year = row[0].replace(",", "")  # 859 = 上年同期
        current = row[1].replace(",", "")    # 1800 = 本期双周
        var_pct = row[2]                      # 109.48% = 同比变化
        out.append(("UNICA Table2 Sugar", current, "千吨"))       # 1800
        out.append(("UNICA Table2 Sugar PrevYear", prev_year, "千吨"))  # 859
        out.append(("UNICA Table2 Sugar Var", var_pct, "%"))      # 109.48%

    # Share sugar行:
    # "sugar 45.23% 38.16% 50.51% 46.14% 39.03% 27.83%" (Table 1)
    # "sugar 45.69% 40.34% 51.41% 47.71% 37.89% 30.12%" (Table 2)
    # 格式: PrevYear_SC Current_SC Other_SC PrevYear_SP Current_SP Other_SP
    share_all = re.findall(
        r"sugar\s+([\d.]+%)\s+([\d.]+%)\s+([\d.]+%)\s+([\d.]+%)\s+([\d.]+%)\s+([\d.]+%)",
        normalized, re.I
    )
    if len(share_all) >= 2:
        table2 = share_all[1]  # Table 2: (45.69%, 40.34%, 51.41%, 47.71%, 37.89%, 30.12%)
        # PrevYear_SC=45.69%, Current_SC=40.34%
        out.append(("UNICA Table2 Share sugar", table2[1], "%"))  # 40.34% = 本期制糖比
        out.append(("UNICA Table2 Share sugar PrevYear", table2[0], "%"))  # 45.69% = 上年同期

    return out


def _load_unica_cached(download_url: str) -> tuple[str, bytes | None, str]:
    pdf = http_get_bytes(download_url, timeout=40)
    text = _extract_pdf_text(pdf) if pdf else ""
    return text, pdf, download_url


# ============================================================
# 全球供需机构观点 — 沐甜"国际—机构观点"栏目
# ============================================================

_INSTITUTION_NAMES = {
    "ISO": ["国际糖业组织", "ISO"],
    "USDA": ["美国农业部", "USDA"],
    "Czarnikow": ["Czarnikow", "嘉利高"],
    "StoneX": ["StoneX"],
    "Green Pool": ["Green Pool"],
    "Datagro": ["Datagro"],
    "Copersucar": ["Copersucar"],
    "Kingsman": ["Kingsman"],
    "S&P Global": ["S&P Global"],
    "花旗": ["花旗", "Citi"],
    "Itaú BBA": ["Itaú BBA", "Itau BBA"],
}

# 目标榨季年度 — 只使用此年度的供需观点
_TARGET_VIEW_SEASON = "2026/27"


def _normalize_season(text: str) -> str:
    """将 2026/27、2026-27、2026/2027、26/27 统一为 2026/27。"""
    m = re.search(r"(\d{4})[/\-](\d{2,4})\s*(?:年度|榨季|年|$)", text)
    if not m:
        # 尝试 26/27 形式
        m2 = re.search(r"(\d{2})[/\-](\d{2})\s*(?:年度|榨季|年|$)", text)
        if m2:
            y1 = int(m2.group(1))
            y2 = int(m2.group(2))
            if y1 < 100:
                y1 += 2000
            if y2 < 100:
                y2 += 2000
            return f"{y1}/{str(y2)[-2:]}"
        return ""
    y1 = int(m.group(1))
    y2 = m.group(2)
    if len(y2) == 2:
        return f"{y1}/{y2}"
    else:
        return f"{y1}/{int(y2)-2000:02d}"


def _extract_institution(text: str) -> str:
    """从文本中识别机构名称。"""
    for inst, names in _INSTITUTION_NAMES.items():
        for name in names:
            if name in text:
                return inst
    return ""


def _extract_surplus_deficit(text: str, target_season: str) -> list[dict]:
    """
    从文章正文中提取所有供需预估（可能包含多个榨季）。
    只提取目标年度的观点。
    返回 [{view_season, direction, value, unit}]
    """
    results = []

    # 先找目标年度的句子范围
    # 文章可能在一句话中提到多个年度，例如"从上一年度的过剩229万吨转为短缺55万吨"
    # 需要区分：目标年度的预估 vs 上一年度的回顾

    # 分句处理：按句号、分号、逗号分句
    sentences = re.split(r'[。；，]', text)

    for sentence in sentences:
        # 检查句子是否提到目标年度
        sentence_season = _normalize_season(sentence)

        # 如果句子没有明确年度，检查前后句
        if not sentence_season:
            # 检查前一句
            idx = text.find(sentence)
            if idx > 0:
                prev_ctx = text[max(0, idx - 150):idx]
                sentence_season = _normalize_season(prev_ctx)

        # 只处理目标年度的句子
        if sentence_season != target_season:
            continue

        # 在句子中查找过剩/短缺数值
        # 模式1: "过剩/短缺 XXX 万吨"
        for m in re.finditer(
            r"(过剩|短缺|供应过剩|供应短缺)\s*(?:约|预计|预估)?\s*([\d,.]+)\s*(?:万吨|百万吨|mt)",
            sentence, re.I
        ):
            direction_text = m.group(1)
            val = m.group(2).replace(",", "")
            direction = "surplus" if "过剩" in direction_text else "deficit"

            # 排除"从过剩转为短缺"中的旧年度数据
            context_before_match = sentence[:m.start()]
            if "从" in context_before_match and "转为" in sentence[m.end():]:
                continue

            results.append({
                "view_season": target_season,
                "direction": direction,
                "value": val,
                "unit": "万吨",
            })

        # 模式2: "XXX 万吨的过剩/短缺"（数字在前）
        for m in re.finditer(
            r"([\d,.]+)\s*(?:万吨|百万吨|mt)\s*(?:的)?\s*(?:温和|小幅|大幅)?\s*(过剩|短缺|供应过剩|供应短缺)",
            sentence, re.I
        ):
            val = m.group(1).replace(",", "")
            direction_text = m.group(2)
            direction = "surplus" if "过剩" in direction_text else "deficit"

            # 排除"从过剩转为短缺"中的旧年度数据
            context_before_match = sentence[:m.start()]
            if "从" in context_before_match and "转为" in sentence[m.end():]:
                continue

            results.append({
                "view_season": target_season,
                "direction": direction,
                "value": val,
                "unit": "万吨",
            })

        # 模式3: "转为 XXX 万吨的短缺"（ISO格式）
        for m in re.finditer(
            r"转为\s*([\d,.]+)\s*(?:万吨|百万吨)\s*(?:的)?\s*(过剩|短缺)",
            sentence, re.I
        ):
            val = m.group(1).replace(",", "")
            direction_text = m.group(2)
            direction = "surplus" if "过剩" in direction_text else "deficit"
            results.append({
                "view_season": target_season,
                "direction": direction,
                "value": val,
                "unit": "万吨",
            })

        # 查找"平衡"表述
        if re.search(r"(供需|整体)\s*(?:基本|处于)?\s*平衡", sentence):
            results.append({
                "view_season": target_season,
                "direction": "balanced",
                "value": "",
                "unit": "",
            })

    return results


def fetch_global_supply_demand(target_date: str) -> list[dict]:
    """
    从沐甜科技"国际—机构观点"栏目抓取全球供需平衡判断。
    只使用 2026/27 年度的机构观点。
    按发布日期选择最新一期，不按机构固定优先级。
    """
    url = "https://www.msweet.com.cn/mtkj/xwzx62/gj29/jggd22/index.html"
    if not is_source_whitelisted("中国", url):
        pass

    results = []
    logger.info("全球供需机构观点: %s", url)

    raw = http_get_text(url, timeout=20)
    if not raw:
        logger.warning("沐甜机构观点页面不可访问，使用CSV缓存")
        return _fallback_global_supply_demand_from_csv(target_date)

    articles = _extract_msweet_list_items(raw, url)
    logger.info("沐甜机构观点: 找到 %d 篇文章", len(articles))

    supply_kw = ["过剩", "短缺", "供需", "平衡", " surplus", " deficit",
                 "预估", "预测", "预计"]

    # 收集所有属于目标年度的机构观点
    target_views = []  # [{institution, published_at, view_season, direction, value, unit, title, url, summary}]

    for item in articles[:20]:
        title = item.get("title", "")
        summary = item.get("summary", "")
        list_haystack = f"{title} {summary}"
        pub = parse_date_anywhere(item.get("published_at", "") or summary, target_date)

        has_supply = any(kw in list_haystack for kw in supply_kw)
        if not has_supply:
            continue

        # 进入详情页获取完整正文
        article_url = item.get("url", "")
        detail_text = _fetch_article_detail(article_url, timeout=15)
        full_text = detail_text if detail_text else list_haystack

        # 识别机构
        institution = _extract_institution(full_text)
        if not institution:
            continue

        # 提取所有供需预估（可能包含多个榨季）
        estimations = _extract_surplus_deficit(full_text, _TARGET_VIEW_SEASON)

        # 去重：同机构同方向只保留一条
        seen_directions = set()
        deduped = []
        for est in estimations:
            key = f"{est['direction']}_{est['value']}"
            if key not in seen_directions:
                seen_directions.add(key)
                deduped.append(est)
        estimations = deduped

        # 从文章标题/摘要也尝试提取榨季
        title_season = _normalize_season(title) or _normalize_season(summary)

        for est in estimations:
            vs = est["view_season"]
            # 如果预估没有明确榨季，用标题榨季兜底
            if not vs:
                vs = title_season

            # 只保留目标年度
            if vs != _TARGET_VIEW_SEASON:
                continue

            target_views.append({
                "institution": institution,
                "published_at": pub,
                "view_season": vs,
                "direction": est["direction"],
                "value": est["value"],
                "unit": est["unit"],
                "title": title[:120],
                "url": article_url,
                "summary": summary[:300],
            })

        # 如果详情页没有提取到，但标题/摘要中有明确的目标年度供需信息
        if not estimations and title_season == _TARGET_VIEW_SEASON:
            # 从标题/摘要提取
            sd_m = re.search(
                r"(过剩|短缺)\s*(?:约|预计|预估)?\s*([\d,.]+)\s*(?:万吨|百万吨)",
                list_haystack, re.I
            )
            if sd_m:
                direction = "surplus" if "过剩" in sd_m.group(1) else "deficit"
                target_views.append({
                    "institution": institution,
                    "published_at": pub,
                    "view_season": _TARGET_VIEW_SEASON,
                    "direction": direction,
                    "value": sd_m.group(2).replace(",", ""),
                    "unit": "万吨",
                    "title": title[:120],
                    "url": article_url,
                    "summary": summary[:300],
                })
            elif any(kw in list_haystack for kw in ["平衡", "紧平衡"]):
                target_views.append({
                    "institution": institution,
                    "published_at": pub,
                    "view_season": _TARGET_VIEW_SEASON,
                    "direction": "balanced",
                    "value": "",
                    "unit": "",
                    "title": title[:120],
                    "url": article_url,
                    "summary": summary[:300],
                })

    logger.info("2026/27年度机构观点: %d 条", len(target_views))
    for v in target_views:
        logger.info("  %s | %s | %s%s%s | %s",
                    v["institution"], v["published_at"],
                    v["direction"], v["value"], v["unit"],
                    v["title"][:50])

    # 按发布日期降序排序
    target_views.sort(key=lambda v: v.get("published_at", ""), reverse=True)

    # 构建CSV记录 — 保存所有找到的2026/27年度观点
    for v in target_views:
        indicator_label = f"全球供需-{v['institution']}"
        impact_dir = ("bearish" if v["direction"] == "surplus" else
                      "bullish" if v["direction"] == "deficit" else "neutral")
        results.append({
            "country": "宏观",
            "region": "全球",
            "indicator": indicator_label,
            "value": f"{v['value']}{v['unit']}" if v["value"] else "",
            "value_or_fact": f"{v['institution']}: {v['title']}. {v['summary']}",
            "unit": v["unit"] or "文本",
            "data_date": v["published_at"] or target_date,
            "published_at": v["published_at"] or target_date,
            "fetched_at": beijing_now().strftime("%Y-%m-%d %H:%M:%S"),
            "source_name": "沐甜科技",
            "source_url": v["url"] or url,
            "source_channel": "institution_views",
            "data_type": DT_FORECAST,
            "target_contract": TARGET_CONTRACT,
            "season": v["view_season"],
            "status": ST_FRESH,
            "official_level": "institution_forecast",
            "institution": v["institution"],
            "view_season": v["view_season"],
            "view_direction": v["direction"],
            "surplus_deficit_value": v["value"],
            "impact_direction": impact_dir,
            "notes": f"{'主观点' if v == target_views[0] else '补充观点'} | {v['title'][:60]}",
        })

    # 如果没有找到目标年度观点，尝试CSV缓存
    if not results:
        logger.info("未找到 %s 年度机构观点，使用CSV缓存", _TARGET_VIEW_SEASON)
        return _fallback_global_supply_demand_from_csv(target_date)

    logger.info("全球供需机构观点: %d 条记录 (目标年度=%s)", len(results), _TARGET_VIEW_SEASON)
    return results


def _fallback_global_supply_demand_from_csv(target_date: str) -> list[dict]:
    """从CSV中查找最近一次有效的目标年度机构观点。"""
    try:
        from update_data_csv import read_all_rows
        rows = read_all_rows()
    except ImportError:
        return []

    candidates = []
    for r in rows:
        if r.get("country") != "宏观":
            continue
        if "全球供需" not in r.get("indicator", ""):
            continue
        vs = r.get("view_season", "") or r.get("season", "")
        if vs != _TARGET_VIEW_SEASON:
            continue
        st = r.get("status", "")
        if st not in ("valid", "fresh", "valid_cached"):
            continue
        candidates.append(r)

    if not candidates:
        return []

    # 按发布日期排序取最新
    candidates.sort(key=lambda r: r.get("published_at", ""), reverse=True)
    latest = candidates[0]

    result = dict(latest)
    result["status"] = ST_CACHED
    result["source_channel"] = "cached"
    result["notes"] = f"CSV缓存 | 目标年度={_TARGET_VIEW_SEASON} | 原日期={latest.get('published_at','')}"
    return [result]


def fetch_unica_biweekly(target_date: str) -> list[dict]:
    if not config.get("unica", {}).get("enabled", True):
        return []
    page_url = config["unica"]["url"]
    if not is_source_whitelisted("巴西", page_url):
        logger.warning("UNICA URL不在巴西白名单: %s", page_url)
        return []

    raw = http_get_text(page_url, timeout=25, encoding="utf-8")
    if not raw:
        return []
    save_raw(target_date, "unica_biweekly_page", clean_html(raw)[:12000])

    download_url = _find_unica_download_url(raw, page_url)
    text = clean_html(raw)
    source_url = download_url or page_url
    if download_url:
        text_from_pdf, _, cache_path = _load_unica_cached(download_url)
        if text_from_pdf:
            text = text_from_pdf
            save_raw(target_date, "unica_biweekly_pdf_text", text[:30000])
            logger.info("UNICA PDF文本来源: %s", cache_path)

    data_date = _parse_unica_report_date(text, target_date)
    period_info = _parse_unica_biweekly_period(text)
    parsed = _parse_unica_table2_values(text)
    results = []

    # Build notes with period info
    period_notes = f"UNICA Table 2 biweekly | period_type={period_info['period_type']}"
    if period_info["period_start"]:
        period_notes += f" | period_start={period_info['period_start']}"
    if period_info["period_end"]:
        period_notes += f" | period_end={period_info['period_end']}"
    period_notes += f" | table_number={period_info['table_number']}"

    # 提取本期和上年同期制糖比，计算同比百分点变化
    current_sugar_mix = None
    prev_year_sugar_mix = None
    current_sugar_val = None
    prev_year_sugar_val = None

    for indicator, value, unit in parsed:
        if indicator == "UNICA Table2 Share sugar":
            current_sugar_mix = value.replace("%", "")
        elif indicator == "UNICA Table2 Share sugar PrevYear":
            prev_year_sugar_mix = value.replace("%", "")
        elif indicator == "UNICA Table2 Sugar":
            current_sugar_val = value
        elif indicator == "UNICA Table2 Sugar PrevYear":
            prev_year_sugar_val = value

    # 计算制糖比同比百分点变化
    sugar_mix_yoy_pp = ""
    if current_sugar_mix and prev_year_sugar_mix:
        try:
            diff = float(current_sugar_mix) - float(prev_year_sugar_mix)
            sugar_mix_yoy_pp = f"{diff:+.2f}"
        except ValueError:
            pass

    # 生成归因结论
    attribution = ""
    if current_sugar_val and prev_year_sugar_val and sugar_mix_yoy_pp:
        try:
            cur_sugar = float(current_sugar_val)
            prev_sugar = float(prev_year_sugar_val)
            mix_change = float(sugar_mix_yoy_pp)
            sugar_change_pct = ((cur_sugar - prev_sugar) / prev_sugar * 100) if prev_sugar > 0 else 0

            if sugar_change_pct > 0 and mix_change < 0:
                attribution = f"甘蔗压榨量增加抵消了制糖比下降{sugar_mix_yoy_pp}个百分点的影响，食糖产量仍同比增加{sugar_change_pct:.2f}%"
            elif sugar_change_pct > 0 and mix_change > 0:
                attribution = f"甘蔗压榨量和制糖比均同比上升，食糖产量同比增加{sugar_change_pct:.2f}%"
            elif sugar_change_pct < 0 and mix_change < 0:
                attribution = f"甘蔗压榨量减少叠加制糖比下降，食糖产量同比减少{abs(sugar_change_pct):.2f}%"
            elif sugar_change_pct < 0 and mix_change > 0:
                attribution = f"制糖比上升未能完全抵消甘蔗压榨量减少的影响，食糖产量同比减少{abs(sugar_change_pct):.2f}%"
        except ValueError:
            pass

    for indicator, value, unit in parsed:
        rec = {
            "country": "巴西",
            "indicator": indicator,
            "value_or_fact": value,
            "unit": unit,
            "data_date": data_date,
            "published_at": target_date,
            "fetched_at": beijing_now().strftime("%Y-%m-%d %H:%M:%S"),
            "source_name": "UNICA",
            "source_url": source_url,
            "data_type": DT_ACTUAL,
            "target_contract": TARGET_CONTRACT,
            "season": "2026/2027",
            "status": ST_FRESH,
            "period_start": period_info["period_start"],
            "period_end": period_info["period_end"],
            "period_type": period_info["period_type"],
            "table_number": period_info["table_number"],
            "notes": period_notes,
        }
        # 在Sugar记录中附加归因和制糖比同比信息
        if indicator == "UNICA Table2 Sugar":
            if sugar_mix_yoy_pp:
                rec["sugar_mix_yoy_change_pp"] = sugar_mix_yoy_pp
            if attribution:
                rec["attribution"] = attribution
        results.append(rec)

    if not results:
        results.append({
            "country": "巴西",
            "indicator": "UNICA Table2解析",
            "value_or_fact": "已发现UNICA官方双周报告入口，但PDF Table 2 的 Sugar 与 Share % 未自动解析，需人工核对。",
            "unit": "文本",
            "data_date": data_date,
            "published_at": target_date,
            "fetched_at": beijing_now().strftime("%Y-%m-%d %H:%M:%S"),
            "source_name": "UNICA",
            "source_url": source_url,
            "data_type": DT_ACTUAL,
            "target_contract": TARGET_CONTRACT,
            "season": "2026/2027",
            "status": ST_NEEDS_VER,
            "notes": "仅使用UNICA官方来源；未用第三方或旧缓存替代。",
        })

    logger.info("UNICA双周报告: %d 条, source=%s", len(results), source_url)
    return results


# ============================================================
# 泰国 SugarZone 生产报告（最高优先级）
# ============================================================

import unicodedata

# Thai month name → number
_THAI_MONTHS = {
    "มกราคม": 1, "กุมภาพันธ์": 2, "มีนาคม": 3, "เมษายน": 4,
    "พฤษภาคม": 5, "มิถุนายน": 6, "กรกฎาคม": 7, "สิงหาคม": 8,
    "กันยายน": 9, "ตุลาคม": 10, "พฤศจิกายน": 11, "ธันวาคม": 12,
}


def normalize_thai_text(text: str) -> str:
    """Normalize Thai text: NFKC + strip all whitespace for fuzzy matching."""
    text = unicodedata.normalize("NFKC", text)
    text = re.sub(r"\s+", "", text)
    return text


def parse_thai_buddhist_date(date_str: str) -> str | None:
    """
    Parse Thai Buddhist date like '5 พฤษภาคม 2569' → '2026-05-05'.
    Returns ISO date string or None.
    """
    date_str = date_str.strip()
    m = re.match(r"(\d{1,2})\s+(\S+)\s+(\d{4})", date_str)
    if not m:
        return None
    day = int(m.group(1))
    month_name = m.group(2)
    buddhist_year = int(m.group(3))

    month = _THAI_MONTHS.get(month_name)
    if not month:
        # Try partial match
        for name, num in _THAI_MONTHS.items():
            if name in month_name or month_name in name:
                month = num
                break
    if not month:
        return None

    gregorian_year = buddhist_year - 543
    try:
        return f"{gregorian_year:04d}-{month:02d}-{day:02d}"
    except ValueError:
        return None


def _extract_sugarzone_report_links(raw: str) -> list[tuple[str, str]]:
    """
    Extract report links from SugarZone production-report-by-region page.
    Returns list of (date_str, pdf_url) tuples.
    """
    reports = []
    # Pattern: look for Thai date strings followed by links
    # The page lists reports with dates and PDF download links
    # Try multiple patterns

    # Pattern 1: date text near PDF links
    # Look for Thai date patterns like "5 พฤษภาคม 2569"
    date_pattern = re.compile(r"(\d{1,2}\s+(?:มกราคม|กุมภาพันธ์|มีนาคม|เมษายน|พฤษภาคม|มิถุนายน|กรกฎาคม|สิงหาคม|กันยายน|ตุลาคม|พฤศจิกายน|ธันวาคม)\s+\d{4})")

    # Find all dates and their positions
    dates_found = []
    for m in date_pattern.finditer(raw):
        date_str = m.group(1)
        iso_date = parse_thai_buddhist_date(date_str)
        if iso_date:
            dates_found.append((m.start(), date_str, iso_date))

    # Find all PDF links
    pdf_links = []
    for m in re.finditer(r'href=["\']([^"\']*\.pdf[^"\']*)["\']', raw, re.I):
        pdf_links.append((m.start(), html.unescape(m.group(1))))

    # Also look for links that contain "production-report" or similar patterns
    for m in re.finditer(r'href=["\']([^"\']*(?:report|production|sugar)[^"\']*)["\']', raw, re.I):
        href = html.unescape(m.group(1))
        if href not in [p[1] for p in pdf_links] and "javascript" not in href.lower():
            pdf_links.append((m.start(), href))

    # Match dates to nearest PDF link
    for date_pos, date_str, iso_date in dates_found:
        # Find the closest PDF link after this date
        best_link = None
        best_dist = 999999
        for link_pos, link_url in pdf_links:
            dist = abs(link_pos - date_pos)
            if dist < best_dist and link_pos >= date_pos - 500:
                best_dist = dist
                best_link = link_url
        if best_link:
            # Make absolute URL
            if best_link.startswith("/"):
                best_link = "https://sugarzone.in.th" + best_link
            elif not best_link.startswith("http"):
                best_link = "https://sugarzone.in.th/" + best_link
            reports.append((date_str, iso_date, best_link))

    # If no date-link pairs found, try a simpler approach: look for any links with dates in URL
    if not reports:
        for m in re.finditer(r'href=["\']([^"\']*(\d{4})[-_](\d{2})[-_](\d{2})[^"\']*\.pdf)["\']', raw, re.I):
            href = html.unescape(m.group(1))
            year, month, day = int(m.group(2)), int(m.group(3)), int(m.group(4))
            if year > 2500:
                year -= 543
            iso_date = f"{year:04d}-{month:02d}-{day:02d}"
            if href.startswith("/"):
                href = "https://sugarzone.in.th" + href
            elif not href.startswith("http"):
                href = "https://sugarzone.in.th/" + href
            reports.append(("", iso_date, href))

    # Deduplicate by URL
    seen_urls = set()
    unique = []
    for date_str, iso_date, url in reports:
        if url not in seen_urls:
            seen_urls.add(url)
            unique.append((date_str, iso_date, url))

    # Sort by date descending
    unique.sort(key=lambda x: x[1], reverse=True)
    return unique


def _parse_sugarzone_pdf(pdf_bytes: bytes, target_season: str) -> list[dict]:
    """
    Parse SugarZone production report PDF.
    Extract national total row (รวมทั้งสิ้น) with sugarcane and sugar values.
    """
    results = []
    try:
        from PyPDF2 import PdfReader
        reader = PdfReader(io.BytesIO(pdf_bytes))
        all_text = ""
        for page in reader.pages:
            page_text = page.extract_text() or ""
            all_text += page_text + "\n"

        if not all_text.strip():
            logger.warning("SugarZone PDF: 无法提取文本")
            return results

        # Normalize for matching
        normalized = normalize_thai_text(all_text)
        marker = normalize_thai_text("รวมทั้งสิ้น")

        # Find the total row - fuzzy match
        # Since PDF text may have character spacing issues, try to find "รวม" near "สิ้น"
        # There may be multiple matches; we want the one with the largest numbers (actual totals)
        lines = all_text.split("\n")
        best_context = ""
        best_large_count = 0

        for i, line in enumerate(lines):
            norm_line = normalize_thai_text(line)
            if marker in norm_line or (
                "รวม" in norm_line and "สิ้น" in norm_line
            ):
                # Extract numbers from this line and nearby lines
                context = line
                for j in range(1, 4):
                    if i + j < len(lines):
                        context += " " + lines[i + j]

                numbers = re.findall(r"[\d,]+(?:\.\d+)?", context)
                large_nums = [float(n.replace(",", "")) for n in numbers
                             if float(n.replace(",", "")) > 1000000]

                logger.info("SugarZone PDF รวมทั้งสิ้น行[%d] 找到 %d 个数字, %d 个大数字: %s",
                           i, len(numbers), len(large_nums), numbers[:5])

                if len(large_nums) > best_large_count:
                    best_large_count = len(large_nums)
                    best_context = context

        if best_context:
            total_row_found = True
            numbers = re.findall(r"[\d,]+(?:\.\d+)?", best_context)
            large_nums = []
            for n in numbers:
                val = float(n.replace(",", ""))
                if val > 1000000:
                    large_nums.append(val)

            if len(large_nums) >= 2:
                # Sort descending - the two largest are sugar and cane
                large_nums.sort(reverse=True)

                # In the SugarZone report, the columns are:
                # Region | Sugarcane (tonnes) | Molasses | Total cane | Yield% | Sugar (100kg bags) | ...
                # The sugarcane total (~105M) and sugar total (~120M) are the two largest
                # Sugar in 100kg bags is typically the largest single number
                sugar_bags = large_nums[0]  # Largest = sugar in 100kg bags
                cane_tonnes = large_nums[1]  # Second largest = cane in tonnes

                # Validate: sugar/cane ratio should be 8-14%
                # sugar_bags in 100kg, cane_tonnes in tonnes
                # ratio% = (sugar_bags * 100) / (cane_tonnes * 1000) * 100
                ratio = (sugar_bags * 100) / (cane_tonnes * 1000) * 100 if cane_tonnes > 0 else 0
                if ratio < 5 or ratio > 20:
                    # Ratio unreasonable, try swapping
                    sugar_bags, cane_tonnes = cane_tonnes, sugar_bags
                    ratio = (sugar_bags * 100) / (cane_tonnes * 1000) if cane_tonnes > 0 else 0

                if 5 <= ratio <= 20:
                    # Convert: sugar_bags × 100kg = kg, then /1000 = tonnes, then /10000 = 万吨
                    sugar_tonnes = sugar_bags * 100 / 1000
                    sugar_10k = sugar_tonnes / 10000
                    cane_10k = cane_tonnes / 10000

                    logger.info("SugarZone解析: 甘蔗=%.2f万吨, 食糖=%.2f万吨 (原始: cane=%s, sugar=%s bags)",
                               cane_10k, sugar_10k, f"{cane_tonnes:,.0f}", f"{sugar_bags:,.0f}")

                    results.append({
                        "indicator": "sugarcane_crushed_accumulated",
                        "value": str(cane_10k),
                        "raw_value": f"{cane_tonnes:,.3f}",
                        "unit": "万吨",
                    })
                    results.append({
                        "indicator": "sugar_production_accumulated",
                        "value": str(sugar_10k),
                        "raw_value": f"{sugar_bags:,.3f}",
                        "unit": "万吨",
                    })
                    # Yield: kg sugar per tonne cane
                    if cane_tonnes > 0:
                        yield_per_tonne = (sugar_bags * 100) / cane_tonnes
                        results.append({
                            "indicator": "sugar_yield_per_cane",
                            "value": f"{yield_per_tonne:.2f}",
                            "raw_value": f"{yield_per_tonne:.2f}",
                            "unit": "kg/t",
                        })
                else:
                    logger.warning("SugarZone PDF: 产糖率异常 ratio=%.2f%%, 无法确定哪列是糖哪列是甘蔗", ratio)
            elif len(large_nums) == 1:
                logger.warning("SugarZone PDF: 只找到1个大数字 %s", large_nums[0])

        if not best_context:
            logger.warning("SugarZone PDF: 未找到 'รวมทั้งสิ้น' 全国总计行")

    except Exception as e:
        logger.warning("SugarZone PDF解析失败: %s", e)

    return results


def fetch_thailand_sugarzone_production(target_date: str) -> list[dict]:
    """
    Fetch Thailand production data from SugarZone official production reports.
    Priority 1 source for current season data.
    """
    cfg = config.get("sugarzone", {})
    if not cfg.get("enabled", True):
        return []

    page_url = cfg.get("production_report_url", "https://sugarzone.in.th/production-report-by-region/")
    target_season = cfg.get("target_season", "2025/26")

    if not is_source_whitelisted("泰国", page_url):
        logger.warning("SugarZone URL不在泰国白名单: %s", page_url)
        return []

    logger.info("=" * 50)
    logger.info("SugarZone泰国生产报告: %s", page_url)
    logger.info("=" * 50)

    # Step 1: Fetch the listing page
    raw = http_get_text(page_url, timeout=25, encoding="utf-8")
    if not raw:
        logger.warning("SugarZone页面不可访问")
        return []

    # Step 2: Extract report dates and PDF links
    reports = _extract_sugarzone_report_links(raw)
    logger.info("SugarZone找到 %d 份报告", len(reports))

    if not reports:
        logger.warning("SugarZone未找到任何报告链接")
        return []

    # Step 3: Select latest report
    latest_date_str, latest_iso, latest_pdf_url = reports[0]
    logger.info("SugarZone最新报告: 日期=%s (泰文: %s), PDF=%s",
               latest_iso, latest_date_str, latest_pdf_url)

    # Step 4: Validate report is for target season
    # Check if report date falls within 2025/26 season (Nov 2025 - Oct 2026)
    try:
        report_dt = datetime.strptime(latest_iso, "%Y-%m-%d")
        # Thai sugar season: Nov previous year to Oct current year
        # 2025/26 = Nov 2025 to Oct 2026
        season_year = report_dt.year if report_dt.month >= 11 else report_dt.year - 1
        report_season = f"{season_year}/{str(season_year + 1)[-2:]}"
        logger.info("SugarZone报告所属榨季: %s (报告日期: %s)", report_season, latest_iso)
        if report_season != target_season:
            logger.warning("SugarZone报告榨季 %s != 目标榨季 %s", report_season, target_season)
            # Still proceed but mark as needs verification
    except ValueError:
        logger.warning("SugarZone报告日期解析失败: %s", latest_iso)
        return []

    # Step 5: Download PDF
    pdf_bytes = http_get_bytes(latest_pdf_url, timeout=40)
    if not pdf_bytes:
        logger.warning("SugarZone PDF下载失败: %s", latest_pdf_url)
        return []

    logger.info("SugarZone PDF下载成功: %d bytes", len(pdf_bytes))

    # Step 6: Parse PDF
    parsed = _parse_sugarzone_pdf(pdf_bytes, target_season)

    if not parsed:
        logger.warning("SugarZone PDF解析未提取到数据")
        return []

    # Step 7: Build records
    # Note: No YoY data available → impact_direction=neutral_to_bearish, confidence=medium_low
    results = []
    # Build current season records
    current_data = {}
    for item in parsed:
        current_data[item["indicator"]] = item["value"]
        rec = _make_thai_record(
            indicator=item["indicator"],
            value=item["value"],
            unit=item["unit"],
            data_date=latest_iso,
            published_at=latest_iso,
            source_name="SugarZone",
            source_url=latest_pdf_url,
            channel="official_production_report",
            official_level="official_actual",
            season=target_season,
            status=ST_FRESH,
            data_type=DT_ACTUAL,
            text_value=f"{item['value']} {item['unit']}",
            notes=f"SugarZone生产报告 | 报告日期: {latest_iso} | 原始值: {item.get('raw_value', '')}",
            impact_direction="neutral_to_bearish",
            confidence="medium_low",
        )
        results.append(rec)

    # ── 上一榨季对比: 2025-04-09 ──
    comparison_date = "2025-04-09"
    logger.info("SugarZone尝试抓取上一榨季对比报告: %s", comparison_date)
    prev_report = None
    for date_str, iso_date, pdf_url in reports:
        if iso_date == comparison_date:
            prev_report = (date_str, iso_date, pdf_url)
            break

    if prev_report:
        _, prev_iso, prev_pdf_url = prev_report
        logger.info("SugarZone找到对比报告: %s -> %s", prev_iso, prev_pdf_url)
        prev_pdf = http_get_bytes(prev_pdf_url, timeout=40)
        if prev_pdf:
            prev_parsed = _parse_sugarzone_pdf(prev_pdf, "2024/25")
            if prev_parsed:
                prev_data = {}
                for item in prev_parsed:
                    prev_data[item["indicator"]] = item["value"]

                # Add comparison records
                for indicator in ["sugarcane_crushed_accumulated", "sugar_production_accumulated", "sugar_yield_per_cane"]:
                    cur_val = current_data.get(indicator)
                    prev_val = prev_data.get(indicator)
                    if cur_val and prev_val:
                        try:
                            diff = float(cur_val) - float(prev_val)
                            rec = _make_thai_record(
                                indicator=f"{indicator}_comparison",
                                value=f"{diff:+.2f}",
                                unit="万吨" if "accumulated" in indicator else "kg/t",
                                data_date=latest_iso,
                                published_at=latest_iso,
                                source_name="SugarZone",
                                source_url=latest_pdf_url,
                                channel="official_production_report",
                                official_level="official_actual",
                                season=target_season,
                                status=ST_FRESH,
                                data_type=DT_ACTUAL,
                                text_value=f"当前{cur_val} vs 上一榨季{prev_val}",
                                notes=f"对比: nearest_previous_season_report | 当前={cur_val}({latest_iso}) vs 上期={prev_val}({prev_iso})",
                                impact_direction="neutral_to_bearish",
                                confidence="medium_low",
                            )
                            # Add comparison metadata
                            rec["comparison_type"] = "nearest_previous_season_report"
                            rec["comparison_date"] = prev_iso
                            rec["previous_value"] = prev_val
                            rec["current_value"] = cur_val
                            results.append(rec)
                        except (ValueError, TypeError):
                            pass
                logger.info("SugarZone对比数据: 当前=%s, 上期=%s", latest_iso, prev_iso)
            else:
                logger.warning("SugarZone对比报告解析失败: %s", prev_iso)
    else:
        logger.info("SugarZone未找到 %s 对比报告，跳过", comparison_date)

    logger.info("SugarZone抓取完成: %d 条记录", len(results))
    return results


# ============================================================
# 泰国 OCSB / 印度 NFCSF / 中国广西沐甜
# ============================================================

THAI_INDICATOR_MAP = {
    "ปริมาณน้ำตาลทราย": "sugar_production",
    "ปริมาณอ้อย": "sugarcane_volume",
    "พื้นที่ปลูกอ้อย": "sugarcane_area",
    "สถิติพื้นที่ปลูกอ้อย": "sugarcane_area",
    "ผลผลิตอ้อยต่อพื้นที่": "sugarcane_yield",
    "สถิติผลผลิตอ้อยต่อพื้นที่": "sugarcane_yield",
    "ข้อมูลการพยากรณ์": "production_forecast",
    "ปริมาณการส่งออกน้ำตาลทราย": "sugar_exports",
}

OCSB_DATASET_SPECS = [
    {
        "query": "ปริมาณน้ำตาลทราย",
        "indicator": "sugar_production",
        "label": "当前榨季最新OCSB食糖产量",
        "value_columns": ["Sugartotal"],
        "unit": "吨",
        "data_type": DT_ACTUAL,
        "aggregate": "sum",
    },
    {
        "query": "ปริมาณอ้อย",
        "indicator": "sugarcane_volume",
        "label": "当前榨季最新OCSB甘蔗压榨量",
        "value_columns": ["Total"],
        "unit": "吨",
        "data_type": DT_ACTUAL,
        "aggregate": "sum",
    },
    {
        "query": "สถิติพื้นที่ปลูกอ้อย",
        "indicator": "sugarcane_area",
        "label": "OCSB甘蔗种植面积",
        "value_columns": ["CaneArea"],
        "unit": "莱",
        "data_type": DT_ACTUAL,
        "aggregate": "sum",
    },
    {
        "query": "สถิติผลผลิตอ้อยต่อพื้นที่",
        "indicator": "sugarcane_yield",
        "label": "OCSB甘蔗单产",
        "value_columns": ["Yield"],
        "weight_column": "CaneArea",
        "unit": "吨/莱",
        "data_type": DT_ACTUAL,
        "aggregate": "weighted_avg",
    },
    {
        "query": "ข้อมูลการพยากรณ์",
        "indicator": "production_forecast",
        "label": "下一榨季OCSB面积和产量预测",
        "value_columns": ["Predict_CaneArea", "Predict_Cane", "Predict_Yield"],
        "unit": "莱 / 吨 / 吨每莱",
        "data_type": DT_FORECAST,
        "aggregate": "multi_sum_avg",
    },
    {
        "query": "ปริมาณการส่งออกน้ำตาลทราย",
        "indicator": "sugar_exports",
        "label": "当前榨季最新OCSB食糖出口量",
        "value_columns": ["22", "20", "quantity", "Quantity"],
        "unit": "吨",
        "data_type": DT_ACTUAL,
        "aggregate": "sum",
    },
]

OCSB_ALLOWED_STATUSES = {
    ST_FRESH, ST_CACHED, "no_new_data", "source_503", "not_published", ST_NEEDS_VER,
}


def normalize_thai_season(season_text: str) -> str:
    """将 2568/2569、2568/69、2025/2026、2025/26 统一为 2025/26。"""
    text = str(season_text or "").strip()
    m = re.search(r"(\d{4})\s*/\s*(\d{2,4})", text)
    if not m:
        return ""
    start = int(m.group(1))
    end_raw = m.group(2)
    if start >= 2400:
        start_be = start
        start = start_be - 543
        if len(end_raw) == 2:
            end_be = int(str(start_be // 100) + end_raw)
            if end_be < start_be:
                end_be += 100
            end = end_be - 543
        else:
            end = int(end_raw) - 543
    else:
        if len(end_raw) == 2:
            end = int(str(start // 100) + end_raw)
            if end < start:
                end += 100
        else:
            end = int(end_raw)
    return f"{start}/{str(end)[-2:]}"


def thai_buddhist_to_season(thai_season: str) -> str:
    return normalize_thai_season(thai_season)


def thai_date_to_iso(date_text: str) -> str:
    m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{2,4})", str(date_text))
    if not m:
        return ""
    year = int(m.group(3))
    if year < 100:
        year += 2500
    year -= 543
    return f"{year:04d}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"


def _num(value) -> float | None:
    if value is None:
        return None
    text = str(value).strip().replace(",", "")
    if not text:
        return None
    m = re.search(r"-?\d+(?:\.\d+)?", text)
    if not m:
        return None
    try:
        return float(m.group(0))
    except ValueError:
        return None


def _fmt_num(value: float) -> str:
    if abs(value - round(value)) < 0.005:
        return str(int(round(value)))
    return f"{value:.2f}".rstrip("0").rstrip(".")


def _ocsb_get(url: str, timeout: int = 15, retries: int = 2) -> tuple[int | None, bytes]:
    last_status = None
    for attempt in range(retries):
        try:
            req = Request(url, headers={"User-Agent": USER_AGENT})
            with urlopen(req, timeout=timeout) as resp:
                return resp.status, resp.read()
        except HTTPError as e:
            last_status = e.code
            if e.code == 503:
                return e.code, b""
        except Exception as e:
            logger.info("OCSB请求失败 attempt=%d url=%s err=%s", attempt + 1, url, e)
        if attempt + 1 < retries:
            time.sleep(1)
    return last_status, b""


def _ocsb_head_status(url: str, timeout: int = 15, retries: int = 2) -> int | None:
    last_status = None
    for attempt in range(retries):
        try:
            req = Request(url, headers={"User-Agent": USER_AGENT}, method="HEAD")
            with urlopen(req, timeout=timeout) as resp:
                return resp.status
        except HTTPError as e:
            last_status = e.code
            if e.code == 503:
                return e.code
        except Exception as e:
            logger.info("OCSB HEAD请求失败 attempt=%d url=%s err=%s", attempt + 1, url, e)
        if attempt + 1 < retries:
            time.sleep(1)
    return last_status


def _decode_table_bytes(data: bytes) -> str:
    for enc in ("utf-8-sig", "utf-8", "cp874", "tis-620"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _read_resource_rows(url: str, fmt: str) -> list[dict]:
    status, data = _ocsb_get(url, timeout=20, retries=2)
    logger.info("OCSB资源下载: status=%s format=%s url=%s", status, fmt, url)
    if status != 200 or not data:
        return []
    fmt_l = (fmt or "").lower()
    if "csv" in fmt_l or url.lower().endswith(".csv"):
        text = _decode_table_bytes(data)
        sample = text[:2048]
        has_header = any(name in sample for name in [
            "ProductionYear", "Sugartotal", "Total", "CaneArea", "Predict_Cane"
        ])
        if has_header:
            return list(csv.DictReader(io.StringIO(text)))
        rows = []
        for row in csv.reader(io.StringIO(text)):
            rows.append({str(i): v for i, v in enumerate(row)})
        return rows
    if "xls" in fmt_l or url.lower().endswith((".xlsx", ".xls")):
        try:
            import openpyxl
            wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
            ws = wb.active
            rows_iter = ws.iter_rows(values_only=True)
            headers = [str(c or "").strip() for c in next(rows_iter)]
            return [{headers[i]: cell for i, cell in enumerate(row) if i < len(headers)}
                    for row in rows_iter]
        except Exception as e:
            logger.info("OCSB XLSX解析失败: %s", e)
            return []
    return []


def _ckan_search(query: str, cfg: dict) -> tuple[int | None, list[dict]]:
    api = cfg.get("ckan_api_url", "https://opendata.ocsb.go.th/api/3/action/package_search")
    url = api + "?" + urlencode({"q": query, "rows": 5})
    status, data = _ocsb_get(url, timeout=20, retries=2)
    logger.info("OCSB Open Data状态码: %s | query=%s", status, query)
    if status != 200 or not data:
        return status, []
    try:
        payload = json.loads(data.decode("utf-8"))
    except Exception as e:
        logger.info("OCSB CKAN JSON解析失败: %s", e)
        return status, []
    return status, payload.get("result", {}).get("results", []) if payload.get("success") else []


def _resource_candidates(package: dict) -> list[dict]:
    resources = package.get("resources", []) or []
    data_resources = []
    dict_resources = []
    for res in resources:
        fmt = (res.get("format") or "").lower()
        name = (res.get("name") or "").lower()
        if "dictionary" in name or "datadic" in name:
            dict_resources.append(res)
        elif "csv" in fmt:
            data_resources.append(res)
        elif "xls" in fmt:
            data_resources.append(res)
    data_resources.sort(key=lambda r: 0 if "csv" in (r.get("format") or "").lower() else 1)
    return data_resources or dict_resources


def _aggregate_ocsb_rows(rows: list[dict], spec: dict, target_season: str) -> tuple[str, str, str]:
    by_season: dict[str, list[dict]] = {}
    buddhist_seen = []
    for row in rows:
        raw_season = row.get("ProductionYear") or row.get("productionyear") or row.get("8") or ""
        norm = normalize_thai_season(str(raw_season))
        if norm:
            by_season.setdefault(norm, []).append(row)
            buddhist_seen.append(str(raw_season))
    if buddhist_seen:
        logger.info("识别到的佛历年份: %s", ", ".join(sorted(set(buddhist_seen))[-5:]))
        logger.info("转换后的公历榨季: %s", ", ".join(sorted(by_season.keys())[-5:]))
    if target_season not in by_season:
        latest = sorted(by_season.keys())[-1] if by_season else ""
        return "", latest, "not_published"

    target_rows = by_season[target_season]
    aggregate = spec.get("aggregate")
    if aggregate == "weighted_avg":
        total_weight = total_value = 0.0
        for row in target_rows:
            val = _num(row.get(spec["value_columns"][0]))
            weight = _num(row.get(spec.get("weight_column", ""))) or 1.0
            if val is None:
                continue
            total_value += val * weight
            total_weight += weight
        return (_fmt_num(total_value / total_weight), target_season, ST_FRESH) if total_weight else ("", target_season, ST_NEEDS_VER)
    if aggregate == "multi_sum_avg":
        values = []
        for col in spec["value_columns"]:
            vals = [_num(row.get(col)) for row in target_rows]
            vals = [v for v in vals if v is not None]
            if not vals:
                continue
            if "Yield" in col:
                values.append(f"{col}={_fmt_num(sum(vals) / len(vals))}")
            else:
                values.append(f"{col}={_fmt_num(sum(vals))}")
        return ("; ".join(values), target_season, ST_FRESH) if values else ("", target_season, ST_NEEDS_VER)
    total = 0.0
    count = 0
    for row in target_rows:
        for col in spec["value_columns"]:
            val = _num(row.get(col))
            if val is not None:
                total += val
                count += 1
                break
    return (_fmt_num(total), target_season, ST_FRESH) if count else ("", target_season, ST_NEEDS_VER)


def _ocsb_cached_rows(target_season: str) -> list[dict]:
    try:
        from update_data_csv import read_all_rows
    except ImportError:
        return []
    rows = []
    now = beijing_now().replace(tzinfo=None)
    for row in read_all_rows():
        if row.get("country") != "泰国" or row.get("source_name") != "OCSB":
            continue
        if row.get("season") != target_season:
            continue
        if row.get("status") not in (ST_FRESH, "valid", ST_CACHED):
            continue
        try:
            age = (now - datetime.strptime(row.get("data_date", ""), "%Y-%m-%d")).days
        except ValueError:
            age = 9999
        if age <= config.get("freshness", {}).get("production_data_days", 90):
            cached = dict(row)
            cached["status"] = ST_CACHED
            cached["source_channel"] = "cached"
            cached["source_status"] = ST_CACHED
            rows.append(cached)
    return rows


def _ocsb_record(spec: dict, value: str, status: str, data_date: str, published_at: str,
                 source_url: str, season: str, channel: str, text: str = "",
                 official_level: str = "", region: str = "") -> dict:
    rec = {
        "country": "泰国",
        "region": region,
        "indicator": spec.get("label", spec["indicator"]),
        "value": value if status == ST_FRESH else "",
        "value_or_fact": value if status == ST_FRESH else text,
        "unit": spec.get("unit", ""),
        "data_date": data_date,
        "published_at": published_at,
        "fetched_at": beijing_now().strftime("%Y-%m-%d %H:%M:%S"),
        "source_name": "OCSB",
        "source_url": source_url,
        "source_status": status,
        "source_channel": channel,
        "official_level": official_level or _status_to_official_level(status),
        "data_type": spec.get("data_type", DT_ACTUAL),
        "target_contract": TARGET_CONTRACT,
        "season": season,
        "status": status,
        "notes": "",
    }
    if status == ST_NOT_PUBLISHED:
        rec["notes"] = f"OCSB Open Data最新可识别榨季尚未公布{season}可验证数据。"
    elif status == ST_SOURCE_503:
        rec["notes"] = "OCSB主站返回503。"
    elif status == ST_SOURCE_UNAVAILABLE:
        rec["notes"] = "泰国官方来源暂时不可访问。"
    elif status == ST_CACHED:
        rec["notes"] = "使用CSV中未过期OCSB官方缓存。"
    elif status == ST_NO_NEW_DATA:
        rec["notes"] = "OCSB官方来源暂无新数据。"
    elif status == ST_NEEDS_VER:
        rec["notes"] = text or "数据需人工核验。"
    return rec


def _status_to_official_level(status: str) -> str:
    if status == ST_FRESH:
        return OL_OCSB_ACTUAL
    if status == ST_NOT_PUBLISHED:
        return OL_NOT_PUBLISHED
    if status in (ST_CACHED, ST_NO_NEW_DATA):
        return OL_NOT_PUBLISHED
    return OL_NOT_PUBLISHED


def _make_thai_record(indicator: str, value: str, unit: str, data_date: str, published_at: str,
                      source_name: str, source_url: str, channel: str, official_level: str,
                      season: str, status: str, data_type: str = DT_ACTUAL,
                      text_value: str = "", region: str = "", notes: str = "",
                      impact_direction: str = "", confidence: str = "") -> dict:
    """构建一条泰国标准化记录。"""
    return {
        "country": "泰国",
        "region": region,
        "indicator": indicator,
        "value": value if status == ST_FRESH else "",
        "value_or_fact": value if status == ST_FRESH else (text_value or value),
        "text_value": text_value[:300] if text_value else "",
        "unit": unit,
        "data_date": data_date,
        "published_at": published_at,
        "fetched_at": beijing_now().strftime("%Y-%m-%d %H:%M:%S"),
        "source_name": source_name,
        "source_url": source_url,
        "source_status": status,
        "source_channel": channel,
        "official_level": official_level,
        "data_type": data_type,
        "target_contract": TARGET_CONTRACT,
        "season": season,
        "status": status,
        "impact_direction": impact_direction,
        "confidence": confidence,
        "notes": notes,
    }


# ============================================================
# 泰国 PRD 政府新闻抓取
# ============================================================

def _fetch_thailand_prd_news(target_date: str, target_season: str) -> list[dict]:
    """
    从泰国政府PRD公开新闻中搜索OCSB/工业部/甘蔗与糖业相关内容。
    只使用同时满足以下条件的内容：
    1. 来源域名属于泰国政府PRD
    2. 正文明确提到工业部、OCSB或甘蔗与糖业委员会
    3. 明确包含当前榨季或当前压榨期
    4. 具体数字有清楚单位
    5. 发布时间和数据期可识别
    """
    cfg = config.get("ocsb", {})
    prd_cfg = config.get("thailand_sources", {}).get("thai_prd", {})
    prd_url = prd_cfg.get("url", "https://www.prd.go.th/")
    keywords = cfg.get("prd_search_keywords", ["อ้อย", "น้ำตาล", "OCSB"])

    results = []
    logger.info("PRD搜索: url=%s keywords=%s", prd_url, keywords[:3])

    # 尝试PRD主站
    raw = http_get_text(prd_url, timeout=20, encoding="utf-8")
    if not raw:
        # 尝试英文版
        raw = http_get_text("https://www.prd.go.th/en/", timeout=20, encoding="utf-8")
    if not raw:
        logger.info("PRD主站不可访问")
        return results

    clean = clean_html(raw)
    thai_kw = ["อ้อย", "น้ำตาล", "OCSB", "อุตสาหกรรม", "คณะกรรมการอ้อย"]
    en_kw = ["sugarcane", "sugar", "OCSB", "Ministry of Industry", "cane"]

    # 搜索PRD新闻链接
    found_links = []
    for m in re.finditer(r'href=["\']([^"\']*?prd\.go\.th[^"\']*?)["\'][^>]*>(.*?)</a>', raw, re.I | re.S):
        href = html.unescape(m.group(1))
        anchor = clean_html(m.group(2))
        ctx = f"{anchor} {clean[max(0, m.start()-200):m.end()+300]}"
        if any(kw.lower() in ctx.lower() for kw in thai_kw + en_kw):
            full_url = urljoin(prd_url, href)
            if full_url not in [l[0] for l in found_links]:
                found_links.append((full_url, anchor[:120]))

    # 如果主页没有链接，尝试搜索
    if not found_links:
        import urllib.parse
        for kw in ["sugar", "น้ำตาล", "อ้อย", "sugarcane"]:
            search_url = f"{prd_url}?s={urllib.parse.quote(kw)}"
            search_raw = http_get_text(search_url, timeout=15, encoding="utf-8")
            if search_raw:
                for m in re.finditer(r'href=["\']([^"\']*?)["\']', search_raw, re.I):
                    href = html.unescape(m.group(1))
                    ctx = search_raw[max(0, m.start()-200):m.end()+300]
                    if any(k.lower() in ctx.lower() for k in thai_kw + en_kw):
                        full_url = urljoin(prd_url, href)
                        if full_url not in [l[0] for l in found_links]:
                            found_links.append((full_url, ""))

    logger.info("PRD找到 %d 条相关链接", len(found_links))

    for link_url, link_title in found_links[:10]:
        page = http_get_text(link_url, timeout=15, encoding="utf-8")
        if not page:
            continue
        text = clean_html(page)
        if len(text) < 100:
            continue

        # 检查是否包含OCSB/工业部/甘蔗与糖业委员会
        has_authority = any(
            kw.lower() in text.lower()
            for kw in ["ocsb", "office of the cane and sugar board",
                       "ministry of industry", "กระทรวงอุตสาหกรรม",
                       "คณะกรรมการอ้อยและน้ำตาลทราย", "cane and sugar"]
        )
        if not has_authority:
            continue

        published_at = parse_date_anywhere(text[:800], target_date)

        # 提取当前榨季信息
        season_patterns = [
            r"(25\d{2})\s*/\s*(26\d{2})",  # 2568/2569
            r"(2025)\s*/\s*(2026|26)",       # 2025/2026
            r"(2025)\s*-\s*(2026|26)",       # 2025-2026
        ]
        has_current_season = False
        for pat in season_patterns:
            if re.search(pat, text):
                has_current_season = True
                break

        # 提取具体指标
        indicators_found = []

        # 甘蔗压榨量
        crush_m = re.search(r"(?:อ้อย|sugarcane|sugar\s*cane)[^0-9]*?(\d[\d,]*(?:\.\d+)?)\s*(?:ล้านตัน|million\s*tonnes?|万吨|tons)", text, re.I)
        if crush_m:
            indicators_found.append(("sugarcane_crushed", crush_m.group(1).replace(",", ""), "吨"))

        # 糖产量
        sugar_m = re.search(r"(?:น้ำตาล|sugar\s*production)[^0-9]*?(\d[\d,]*(?:\.\d+)?)\s*(?:ล้านตัน|million\s*tonnes?|万吨|tons)", text, re.I)
        if sugar_m:
            indicators_found.append(("sugar_production", sugar_m.group(1).replace(",", ""), "吨"))

        # 鲜蔗占比
        fresh_m = re.search(r"(?:fresh\s*cane|อ้อยสด)[^0-9]*?(\d+(?:\.\d+)?)\s*%", text, re.I)
        if fresh_m:
            indicators_found.append(("fresh_cane_share", fresh_m.group(1), "%"))

        # 燃烧甘蔗占比
        burnt_m = re.search(r"(?:burnt\s*cane|อ้อยเผา|อ้อยไฟไหม้)[^0-9]*?(\d+(?:\.\d+)?)\s*%", text, re.I)
        if burnt_m:
            indicators_found.append(("burnt_cane_share", burnt_m.group(1), "%"))

        # 每吨甘蔗产糖量
        yield_m = re.search(r"(?:yield|ผลผลิต)[^0-9]*?(\d+(?:\.\d+)?)\s*(?:kg|กิโลกรัม|公斤)\s*(?:per\s*tonne?|ต่อตัน)", text, re.I)
        if yield_m:
            indicators_found.append(("sugar_yield_per_cane", yield_m.group(1), "公斤/吨"))

        # 糖厂数量
        mills_m = re.search(r"(\d+)\s*(?:โรงงาน|mills?|factories|糖厂)", text, re.I)
        if mills_m:
            indicators_found.append(("number_of_mills", mills_m.group(1), "家"))

        # 压榨完成
        if re.search(r"(?:crushing\s*(?:completed|finished|ended)|ปิดหีบ|หมดฤดู)", text, re.I):
            indicators_found.append(("crushing_completed", "是", "布尔"))

        if not indicators_found and not has_current_season:
            continue

        # 确定来源名称
        if "ministry of industry" in text.lower() or "กระทรวงอุตสาหกรรม" in text.lower():
            src_name = "Thai Government PRD / Ministry of Industry"
        elif "ocsb" in text.lower():
            src_name = "Thai Government PRD / OCSB"
        else:
            src_name = "Thai Government PRD"

        for ind, val, unt in indicators_found:
            results.append(_make_thai_record(
                indicator=f"PRD-{ind}",
                value=val, unit=unt,
                data_date=published_at, published_at=published_at,
                source_name=src_name, source_url=link_url,
                channel=SC_GOVERNMENT_PRD,
                official_level=OL_THAI_GOVERNMENT,
                season=target_season, status=ST_FRESH, data_type=DT_ACTUAL,
                text_value=f"{THAI_STAGE_INDICATORS.get(ind, ind)}: {val} {unt}",
                notes=f"PRD新闻提取 | 标题: {link_title[:80]}"
            ))

        if not indicators_found and has_current_season:
            results.append(_make_thai_record(
                indicator="PRD-season_update",
                value="", unit="文本",
                data_date=published_at, published_at=published_at,
                source_name=src_name, source_url=link_url,
                channel=SC_GOVERNMENT_PRD,
                official_level=OL_THAI_GOVERNMENT,
                season=target_season, status=ST_FRESH, data_type=DT_ACTUAL,
                text_value=text[:300],
                notes=f"PRD新闻 | 标题: {link_title[:80]}"
            ))

    logger.info("PRD抓取完成: %d 条记录", len(results))
    return results


# ============================================================
# 泰国 OCSB 官方 Facebook
# ============================================================

def _fetch_thailand_ocsb_facebook(target_date: str, target_season: str) -> list[dict]:
    """
    从OCSB官方Facebook读取无需登录即可访问的公开内容。
    规则:
      1. 只读取公开内容
      2. 不模拟Cookie
      3. 不调用需要登录的接口
      4. 不做OCR
      5. 如果只能拿到标题和链接但拿不到正文 → not_published
      6. 如果正文可读取 → 提取发布时间、榨季、原始事实
    """
    fb_url = config.get("ocsb", {}).get("facebook_mobile_url",
                "https://mbasic.facebook.com/ocsbofficial/")
    results = []
    logger.info("OCSB Facebook: url=%s", fb_url)

    # 尝试mbasic版本（通常更简单，对爬虫更友好）
    raw = http_get_text(fb_url, timeout=20, encoding="utf-8")
    if not raw:
        logger.info("OCSB Facebook页面不可访问（可能受限）")
        results.append(_make_thai_record(
            indicator="OCSB Facebook",
            value="", unit="文本",
            data_date=target_date, published_at=target_date,
            source_name="OCSB Official Facebook", source_url=fb_url,
            channel=SC_OFFICIAL_FACEBOOK,
            official_level=OL_NOT_PUBLISHED,
            season=target_season, status=ST_SOURCE_UNAVAILABLE, data_type=DT_ACTUAL,
            text_value="OCSB官方Facebook页面不可公开访问。",
            notes="Facebook页面受限；不模拟登录。"
        ))
        return results

    clean = clean_html(raw)
    if len(clean) < 50:
        logger.info("OCSB Facebook页面内容不足（可能被拦截或需登录）")
        results.append(_make_thai_record(
            indicator="OCSB Facebook",
            value="", unit="文本",
            data_date=target_date, published_at=target_date,
            source_name="OCSB Official Facebook", source_url=fb_url,
            channel=SC_OFFICIAL_FACEBOOK,
            official_level=OL_NOT_PUBLISHED,
            season=target_season, status=ST_SOURCE_UNAVAILABLE, data_type=DT_ACTUAL,
            text_value="OCSB Facebook仅链接不可读。",
            notes="页面内容不足，可能需登录。"
        ))
        return results

    # 尝试提取帖子
    # mbasic facebook 的帖子通常在特定结构中
    post_patterns = [
        r'<div[^>]*class="[^"]*post[^"]*"[^>]*>(.*?)</div>',
        r'<article[^>]*>(.*?)</article>',
    ]
    posts_found = []
    for pat in post_patterns:
        for m in re.finditer(pat, raw, re.I | re.S):
            post_text = clean_html(m.group(1))
            if len(post_text) > 30:
                posts_found.append(post_text)

    # 如果没有结构化帖子，尝试直接提取正文文本
    if not posts_found and len(clean) > 100:
        posts_found.append(clean[:2000])

    for post_text in posts_found[:5]:
        # 提取日期
        post_date = parse_date_anywhere(post_text[:500], target_date)

        # 检查是否包含榨季信息
        season_found = bool(
            re.search(r"(25\d{2})\s*/\s*(26\d{2})", post_text) or
            re.search(r"(2025)\s*/\s*(2026|26)", post_text)
        )

        # 提取具体指标
        thai_kw_post = ["อ้อย", "น้ำตาล", "การผลิต", "ผลผลิต", "ส่งออก"]
        en_kw_post = ["sugarcane", "sugar", "production", "crushing", "export", "million"]

        has_data = any(kw.lower() in post_text.lower() for kw in thai_kw_post + en_kw_post)

        if has_data or season_found:
            results.append(_make_thai_record(
                indicator="OCSB Facebook",
                value="", unit="文本",
                data_date=post_date, published_at=post_date,
                source_name="OCSB Official Facebook", source_url=fb_url,
                channel=SC_OFFICIAL_FACEBOOK,
                official_level=OL_OCSB_POLICY if not season_found else OL_OCSB_ACTUAL,
                season=target_season, status=ST_FRESH if (has_data and season_found) else ST_NEEDS_VER,
                data_type=DT_ACTUAL,
                text_value=post_text[:300],
                notes=f"Facebook公开帖子 | 日期: {post_date}"
            ))

    if not results:
        results.append(_make_thai_record(
            indicator="OCSB Facebook",
            value="", unit="文本",
            data_date=target_date, published_at=target_date,
            source_name="OCSB Official Facebook", source_url=fb_url,
            channel=SC_OFFICIAL_FACEBOOK,
            official_level=OL_NOT_PUBLISHED,
            season=target_season, status=ST_NOT_PUBLISHED,
            data_type=DT_ACTUAL,
            text_value="OCSB Facebook未找到当前榨季的可核验数据。",
            notes="页面可访问但无相关数据。"
        ))

    logger.info("OCSB Facebook抓取完成: %d 条记录", len(results))
    return results


# ============================================================
# 泰国 OCSB 区域中心
# ============================================================

def _fetch_thailand_regional_center(target_date: str, target_season: str) -> list[dict]:
    """
    从OCSB区域推广中心获取地区信息。
    只能用于: 地区种植、甘蔗生长、天气灾情、区域政策培训、区域压榨。
    不得将区域数据扩展为全国结论。
    """
    results = []
    # OCSB区域中心通常没有固定的结构化数据入口
    # 此函数为将来预留，当前返回空列表
    logger.info("OCSB区域中心: 当前无可用结构化入口，跳过。")
    return results


# ============================================================
# 泰国官方预测
# ============================================================

def _fetch_thailand_official_forecast(target_date: str, target_season: str) -> list[dict]:
    """
    使用泰国官方预测数据作为最后补充。
    只允许: OCSB官方预测、泰国工业部官方预测、泰国政府官方发布中的预测。
    必须单独标记 data_type=forecast, official_level=official_forecast。
    """
    results = []
    # 优先检查CSV中是否已有未过期OCSB预测
    cached = _ocsb_cached_rows(target_season)
    forecast_cached = [
        r for r in cached
        if r.get("data_type") == DT_FORECAST
        and r.get("official_level") == OL_OFFICIAL_FORECAST
    ]
    if forecast_cached:
        for row in forecast_cached:
            row["status"] = ST_CACHED
            row["source_channel"] = SC_CACHED
            results.append(row)
        logger.info("泰国官方预测使用缓存: %d 条", len(results))
        return results

    # 尝试通过OCSB Open Data获取预测数据集
    cfg = config.get("ocsb", {})
    forecast_query = "ข้อมูลการพยากรณ์"
    api_status, packages = _ckan_search(forecast_query, cfg)
    if api_status == 200 and packages:
        package = packages[0]
        resources = _resource_candidates(package)
        for res in resources[:1]:
            fmt = res.get("format", "")
            res_url = res.get("url", "")
            rows = _read_resource_rows(res_url, fmt)
            for spec in OCSB_DATASET_SPECS:
                if spec.get("data_type") != DT_FORECAST:
                    continue
                value, latest_season, status = _aggregate_ocsb_rows(rows, spec, target_season)
                if status == ST_FRESH:
                    modified = (res.get("last_modified") or res.get("created") or target_date)
                    res_date = str(modified)[:10]
                    results.append(_make_thai_record(
                        indicator=spec.get("label", spec["indicator"]),
                        value=value, unit=spec.get("unit", ""),
                        data_date=res_date, published_at=res_date,
                        source_name="OCSB", source_url=res_url,
                        channel=SC_OPEN_DATA,
                        official_level=OL_OFFICIAL_FORECAST,
                        season=target_season, status=ST_FRESH, data_type=DT_FORECAST,
                        notes="OCSB官方预测数据。注意: 预测不等于实际。"
                    ))
                elif latest_season:
                    results.append(_make_thai_record(
                        indicator=spec.get("label", spec["indicator"]),
                        value="", unit=spec.get("unit", ""),
                        data_date=target_date, published_at=target_date,
                        source_name="OCSB", source_url="",
                        channel=SC_OPEN_DATA,
                        official_level=OL_OFFICIAL_FORECAST,
                        season=target_season, status=ST_NOT_PUBLISHED, data_type=DT_FORECAST,
                        text_value=f"OCSB预测数据最新可识别榨季为{latest_season}，尚未发布{target_season}预测。",
                        notes="OCSB官方预测未发布。"
                    ))

    logger.info("泰国官方预测: %d 条记录", len(results))
    return results


# ============================================================
# 泰国数据主入口 — 按优先级尝试所有官方来源
# ============================================================

def fetch_thailand_ocsb_production(target_date: str) -> list[dict]:
    """
    泰国数据主入口 — 按官方来源优先级依次尝试:
      1. SugarZone全国分区生产报告（最高优先级）
      2. OCSB Open Data 结构化数据
      3. 泰国政府PRD官方新闻稿
      4. OCSB主站官方公告
      5. OCSB官方Facebook公开内容
      6. OCSB区域推广中心
      7. CSV中未过期OCSB官方缓存
      8. 官方预测数据

    规则:
      - 不使用越南数据
      - 不使用普通媒体或商业资讯代替泰国官方数据
      - 不绕过登录、验证码或访问限制
      - 不允许反推全国产量
      - 不允许将预测写成实际
    """
    cfg = config.get("ocsb", {})
    if not cfg.get("enabled", True):
        return []

    main_url = cfg.get("production_url", "")
    open_base = cfg.get("open_data_base_url", "https://opendata.ocsb.go.th/")
    if not main_url or not is_source_whitelisted("泰国", main_url):
        return []
    if not is_source_whitelisted("泰国", open_base):
        return []

    target_season = normalize_thai_season(cfg.get("target_season", "")) or \
                    thai_buddhist_to_season(cfg.get("target_season_thai", ""))
    logger.info("=" * 50)
    logger.info("泰国数据抓取: 目标榨季=%s | 日期=%s", target_season, target_date)
    logger.info("=" * 50)

    all_results: list[dict] = []
    has_fresh_data = False
    has_national_total = False

    # ── 第1优先级: SugarZone 生产报告 ─────────────────────
    logger.info("--- [优先级1] SugarZone生产报告 ---")
    sugarzone_results = fetch_thailand_sugarzone_production(target_date)
    sugarzone_fresh = [r for r in sugarzone_results if r.get("status") == ST_FRESH]
    sugarzone_national = [r for r in sugarzone_fresh
                          if "accumulated" in r.get("indicator", "")]
    all_results.extend(sugarzone_results)
    if sugarzone_fresh:
        has_fresh_data = True
        logger.info("SugarZone找到 %d 条当前榨季数据", len(sugarzone_fresh))
    if sugarzone_national:
        has_national_total = True
        logger.info("SugarZone已获取全国累计产量数据")

    # ── 第2优先级: OCSB Open Data 结构化数据 ──────────────
    if not has_fresh_data or not has_national_total:
        logger.info("--- [优先级2] OCSB Open Data ---")
    open_status = _ocsb_head_status(open_base, timeout=15, retries=2)
    logger.info("OCSB Open Data入口状态码: %s", open_status)

    for spec in OCSB_DATASET_SPECS:
        query = spec["query"]
        api_status, packages = _ckan_search(query, cfg)
        logger.info("CKAN搜索: query=%s status=%s results=%d",
                    query, api_status, len(packages))

        if not packages:
            all_results.append(_ocsb_record(
                spec, "", ST_NO_NEW_DATA, target_date, target_date, open_base,
                target_season, SC_OPEN_DATA,
                f"OCSB Open Data未找到数据集: {query}",
                official_level=OL_NOT_PUBLISHED
            ))
            continue

        package = packages[0]
        title = package.get("title") or package.get("name") or query
        resources = _resource_candidates(package)
        if not resources:
            all_results.append(_ocsb_record(
                spec, "", ST_NEEDS_VER, target_date, target_date,
                package.get("url") or open_base, target_season, SC_OPEN_DATA,
                f"OCSB数据集无CSV/XLSX资源: {title}",
                official_level=OL_NOT_PUBLISHED
            ))
            continue

        chosen = resources[0]
        fmt = chosen.get("format", "")
        res_url = chosen.get("url", "")
        modified = (chosen.get("last_modified") or chosen.get("created") or
                    package.get("metadata_modified") or target_date)
        res_date = str(modified)[:10]
        rows = _read_resource_rows(res_url, fmt)
        value, latest_season, status = _aggregate_ocsb_rows(rows, spec, target_season)
        logger.info("Open Data聚合: indicator=%s value=%s latest_season=%s status=%s",
                    spec["indicator"], value[:40] if value else "-", latest_season, status)

        if status == ST_FRESH:
            rec = _ocsb_record(spec, value, ST_FRESH, res_date, res_date, res_url,
                              target_season, SC_OPEN_DATA,
                              official_level=OL_OCSB_ACTUAL)
            all_results.append(rec)
            has_fresh_data = True
            if spec.get("indicator") == "sugar_production":
                has_national_total = True
        elif status == ST_NOT_PUBLISHED:
            rec = _ocsb_record(
                spec, "", ST_NOT_PUBLISHED, res_date, res_date, res_url,
                target_season, SC_OPEN_DATA,
                f"OCSB Open Data最新可识别榨季为{latest_season or '未知'}，"
                f"尚未公布{target_season}可验证数据。",
                official_level=OL_NOT_PUBLISHED
            )
            all_results.append(rec)
        else:
            all_results.append(_ocsb_record(
                spec, "", ST_NEEDS_VER, res_date, res_date, res_url,
                target_season, SC_OPEN_DATA,
                f"OCSB资源字段缺失或数值异常: {title}",
                official_level=OL_NOT_PUBLISHED
            ))

    logger.info("Open Data完成: has_fresh=%s has_national_total=%s",
                has_fresh_data, has_national_total)

    # ── 第3优先级: 泰国政府PRD ────────────────────────────
    if not has_fresh_data or not has_national_total:
        logger.info("--- [优先级3] 泰国政府PRD ---")
        prd_results = _fetch_thailand_prd_news(target_date, target_season)
        prd_fresh = [r for r in prd_results if r.get("status") == ST_FRESH]
        all_results.extend(prd_results)
        if prd_fresh:
            has_fresh_data = True
            logger.info("PRD找到 %d 条当前榨季官方信息", len(prd_fresh))

    # ── 第4优先级: OCSB主站 ───────────────────────────────
    if not has_fresh_data:
        logger.info("--- [优先级4] OCSB主站 ---")
        main_status = _ocsb_head_status(main_url, timeout=12, retries=2)
        logger.info("OCSB主站状态码: %s", main_status)

        if main_status == 200:
            main_body_status, main_body = _ocsb_get(main_url, timeout=12, retries=2)
            if main_body_status == 200 and main_body:
                clean = _decode_table_bytes(main_body)
                clean_text = clean_html(clean)
                target_field = cfg.get("target_field", "รวมน้ำตาลทรายปริ")
                idx = clean_text.find(target_field)
                if idx >= 0:
                    ctx = clean_text[idx:idx + 800]
                    nums = re.findall(r"\d[\d,]*(?:\.\d+)?", ctx)
                    if nums:
                        value = nums[-1].replace(",", "")
                        spec = OCSB_DATASET_SPECS[0]
                        all_results.append(_ocsb_record(
                            spec, value, ST_FRESH,
                            thai_date_to_iso(cfg.get("target_date_hint", "")) or target_date,
                            target_date, main_url, target_season, SC_OCSB_MAIN,
                            official_level=OL_OCSB_ACTUAL
                        ))
                        has_fresh_data = True
                        has_national_total = True
                        logger.info("OCSB主站提取到产量数据: %s", value)
                else:
                    all_results.append(_ocsb_record(
                        OCSB_DATASET_SPECS[0], "", ST_NEEDS_VER,
                        target_date, target_date, main_url, target_season, SC_OCSB_MAIN,
                        "OCSB主站可访问但目标字段缺失。",
                        official_level=OL_NOT_PUBLISHED
                    ))
        elif main_status == 503:
            logger.warning("OCSB主站返回503")
            all_results.append(_ocsb_record(
                OCSB_DATASET_SPECS[0], "", ST_SOURCE_503,
                target_date, target_date, main_url, target_season, SC_OCSB_MAIN,
                "OCSB主站返回503，继续尝试其他官方来源。",
                official_level=OL_NOT_PUBLISHED
            ))

    # ── 第5优先级: OCSB官方Facebook ───────────────────────
    if not has_fresh_data:
        logger.info("--- [优先级5] OCSB官方Facebook ---")
        fb_results = _fetch_thailand_ocsb_facebook(target_date, target_season)
        fb_fresh = [r for r in fb_results if r.get("status") == ST_FRESH]
        all_results.extend(fb_results)
        if fb_fresh:
            has_fresh_data = True
            logger.info("OCSB Facebook找到 %d 条有效信息", len(fb_fresh))

    # ── 第6优先级: OCSB区域中心 ───────────────────────────
    logger.info("--- [优先级6] OCSB区域中心 ---")
    regional_results = _fetch_thailand_regional_center(target_date, target_season)
    all_results.extend(regional_results)

    # ── 第7优先级: CSV未过期OCSB缓存 ─────────────────────
    if not has_fresh_data:
        logger.info("--- [优先级7] CSV缓存 ---")
        cached = _ocsb_cached_rows(target_season)
        if cached:
            logger.info("使用CSV中未过期OCSB缓存: %d条", len(cached))
            valid_cached = [r for r in cached
                           if r.get("status") in (ST_FRESH, ST_CACHED, "valid")]
            if valid_cached:
                converted = []
                for row in valid_cached:
                    converted.append(_make_thai_record(
                        indicator=row.get("indicator", ""),
                        value=row.get("value", ""),
                        unit=row.get("unit", ""),
                        data_date=row.get("data_date", ""),
                        published_at=row.get("published_at", ""),
                        source_name=row.get("source_name", "OCSB"),
                        source_url=row.get("source_url", ""),
                        channel=SC_CACHED,
                        official_level=OL_OCSB_ACTUAL,
                        season=target_season, status=ST_CACHED, data_type=row.get("data_type", DT_ACTUAL),
                        text_value=row.get("text_value", ""),
                        notes="OCSB未发布更新，使用CSV中未过期OCSB官方缓存。"
                    ))
                all_results.extend(converted)
                has_fresh_data = True

    # ── 第8优先级: 官方预测 ───────────────────────────────
    if not has_fresh_data:
        logger.info("--- [优先级8] 官方预测 ---")
        forecast_results = _fetch_thailand_official_forecast(target_date, target_season)
        all_results.extend(forecast_results)

    # ── 日志汇总 ──────────────────────────────────────────
    fresh_count = sum(1 for r in all_results if r.get("status") == ST_FRESH)
    cached_count = sum(1 for r in all_results if r.get("status") == ST_CACHED)
    not_pub_count = sum(1 for r in all_results if r.get("status") == ST_NOT_PUBLISHED)
    s503_count = sum(1 for r in all_results if r.get("status") == ST_SOURCE_503)
    logger.info("泰国数据汇总: fresh=%d cached=%d not_published=%d source_503=%d total=%d",
                fresh_count, cached_count, not_pub_count, s503_count, len(all_results))
    logger.info("泰国最终产量: %s", "已获取" if has_national_total else "未发布")
    logger.info("泰国阶段性数据: %s", "有" if has_fresh_data else "无")

    return all_results


def _extract_pdf_text_all(pdf_bytes: bytes) -> str:
    try:
        from PyPDF2 import PdfReader
        reader = PdfReader(io.BytesIO(pdf_bytes))
        return "\n".join((p.extract_text() or "") for p in reader.pages)
    except Exception as e:
        logger.info("PDF解析失败: %s", e)
        return ""


def fetch_india_coopsugar_production(target_date: str) -> list[dict]:
    cfg = config.get("coopsugar", {})
    if not cfg.get("enabled", True):
        return []
    url = cfg.get("statistics_url", "")
    if not url or not is_source_whitelisted("印度", url):
        return []
    raw = http_get_text(url, timeout=25)
    if not raw:
        return []
    pdf_links = []
    for m in re.finditer(r'href=["\']([^"\']+\.pdf[^"\']*)["\'][^>]*>(.*?)</a>', raw, re.I | re.S):
        href = urljoin(url, html.unescape(m.group(1)))
        label = clean_html(m.group(2))
        if "crushing" in (href + " " + label).lower() or "report" in (href + " " + label).lower():
            pdf_links.append(href)
    if not pdf_links:
        return []

    pdf_url = pdf_links[0]
    pdf = http_get_bytes(pdf_url, timeout=35)
    text = _extract_pdf_text_all(pdf) if pdf else ""
    if not text:
        return []
    date_match = re.search(r"(\d{1,2})-(\d{1,2})-(\d{4})", text)
    data_date = target_date
    if date_match:
        data_date = f"{int(date_match.group(3)):04d}-{int(date_match.group(2)):02d}-{int(date_match.group(1)):02d}"
    row = re.search(r"ALL\s+INDIA\s+(.+?)(?:VARIANCE|Contribution|MD/|$)", text, re.I | re.S)
    value = ""
    if row:
        nums = re.findall(r"\d+(?:\.\d+)?%?", row.group(1))
        # ALL INDIA row columns: operated mills, previous, closed, previous, crushing mills,
        # previous, cane crushed current/previous, sugar production current/previous, recovery current/previous.
        if len(nums) >= 10:
            value = nums[8].rstrip("%")
    if not value:
        return []
    return [{
        "country": "印度",
        "indicator": "ALL INDIA Sugar Production In Lmts",
        "value_or_fact": value,
        "unit": "Lmts",
        "data_date": data_date,
        "published_at": data_date,
        "fetched_at": beijing_now().strftime("%Y-%m-%d %H:%M:%S"),
        "source_name": cfg.get("source_name", "NFCSF"),
        "source_url": pdf_url,
        "data_type": DT_ACTUAL,
        "target_contract": TARGET_CONTRACT,
        "season": "2025/2026",
        "status": ST_FRESH,
        "notes": "ALL INDIA Sugar Production In Lmts",
    }]


def _extract_msweet_domestic_items(raw: str, list_url: str) -> list[dict]:
    return _extract_msweet_list_items(raw, list_url)


def _is_meeting_article(title: str, summary: str) -> bool:
    """Check if an article is a meeting/conference (should not be used as production data)."""
    cfg = config.get("msweet", {})
    exclude_kw = cfg.get("domestic_exclude_keywords", [
        "座谈会", "论坛", "研讨会", "产业升级", "签约", "招商",
        "会议", "领导调研", "企业活动", "品牌活动",
    ])
    haystack = f"{title} {summary}"
    return any(kw in haystack for kw in exclude_kw)


def _extract_production_numbers(text: str) -> dict:
    """
    Extract production/sales/inventory numbers from Chinese sugar article text.
    Returns dict with extracted indicator values.
    """
    results = {}

    # Cumulative sugar production (累计产糖)
    m = re.search(r"累计产糖[^\d]*?([\d,.]+)\s*(?:万吨|吨)", text)
    if m:
        val = m.group(1).replace(",", "")
        results["sugar_production_accumulated"] = val

    # Mixed sugar production (产混合糖)
    m = re.search(r"产混合糖[^\d]*?([\d,.]+)\s*(?:万吨|吨)", text)
    if m:
        val = m.group(1).replace(",", "")
        results["mixed_sugar_production"] = val

    # Cumulative sugar sales (累计销糖)
    m = re.search(r"累计销糖[^\d]*?([\d,.]+)\s*(?:万吨|吨)", text)
    if m:
        val = m.group(1).replace(",", "")
        results["sugar_sales_accumulated"] = val

    # Industrial inventory (工业库存)
    m = re.search(r"工业库存[^\d]*?([\d,.]+)\s*(?:万吨|吨)", text)
    if m:
        val = m.group(1).replace(",", "")
        results["industrial_inventory"] = val

    # Sales ratio (产销率)
    m = re.search(r"产销率[^\d]*?([\d,.]+)\s*%", text)
    if m:
        results["sales_ratio"] = m.group(1)

    # Sugarcane crushed (入榨甘蔗)
    m = re.search(r"入榨甘蔗[^\d]*?([\d,.]+)\s*(?:万吨|吨)", text)
    if m:
        val = m.group(1).replace(",", "")
        results["sugarcane_crushed"] = val

    # Sugar yield (产糖率)
    m = re.search(r"产糖率[^\d]*?([\d,.]+)\s*%", text)
    if m:
        results["sugar_yield"] = m.group(1)

    # Mills closed (收榨)
    if "收榨" in text:
        m = re.search(r"(\d+)\s*(?:家|间|座)\s*(?:糖厂|厂).*?收榨", text)
        if m:
            results["mills_closed"] = m.group(1)
        elif "全部收榨" in text or "收榨完毕" in text:
            results["mills_closed"] = "全部"

    # Mills operating (开榨)
    if "开榨" in text:
        m = re.search(r"(\d+)\s*(?:家|间|座)\s*(?:糖厂|厂).*?开榨", text)
        if m:
            results["mills_operating"] = m.group(1)

    return results


def _detect_region(text: str) -> str:
    """Detect which region the article is about."""
    if "云南" in text:
        return "云南"
    if "广西" in text:
        return "广西"
    if "广东" in text:
        return "广东"
    if "海南" in text:
        return "海南"
    if "全国" in text or "中国" in text:
        return "全国"
    return ""


def _classify_article_region(title: str, summary: str) -> str:
    """根据标题和摘要判断文章所属地区。只用地名做判断，不用通用关键词。"""
    # 标题中的地名前缀是最可靠的信号
    if "云南" in title:
        return "云南"
    if "广西" in title:
        return "广西"
    if "广东" in title:
        return "广东"
    if "海南" in title:
        return "海南"
    if "全国" in title:
        return "全国"

    # 标题无地名时，检查摘要开头（避免用页面上下文中的其他地区名）
    # 只检查摘要前100字符
    summary_head = summary[:100]
    if "云南" in summary_head:
        return "云南"
    if "广西" in summary_head:
        return "广西"
    if "全国" in summary_head:
        return "全国"
    return ""


def _fetch_article_detail(url: str, timeout: int = 15) -> str | None:
    """获取文章详情页正文。"""
    raw = http_get_text(url, timeout=timeout)
    if not raw:
        return None
    text = _extract_msweet_article_text(raw)
    return text if len(text) > 80 else None


def _is_forecast_text(text: str) -> bool:
    """判断文本是否包含预估/预计表述。"""
    forecast_markers = ["预计", "预估", "有望", "大概率", "估计", "预期", "估算"]
    return any(m in text for m in forecast_markers)


def _extract_enhanced_production_numbers(text: str) -> dict:
    """
    增强版：从文章正文中提取产销量、库存、收榨等结构化数据。
    返回 dict[indicator] = {"value": str, "data_type": str}
    """
    results = {}

    # 预计最终产糖
    m = re.search(r"(?:预计|预估)\s*(?:最终|全[榨年]季)\s*产糖[^\d]*?([\d,.]+)\s*(?:万吨|吨)", text)
    if m:
        val = m.group(1).replace(",", "")
        results["final_sugar_production_estimate"] = {"value": val, "data_type": DT_FORECAST}

    # 累计产糖
    m = re.search(r"累计产糖[^\d]*?([\d,.]+)\s*(?:万吨|吨)", text)
    if m:
        val = m.group(1).replace(",", "")
        dt = DT_FORECAST if _is_forecast_text(text[:text.find("累计产糖") + 30]) else DT_ACTUAL
        results["sugar_production_accumulated"] = {"value": val, "data_type": dt}

    # 产混合糖
    m = re.search(r"产混合糖[^\d]*?([\d,.]+)\s*(?:万吨|吨)", text)
    if m:
        val = m.group(1).replace(",", "")
        results["mixed_sugar_production"] = {"value": val, "data_type": DT_ACTUAL}

    # 单月产糖量
    m = re.search(r"(?:\d{1,2}月)\s*(?:产糖|食糖产量)[^\d]*?([\d,.]+)\s*(?:万吨|吨)", text)
    if m:
        val = m.group(1).replace(",", "")
        results["production_monthly"] = {"value": val, "data_type": DT_ACTUAL}

    # 累计销糖
    m = re.search(r"累计销糖[^\d]*?([\d,.]+)\s*(?:万吨|吨)", text)
    if m:
        val = m.group(1).replace(",", "")
        results["sugar_sales_accumulated"] = {"value": val, "data_type": DT_ACTUAL}

    # 单月销量
    m = re.search(r"(?:\d{1,2}月)\s*(?:销量|食糖销售|销糖)[^\d]*?([\d,.]+)\s*(?:万吨|吨)", text)
    if m:
        val = m.group(1).replace(",", "")
        results["sales_monthly"] = {"value": val, "data_type": DT_ACTUAL}

    # 预计销量
    m = re.search(r"(?:预计|预估)\s*(?:\d{1,2}月)?\s*(?:销量|食糖销售|销糖)[^\d]*?([\d,.]+)\s*(?:万吨|吨)", text)
    if m and "sales_monthly" not in results:
        val = m.group(1).replace(",", "")
        results["sales_monthly"] = {"value": val, "data_type": DT_FORECAST}

    # 工业库存
    m = re.search(r"工业库存[^\d]*?([\d,.]+)\s*(?:万吨|吨)", text)
    if m:
        val = m.group(1).replace(",", "")
        results["industrial_inventory"] = {"value": val, "data_type": DT_ACTUAL}

    # 产销率
    m = re.search(r"产销率[^\d]*?([\d,.]+)\s*%", text)
    if m:
        results["sales_ratio"] = {"value": m.group(1), "data_type": DT_ACTUAL}

    # 入榨甘蔗
    m = re.search(r"入榨甘蔗[^\d]*?([\d,.]+)\s*(?:万吨|吨)", text)
    if m:
        val = m.group(1).replace(",", "")
        results["sugarcane_crushed"] = {"value": val, "data_type": DT_ACTUAL}

    # 产糖率
    m = re.search(r"产糖率[^\d]*?([\d,.]+)\s*%", text)
    if m:
        results["sugar_yield"] = {"value": m.group(1), "data_type": DT_ACTUAL}

    # 收榨糖厂数
    if "收榨" in text:
        m = re.search(r"(?:已有|累计已有|已有)\s*(\d+)\s*(?:家|间|座)\s*(?:糖厂|厂).*?收榨", text)
        if m:
            results["mills_closed"] = {"value": m.group(1), "data_type": DT_ACTUAL}
        elif "全部收榨" in text or "收榨完毕" in text:
            results["mills_closed"] = {"value": "全部", "data_type": DT_ACTUAL}
        # "生产结束" 也算收榨
        elif "生产结束" in text:
            m2 = re.search(r"(\d+)\s*(?:家|间|座)", text)
            if m2:
                results["mills_closed"] = {"value": m2.group(1), "data_type": DT_ACTUAL}

    # 同比对比文本
    comparison = ""
    m = re.search(r"同比[^\n。]{5,60}", text)
    if m:
        comparison = m.group(0).strip()

    # 榨季状态
    season_status = ""
    if "生产结束" in text or "纯销售期" in text:
        m = re.search(r"(\d{1,2}月\d{1,2}日).*?(?:生产结束|纯销售期)", text)
        if m:
            season_status = f"{m.group(1)}生产结束，进入纯销售期"
        else:
            season_status = "生产结束，进入纯销售期"

    if comparison:
        results["_comparison_text"] = {"value": comparison, "data_type": DT_ACTUAL}
    if season_status:
        results["_season_status"] = {"value": season_status, "data_type": DT_ACTUAL}

    return results


def _check_domestic_cache_freshness(target_date: str) -> dict[str, dict]:
    """
    检查CSV中国内数据的缓存新鲜度。
    返回 {region: {indicator_group: {"data_date": str, "status": str, "age_days": int}}}
    """
    freshness_cfg = config.get("freshness", {})
    max_ages = {
        "production": freshness_cfg.get("domestic_production_days", 60),
        "sales": freshness_cfg.get("domestic_sales_days", 45),
        "inventory": freshness_cfg.get("domestic_inventory_days", 45),
        "season_progress": freshness_cfg.get("domestic_season_progress_days", 45),
        "final_estimate": freshness_cfg.get("domestic_final_estimate_days", 90),
        "area": freshness_cfg.get("domestic_area_days", 90),
    }

    try:
        from update_data_csv import read_all_rows
        rows = read_all_rows()
    except ImportError:
        return {}

    now = datetime.strptime(target_date, "%Y-%m-%d")
    cache = {}

    for r in rows:
        if r.get("country") != "中国":
            continue
        if r.get("status") not in ("valid", "fresh", "valid_cached"):
            continue

        region = r.get("region", "")
        indicator = r.get("indicator", "")
        data_date = r.get("data_date", "")

        try:
            dd = datetime.strptime(data_date, "%Y-%m-%d")
            age = (now - dd).days
        except ValueError:
            continue

        # 分组
        if any(kw in indicator for kw in ["production", "产糖", "产混合糖"]):
            group = "production"
        elif any(kw in indicator for kw in ["sales", "销糖", "销量"]):
            group = "sales"
        elif any(kw in indicator for kw in ["inventory", "库存"]):
            group = "inventory"
        elif any(kw in indicator for kw in ["mills_closed", "收榨", "season_status"]):
            group = "season_progress"
        elif "final_estimate" in indicator or "预计最终" in indicator:
            group = "final_estimate"
        elif "area" in indicator or "面积" in indicator:
            group = "area"
        else:
            continue

        max_age = max_ages.get(group, 45)
        status = "valid_cached" if age <= max_age else "stale"

        key = f"{region}_{group}"
        if key not in cache or data_date > cache[key].get("data_date", ""):
            cache[key] = {
                "data_date": data_date,
                "status": status,
                "age_days": age,
                "max_age": max_age,
                "indicator": indicator,
                "region": region,
            }

    return cache


def fetch_china_msweet_production(target_date: str) -> list[dict]:
    """
    国内产销数据抓取主入口。
    优先级:
      1. 沐甜产销预估栏目（最新广西和云南文章，必须进入详情页）
      2. 沐甜各省产销、榨季追踪栏目
      3. 广西/云南糖业协会公开信息
      4. 泛糖科技公开资讯
      5. 公开微信公众号文章
      6. CSV中最近一次有效记录（valid_cached）

    不再使用"当天无新文章 = 无数据"的逻辑。
    """
    cfg = config.get("msweet", {})
    if not cfg.get("enabled", False):
        return []

    source_name = cfg.get("source_name", "沐甜科技")
    results = []
    seen_indicators = set()

    prod_kw = cfg.get("domestic_production_keywords", [
        "累计产糖", "产混合糖", "累计销糖", "工业库存", "产销率",
        "入榨甘蔗", "收榨", "开榨", "食糖库存", "最终产糖",
        "截至月底", "截至月末", "产糖量", "产糖", "销糖", "销售", "库存",
    ])

    # ── 收集所有栏目文章 ──
    all_articles = []
    columns = cfg.get("domestic_columns", [])

    for col in columns:
        col_url = col.get("url", "")
        col_name = col.get("name", "")
        scan_pages = int(col.get("scan_pages", 3))
        priority = col.get("priority", 99)

        if not col_url or not is_source_whitelisted("中国", col_url):
            logger.info("沐甜栏目跳过（不在白名单）: %s -> %s", col_name, col_url)
            continue

        logger.info("沐甜抓取栏目: %s (优先级%d, %s)", col_name, priority, col_url)

        for page_no in range(1, scan_pages + 1):
            list_url = _msweet_page_url(col_url, page_no)
            raw = http_get_text(list_url, timeout=20)
            if not raw:
                continue

            for item in _extract_msweet_list_items(raw, list_url):
                item["_column"] = col_name
                item["_priority"] = priority
                all_articles.append(item)

        logger.info("沐甜栏目 '%s': 找到 %d 篇文章", col_name, len(all_articles))

    # Fallback: 国内糖市
    general_url = cfg.get("domestic_url", "")
    if general_url and is_source_whitelisted("中国", general_url):
        raw = http_get_text(general_url, timeout=20)
        if raw:
            for item in _extract_msweet_list_items(raw, general_url):
                item["_column"] = "国内糖市"
                item["_priority"] = 99
                all_articles.append(item)

    logger.info("沐甜国内文章总数: %d", len(all_articles))

    # ── 分类和排序 ──
    production_articles = []
    area_articles = []
    weather_articles = []

    for item in all_articles:
        title = item.get("title", "")
        summary = item.get("summary", "")
        haystack = f"{title} {summary}"

        # 排除会议/活动文章（所有分类都检查）
        if _is_meeting_article(title, summary):
            logger.debug("排除会议/活动文章: %s", title[:60])
            continue

        has_production_kw = any(kw in haystack for kw in prod_kw)
        has_area_kw = any(kw in haystack for kw in cfg.get("domestic_area_keywords", []))
        has_weather_kw = any(kw in haystack for kw in cfg.get("domestic_weather_keywords", []))

        if has_production_kw:
            production_articles.append(item)
        elif has_area_kw:
            area_articles.append(item)
        elif has_weather_kw:
            weather_articles.append(item)

    # 按优先级和发布日期排序
    def _sort_key(item):
        pub = str(item.get("published_at", ""))[:10]
        try:
            pub_dt = datetime.strptime(pub, "%Y-%m-%d")
            if pub_dt.year < 2000:
                pub_dt = datetime(2000, 1, 1)
        except (ValueError, TypeError):
            pub_dt = datetime(2000, 1, 1)
        return (item.get("_priority", 99), -pub_dt.timestamp())

    production_articles.sort(key=_sort_key)

    # 按地区分组，取每个地区最新
    gx_articles = [a for a in production_articles if _classify_article_region(a.get("title", ""), a.get("summary", "")) == "广西"]
    yn_articles = [a for a in production_articles if _classify_article_region(a.get("title", ""), a.get("summary", "")) == "云南"]
    other_articles = [a for a in production_articles if _classify_article_region(a.get("title", ""), a.get("summary", "")) not in ("广西", "云南")]

    logger.info("生产文章分类: 广西=%d, 云南=%d, 其他=%d", len(gx_articles), len(yn_articles), len(other_articles))

    # ── 处理广西文章（进入详情页）──
    for item in gx_articles[:3]:
        title = item.get("title", "")
        summary = item.get("summary", "")
        url = item.get("url", "")
        pub = parse_date_anywhere(item.get("published_at", "") or summary, target_date)

        # 进入详情页
        detail_text = _fetch_article_detail(url)
        text = detail_text if detail_text else summary
        summary_only = detail_text is None

        numbers = _extract_enhanced_production_numbers(text)

        for indicator, info in numbers.items():
            if indicator.startswith("_"):
                continue
            key = f"广西_{indicator}"
            if key in seen_indicators:
                continue
            seen_indicators.add(key)

            value = info["value"]
            data_type = info["data_type"]
            unit = "万吨"
            if indicator in ("sales_ratio", "sugar_yield"):
                unit = "%"
            elif indicator in ("mills_closed", "mills_operating"):
                unit = "家"

            results.append({
                "country": "中国",
                "region": "广西",
                "indicator": indicator,
                "value": value,
                "value_or_fact": value,
                "unit": unit,
                "data_date": pub,
                "published_at": pub,
                "fetched_at": beijing_now().strftime("%Y-%m-%d %H:%M:%S"),
                "source_name": source_name,
                "source_url": url,
                "source_channel": "domestic_production_forecast",
                "data_type": data_type,
                "target_contract": TARGET_CONTRACT,
                "season": "2025/2026",
                "status": ST_FRESH,
                "notes": f"沐甜{item.get('_column', '')}栏目 | {'摘要' if summary_only else '详情'} | {title[:60]}",
            })

        # 保存赛季状态
        if "_season_status" in numbers:
            ss = numbers["_season_status"]
            ss_key = "广西_season_status"
            if ss_key not in seen_indicators:
                seen_indicators.add(ss_key)
                results.append({
                    "country": "中国",
                    "region": "广西",
                    "indicator": "season_status",
                    "value": "",
                    "text_value": ss["value"],
                    "value_or_fact": ss["value"],
                    "unit": "文本",
                    "data_date": pub,
                    "published_at": pub,
                    "fetched_at": beijing_now().strftime("%Y-%m-%d %H:%M:%S"),
                    "source_name": source_name,
                    "source_url": url,
                    "source_channel": "domestic_production_forecast",
                    "data_type": ss["data_type"],
                    "target_contract": TARGET_CONTRACT,
                    "season": "2025/2026",
                    "status": ST_FRESH,
                    "notes": f"沐甜{item.get('_column', '')}栏目 | 榨季状态",
                })

        # 保存对比文本
        if "_comparison_text" in numbers:
            ct = numbers["_comparison_text"]
            ct_key = "广西_comparison"
            if ct_key not in seen_indicators:
                seen_indicators.add(ct_key)
                results.append({
                    "country": "中国",
                    "region": "广西",
                    "indicator": "comparison_text",
                    "value": "",
                    "text_value": ct["value"],
                    "value_or_fact": ct["value"],
                    "unit": "文本",
                    "data_date": pub,
                    "published_at": pub,
                    "fetched_at": beijing_now().strftime("%Y-%m-%d %H:%M:%S"),
                    "source_name": source_name,
                    "source_url": url,
                    "data_type": DT_ACTUAL,
                    "target_contract": TARGET_CONTRACT,
                    "season": "2025/2026",
                    "status": ST_FRESH,
                    "notes": f"沐甜{item.get('_column', '')}栏目 | 同比对比",
                })

        # 如果没有提取到数字但有生产关键词，保存文本记录
        if not any(k for k in numbers if not k.startswith("_")):
            text_key = "广西_text"
            if text_key not in seen_indicators:
                seen_indicators.add(text_key)
                results.append({
                    "country": "中国",
                    "region": "广西",
                    "indicator": "沐甜广西生产资讯",
                    "value": "",
                    "value_or_fact": f"{title}。{text[:200]}",
                    "unit": "文本",
                    "data_date": pub,
                    "published_at": pub,
                    "fetched_at": beijing_now().strftime("%Y-%m-%d %H:%M:%S"),
                    "source_name": source_name,
                    "source_url": url,
                    "data_type": DT_ACTUAL,
                    "target_contract": TARGET_CONTRACT,
                    "season": "2025/2026",
                    "status": ST_FRESH,
                    "notes": f"沐甜{item.get('_column', '')}栏目 | 含生产关键词但未提取到数值 | {title[:60]}",
                })

    # ── 处理云南文章（进入详情页）──
    for item in yn_articles[:3]:
        title = item.get("title", "")
        summary = item.get("summary", "")
        url = item.get("url", "")
        pub = parse_date_anywhere(item.get("published_at", "") or summary, target_date)

        detail_text = _fetch_article_detail(url)
        text = detail_text if detail_text else summary
        summary_only = detail_text is None

        numbers = _extract_enhanced_production_numbers(text)

        for indicator, info in numbers.items():
            if indicator.startswith("_"):
                continue
            key = f"云南_{indicator}"
            if key in seen_indicators:
                continue
            seen_indicators.add(key)

            value = info["value"]
            data_type = info["data_type"]
            unit = "万吨"
            if indicator in ("sales_ratio", "sugar_yield"):
                unit = "%"
            elif indicator in ("mills_closed", "mills_operating"):
                unit = "家"

            results.append({
                "country": "中国",
                "region": "云南",
                "indicator": indicator,
                "value": value,
                "value_or_fact": value,
                "unit": unit,
                "data_date": pub,
                "published_at": pub,
                "fetched_at": beijing_now().strftime("%Y-%m-%d %H:%M:%S"),
                "source_name": source_name,
                "source_url": url,
                "source_channel": "domestic_production",
                "data_type": data_type,
                "target_contract": TARGET_CONTRACT,
                "season": "2025/2026",
                "status": ST_FRESH,
                "notes": f"沐甜{item.get('_column', '')}栏目 | {'摘要' if summary_only else '详情'} | {title[:60]}",
            })

        # 保存对比文本
        if "_comparison_text" in numbers:
            ct = numbers["_comparison_text"]
            ct_key = "云南_comparison"
            if ct_key not in seen_indicators:
                seen_indicators.add(ct_key)
                results.append({
                    "country": "中国",
                    "region": "云南",
                    "indicator": "comparison_text",
                    "value": "",
                    "text_value": ct["value"],
                    "value_or_fact": ct["value"],
                    "unit": "文本",
                    "data_date": pub,
                    "published_at": pub,
                    "fetched_at": beijing_now().strftime("%Y-%m-%d %H:%M:%S"),
                    "source_name": source_name,
                    "source_url": url,
                    "data_type": DT_ACTUAL,
                    "target_contract": TARGET_CONTRACT,
                    "season": "2025/2026",
                    "status": ST_FRESH,
                    "notes": f"沐甜{item.get('_column', '')}栏目 | 同比对比",
                })

        if not any(k for k in numbers if not k.startswith("_")):
            text_key = "云南_text"
            if text_key not in seen_indicators:
                seen_indicators.add(text_key)
                results.append({
                    "country": "中国",
                    "region": "云南",
                    "indicator": "沐甜云南生产资讯",
                    "value": "",
                    "value_or_fact": f"{title}。{text[:200]}",
                    "unit": "文本",
                    "data_date": pub,
                    "published_at": pub,
                    "fetched_at": beijing_now().strftime("%Y-%m-%d %H:%M:%S"),
                    "source_name": source_name,
                    "source_url": url,
                    "data_type": DT_ACTUAL,
                    "target_contract": TARGET_CONTRACT,
                    "season": "2025/2026",
                    "status": ST_FRESH,
                    "notes": f"沐甜{item.get('_column', '')}栏目 | 含生产关键词但未提取到数值",
                })

    # ── 处理其他地区文章 ──
    for item in other_articles[:5]:
        title = item.get("title", "")
        summary = item.get("summary", "")
        url = item.get("url", "")
        pub = parse_date_anywhere(item.get("published_at", "") or summary, target_date)
        text = f"{title}。{summary}"

        numbers = _extract_enhanced_production_numbers(text)
        region = _detect_region(text)

        for indicator, info in numbers.items():
            if indicator.startswith("_"):
                continue
            key = f"{region}_{indicator}"
            if key in seen_indicators:
                continue
            seen_indicators.add(key)

            value = info["value"]
            data_type = info["data_type"]
            unit = "万吨"
            if indicator in ("sales_ratio", "sugar_yield"):
                unit = "%"
            elif indicator in ("mills_closed", "mills_operating"):
                unit = "家"

            results.append({
                "country": "中国",
                "region": region or "全国",
                "indicator": indicator,
                "value": value,
                "value_or_fact": value,
                "unit": unit,
                "data_date": pub,
                "published_at": pub,
                "fetched_at": beijing_now().strftime("%Y-%m-%d %H:%M:%S"),
                "source_name": source_name,
                "source_url": url,
                "source_channel": "domestic_production",
                "data_type": data_type,
                "target_contract": TARGET_CONTRACT,
                "season": "2025/2026",
                "status": ST_FRESH,
                "notes": f"沐甜{item.get('_column', '')}栏目 | {title[:60]}",
            })

    # ── 处理26/27种植面积文章 ──
    for item in area_articles[:5]:
        title = item.get("title", "")
        summary = item.get("summary", "")
        url = item.get("url", "")
        pub = parse_date_anywhere(item.get("published_at", "") or summary, target_date)

        detail_text = _fetch_article_detail(url)
        text = detail_text if detail_text else f"{title}。{summary}"

        region = _classify_article_region(title, summary) or _detect_region(text)
        area_key = f"{region}_area"
        if area_key not in seen_indicators:
            seen_indicators.add(area_key)

            # 判断数据类型
            if any(kw in text for kw in ["调研", "走访", "估计", "估算"]):
                data_type = "survey_estimate"
            elif any(kw in text for kw in ["目标", "计划", "力争"]):
                data_type = "target"
            elif any(kw in text for kw in ["实际", "完成", "确认"]):
                data_type = DT_ACTUAL
            else:
                data_type = DT_FORECAST

            results.append({
                "country": "中国",
                "region": region or "广西",
                "indicator": "26/27种植面积",
                "value": "",
                "value_or_fact": text[:300],
                "unit": "文本",
                "data_date": pub,
                "published_at": pub,
                "fetched_at": beijing_now().strftime("%Y-%m-%d %H:%M:%S"),
                "source_name": source_name,
                "source_url": url,
                "source_channel": "domestic_area",
                "data_type": data_type,
                "target_contract": TARGET_CONTRACT,
                "season": "2026/2027",
                "status": ST_FRESH,
                "notes": f"沐甜{item.get('_column', '')}栏目 | 26/27种植面积 | {title[:60]}",
            })

    # ── 处理天气文章 ──
    for item in weather_articles[:3]:
        title = item.get("title", "")
        summary = item.get("summary", "")
        url = item.get("url", "")
        pub = parse_date_anywhere(item.get("published_at", "") or summary, target_date)
        text = f"{title}。{summary}"
        region = _detect_region(text)

        weather_key = f"{region}_weather"
        if weather_key not in seen_indicators:
            seen_indicators.add(weather_key)
            results.append({
                "country": "中国",
                "region": region or "广西",
                "indicator": "天气/苗情",
                "value": "",
                "value_or_fact": text[:300],
                "unit": "文本",
                "data_date": pub,
                "published_at": pub,
                "fetched_at": beijing_now().strftime("%Y-%m-%d %H:%M:%S"),
                "source_name": source_name,
                "source_url": url,
                "source_channel": "domestic_weather",
                "data_type": DT_WEATHER,
                "target_contract": TARGET_CONTRACT,
                "season": "2026/2027",
                "status": ST_FRESH,
                "notes": f"沐甜{item.get('_column', '')}栏目 | 天气/苗情",
            })

    # ── 缓存兜底：如果某地区无新鲜数据，使用最近有效缓存 ──
    cache_freshness = _check_domestic_cache_freshness(target_date)
    logger.info("国内缓存新鲜度检查: %d 个分组", len(cache_freshness))

    for key, info in cache_freshness.items():
        if info["status"] == "stale":
            continue  # 过期缓存不用

        region = info["region"]
        group = key.replace(f"{region}_", "")

        # 检查是否已有该地区的该类数据
        existing_key = f"{region}_{group}"
        if existing_key in seen_indicators:
            continue

        # 只有在没有新鲜数据时才使用缓存
        has_fresh = any(
            r.get("region") == region and
            any(kw in r.get("indicator", "") for kw in _group_keywords(group))
            for r in results
            if r.get("status") == ST_FRESH
        )
        if has_fresh:
            continue

        seen_indicators.add(existing_key)
        results.append({
            "country": "中国",
            "region": region,
            "indicator": info["indicator"],
            "value": "",
            "value_or_fact": f"沿用最近一期数据（{info['data_date']}，有效期{info['max_age']}天，已用{info['age_days']}天）",
            "unit": "文本",
            "data_date": info["data_date"],
            "published_at": info["data_date"],
            "fetched_at": beijing_now().strftime("%Y-%m-%d %H:%M:%S"),
            "source_name": "CSV缓存",
            "source_url": "",
            "source_channel": "cached",
            "data_type": DT_ACTUAL,
            "target_contract": TARGET_CONTRACT,
            "season": "2025/2026",
            "status": ST_CACHED,
            "notes": f"沐甜无当日更新，使用CSV中未过期缓存 | 数据日期: {info['data_date']} | 已用{info['age_days']}/{info['max_age']}天",
        })

    logger.info("沐甜国内抓取完成: %d 条记录", len(results))
    return results


def _group_keywords(group: str) -> list[str]:
    """返回指标分组对应的关键词。"""
    mapping = {
        "production": ["production", "产糖", "产混合糖", "最终产糖"],
        "sales": ["sales", "销糖", "销量"],
        "inventory": ["inventory", "库存"],
        "season_progress": ["mills_closed", "收榨", "season_status", "纯销售期"],
        "final_estimate": ["final_estimate", "预计最终"],
        "area": ["area", "面积"],
    }
    return mapping.get(group, [])


# ============================================================
# 数据校验
# ============================================================

def validate_record(rec: dict, prev_records: list[dict]) -> list[str]:
    """校验单条记录，返回问题列表。"""
    issues = []

    # 数据日期不能晚于发布日期
    data_date = rec.get("data_date", "")
    published = rec.get("published_at", "")
    if data_date and published:
        try:
            dd = datetime.strptime(data_date, "%Y-%m-%d")
            pd = datetime.strptime(published, "%Y-%m-%d")
            if dd > pd:
                issues.append(f"数据日期 {data_date} 晚于发布日期 {published}")
        except ValueError:
            pass

    # data_type 校验
    dt = rec.get("data_type", "")
    if dt not in (DT_ACTUAL, DT_FORECAST, DT_POLICY, DT_WEATHER, DT_MARKET):
        issues.append(f"未知 data_type: {dt}")

    # 预测值不能标记为 actual
    indicator = rec.get("indicator", "").lower()
    val = str(rec.get("value_or_fact", "")).lower()
    if dt == DT_ACTUAL and any(w in indicator + val for w in ["预测", "预估", "forecast", "expected"]):
        issues.append("预测值标记为 actual")

    # 制糖比范围检查
    if "制糖比" in rec.get("indicator", ""):
        try:
            v = float(re.findall(r"[\d.]+", str(rec.get("value_or_fact", "")))[0])
            if not (0 <= v <= 100):
                issues.append(f"制糖比 {v}% 超出有效范围 0-100%")
        except (ValueError, IndexError):
            pass

    # 异常变动检查
    thresholds = config.get("anomaly_thresholds", {})
    for prev in prev_records:
        if (prev.get("indicator") == rec.get("indicator") and
            prev.get("country") == rec.get("country")):
            try:
                old_vals = re.findall(r"[\d.]+", str(prev.get("value_or_fact", "")))
                new_vals = re.findall(r"[\d.]+", str(rec.get("value_or_fact", "")))
                if old_vals and new_vals:
                    old = float(old_vals[0])
                    new = float(new_vals[0])
                    if old != 0:
                        chg = abs(new - old) / abs(old) * 100
                        if "压榨" in rec.get("indicator", "") and chg > thresholds.get("crush_change_pct", 20):
                            issues.append(f"压榨量环比变动 {chg:.1f}% 超过阈值")
                            rec["status"] = ST_NEEDS_VER
                        if "产量" in rec.get("indicator", "") and chg > thresholds.get("production_change_pct", 10):
                            issues.append(f"产量预估变动 {chg:.1f}% 超过阈值")
                            rec["status"] = ST_NEEDS_VER
            except (ValueError, IndexError):
                pass
            break

    return issues


# ============================================================
# 缓存合并
# ============================================================

def merge_into_cache(new_records: list[dict], target_date: str) -> int:
    """
    合并到缓存：去重 → 校验 → 时效更新。
    返回通过校验的记录数。
    """
    cache = load_cache()
    existing = cache.get("records", [])

    # 构建去重索引: country|indicator|data_date|source_name
    seen_idx = {}
    for i, rec in enumerate(existing):
        key = f"{rec.get('country','')}|{rec.get('indicator','')}|{rec.get('data_date','')}|{rec.get('source_name','')}"
        seen_idx[key] = i

    now = beijing_now()
    merged = 0

    for rec in new_records:
        key = f"{rec.get('country','')}|{rec.get('indicator','')}|{rec.get('data_date','')}|{rec.get('source_name','')}"

        # 校验
        issues = validate_record(rec, existing)
        if issues:
            logger.warning("校验 %s: %s", key[:80], "; ".join(issues))

        if key in seen_idx:
            existing[seen_idx[key]].update(rec)
            existing[seen_idx[key]]["status"] = ST_FRESH
        else:
            existing.append(rec)
            seen_idx[key] = len(existing) - 1

        merged += 1

    # 更新所有记录时效
    for rec in existing:
        update_record_status(rec, now)

        # 已过期的记录如果之前是 fresh/cached → stale
        data_date = rec.get("data_date", "")
        try:
            dd = datetime.strptime(data_date, "%Y-%m-%d")
            max_age = get_freshness_days(rec.get("data_type", DT_ACTUAL))
            if (now.replace(tzinfo=None) - dd).days > max_age * 2:
                if rec.get("status") in (ST_FRESH, ST_UNCHANGED, ST_CACHED):
                    rec["status"] = ST_STALE
        except ValueError:
            pass

    cache["records"] = existing
    save_cache(cache)
    logger.info("缓存: %d 条 (本次合并 %d 条)", len(existing), merged)
    return merged


def get_valid_records(target_contract: str | None = None, countries: list[str] | None = None) -> list[dict]:
    """获取有效记录（fresh + unchanged + valid_cached），过滤目标合约。"""
    cache = load_cache()
    records = cache.get("records", [])
    valid = []
    for r in records:
        st = r.get("status", "")
        if st not in (ST_FRESH, ST_UNCHANGED, ST_CACHED):
            continue
        if target_contract and r.get("target_contract") != target_contract:
            continue
        if countries and r.get("country") not in countries:
            continue
        valid.append(r)
    return valid


# ============================================================
# 按国家统计
# ============================================================

def fetch_status_by_country() -> dict[str, dict]:
    """返回每个国家的CSV台账状态。"""
    try:
        from update_data_csv import read_all_rows
        records = read_all_rows()
    except ImportError:
        records = []
    status = {}
    for c in ALL_COUNTRIES:
        status[c] = {"fresh": 0, "cached": 0, "stale": 0, "failed": 0, "total": 0}

    for r in records:
        c = r.get("country", "")
        if c not in status:
            continue
        st = r.get("status", "")
        status[c]["total"] += 1
        if st in (ST_FRESH, "valid"):
            status[c]["fresh"] += 1
        elif st in (ST_UNCHANGED, ST_CACHED, "needs_verification", "no_new_data", "not_published", "source_503"):
            status[c]["cached"] += 1
        elif st in (ST_STALE, "superseded"):
            status[c]["stale"] += 1
        elif st in (ST_FAILED, ST_CONFLICT, "conflict"):
            status[c]["failed"] += 1

    return status


# ============================================================
# 主入口
# ============================================================

def run(target_date: str | None = None) -> dict:
    """
    执行全部抓取，返回结果摘要。
    """
    if target_date is None:
        target_date = beijing_now().strftime("%Y-%m-%d")

    logger.info("=" * 50)
    logger.info("基本面自动抓取 - %s | 目标合约: %s", target_date, TARGET_CONTRACT)
    logger.info("=" * 50)

    summary = {"fetched": 0, "merged": 0, "errors": [], "by_country": {}}
    all_new = []

    # 0. 全球供需机构观点 — 沐甜国际机构观点栏目
    logger.info("--- 全球供需机构观点 ---")
    gsd = fetch_global_supply_demand(target_date)
    all_new.extend(gsd)
    summary["fetched"] += len(gsd)

    # 1. UNICA — 巴西双周报告
    logger.info("--- UNICA 巴西双周报告 ---")
    unica = fetch_unica_biweekly(target_date)
    all_new.extend(unica)
    summary["fetched"] += len(unica)
    unica_date = next((r.get("data_date") for r in unica if r.get("data_date")), "")
    if unica_date:
        for deprecated in [
            "UNICA双周报告",
            "中南部糖产量累计",
            "中南部糖产量累计同比",
            "中南部制糖比累计",
            "中南部制乙醇比累计",
            "中南部糖产量双周",
            "中南部糖产量双周同比",
            "中南部制糖比双周",
            "中南部制乙醇比双周",
        ]:
            all_new.append({
                "country": "巴西",
                "indicator": deprecated,
                "value_or_fact": "",
                "unit": "",
                "data_date": unica_date,
                "published_at": target_date,
                "fetched_at": beijing_now().strftime("%Y-%m-%d %H:%M:%S"),
                "source_name": "UNICA",
                "source_url": config["unica"]["url"],
                "data_type": DT_ACTUAL,
                "target_contract": TARGET_CONTRACT,
                "season": "2026/2027",
                "status": "superseded",
                "notes": "已由UNICA Table 2定向指标替代",
            })

    # 2. 印度 NFCSF/coopsugar
    logger.info("--- 印度 NFCSF/coopsugar ---")
    india = fetch_india_coopsugar_production(target_date)
    all_new.extend(india)
    summary["fetched"] += len(india)

    # 3. 泰国 OCSB
    logger.info("--- 泰国 OCSB ---")
    thailand = fetch_thailand_ocsb_production(target_date)
    all_new.extend(thailand)
    summary["fetched"] += len(thailand)

    # 4. 中国/广西 沐甜国内
    logger.info("--- 中国/广西 沐甜国内 ---")
    china = fetch_china_msweet_production(target_date)
    all_new.extend(china)
    summary["fetched"] += len(china)

    # 旧资讯源退出有效输入
    try:
        from update_data_csv import read_all_rows
        for old in read_all_rows():
            old_indicator = old.get("indicator", "")
            old_country = old.get("country", "")
            is_old_international_news = old_indicator in (
                "沐甜国际资讯-印度", "沐甜国际资讯-泰国", "泛糖国际资讯-印度", "泛糖国际资讯-泰国"
            )
            is_old_ocsb_thailand = (
                old_country == "泰国"
                and old_indicator == "OCSB糖产量"
                and "ocsb.go.th" in old.get("source_url", "")
            )
            if not (is_old_international_news or is_old_ocsb_thailand):
                continue
            all_new.append({
                "country": old_country,
                "indicator": old_indicator,
                "value_or_fact": old.get("text_value", ""),
                "unit": old.get("unit", "文本"),
                "data_date": old.get("data_date", target_date),
                "published_at": old.get("published_at", old.get("data_date", target_date)),
                "fetched_at": beijing_now().strftime("%Y-%m-%d %H:%M:%S"),
                "source_name": old.get("source_name", ""),
                "source_url": old.get("source_url", ""),
                "data_type": old.get("data_type", DT_ACTUAL),
                "target_contract": TARGET_CONTRACT,
                "season": old.get("season", ""),
                "status": "superseded",
                "source_channel": old.get("source_channel", ""),
                "source_status": "superseded",
                "notes": "已由指定结构化来源替代",
            })
    except ImportError:
        pass

    # 写入CSV台账
    if all_new:
        try:
            from update_data_csv import csv_row, upsert_rows
            rows = []
            for r in all_new:
                rows.append(csv_row(
                    category="international" if r.get("country") in ("巴西", "印度", "泰国") else
                             "domestic" if r.get("country") == "中国" else "macro",
                    country=r.get("country", ""),
                    region=r.get("region", ""),
                    indicator=r.get("indicator", ""),
                    value=str(r.get("value", "")) if r.get("value") else "",
                    text_value=(str(r.get("text_value", "") or r.get("value_or_fact", ""))[:300]
                                if not r.get("value") else ""),
                    unit=r.get("unit", ""),
                    data_date=r.get("data_date", ""),
                    published_at=r.get("published_at", r.get("fetched_at", "")),
                    source_name=r.get("source_name", ""),
                    source_url=r.get("source_url", ""),
                    source_status=r.get("source_status", r.get("status", "")),
                    source_channel=r.get("source_channel", ""),
                    official_level=r.get("official_level", ""),
                    data_type=r.get("data_type", "weather"),
                    season=r.get("season", ""),
                    status=r.get("status", ""),
                ))
            ins, upd, _ = upsert_rows(rows)
            summary["merged"] = ins + upd
        except ImportError:
            summary["merged"] = 0

    # 统计
    summary["by_country"] = fetch_status_by_country()
    for c, s in summary["by_country"].items():
        logger.info("%s: total=%d fresh=%d cached=%d stale=%d",
                    c, s["total"], s["fresh"], s["cached"], s["stale"])

    return summary


def main():
    parser = argparse.ArgumentParser(description="基本面公开数据抓取")
    parser.add_argument("--date", type=str, default=None, help="目标日期")
    args = parser.parse_args()

    s = run(args.date)
    print(f"\n抓取: {s['fetched']} 条, 合并: {s['merged']} 条")
    print("\n按国家:")
    for c, st in s["by_country"].items():
        print(f"  {c}: fresh={st['fresh']} cached={st['cached']} stale={st['stale']} total={st['total']}")


if __name__ == "__main__":
    main()
