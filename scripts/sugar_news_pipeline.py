from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from copy import copy
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import quote_plus
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from openpyxl import load_workbook
from openpyxl.styles import Alignment


PROJECT_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = PROJECT_ROOT.parent
DEFAULT_TASK_ROOT = WORKSPACE_ROOT / "Sugar News"
PUBLIC_ROOT = PROJECT_ROOT / "public" / "sugar-news"
PUBLIC_DATA_ROOT = PUBLIC_ROOT / "data"
try:
    SHANGHAI = ZoneInfo("Asia/Shanghai")
except Exception:
    SHANGHAI = timezone(timedelta(hours=8), name="Asia/Shanghai")
GROUP_ORDER = {"巴西": 0, "印度": 1, "泰国": 2, "中国": 3, "其他国家": 4}
IMPACT_PREFIXES = ("偏多糖价：", "偏空糖价：", "中性：", "影响有限：")
PLACEHOLDERS = (
    "暂无新闻",
    "暂无最新数据",
    "暂无最新对比数据",
    "暂无可比数据",
    "暂无最新",
    "暂未更新",
    "数据尚未公布",
)
INDIA_MAIN_CANE_REGIONS = (
    "北方邦", "Uttar Pradesh", "UP",
    "马哈拉施特拉邦", "Maharashtra",
    "卡纳塔克邦", "Karnataka",
    "泰米尔纳德邦", "Tamil Nadu",
    "古吉拉特邦", "Gujarat",
    "比哈尔邦", "Bihar",
    "旁遮普邦", "Punjab",
    "哈里亚纳邦", "Haryana",
    "北阿坎德邦", "Uttarakhand",
)
INDIA_WEATHER_TERMS = (
    "降雨", "雨", "季风", "天气", "气象", "预警", "强降雨", "暴雨", "干旱", "洪涝", "积水",
    "rain", "rainfall", "monsoon", "weather", "alert", "warning", "heavy rain", "flood", "drought",
)
INDIA_RAIN_BENEFIT_TERMS = (
    "适量降雨", "降雨增加", "降雨增多", "未来降雨", "强降雨", "暴雨预报", "墒情改善",
    "季风活跃", "季风增强", "widespread rainfall", "heavy rainfall", "rainfall forecast",
    "monsoon revival", "active monsoon",
)
INDIA_DROUGHT_TERMS = (
    "干旱", "降雨不足", "季风偏弱", "降雨减少", "雨量不足", "deficient rainfall",
    "weak monsoon", "dry spell", "rainfall deficit",
)
INDIA_DAMAGE_TERMS = (
    "已造成", "洪涝", "农田被淹", "甘蔗倒伏", "道路中断", "作物受损", "预计减产",
    "受灾", "损失", "flood damage", "crop damage", "waterlogging", "lodging", "road disruption",
)
INDIA_HARVEST_TERMS = ("收割", "压榨", "运输", "入榨", "开榨", "砍蔗", "harvest", "crushing", "transport")
INDIA_PRICE_INVENTORY_SEARCH_TEMPLATES = (
    ("en", "India sugar S-grade M-grade domestic price {readable}"),
    ("en", "India sugar prices today S grade M grade {readable}"),
    ("en", "Maharashtra Uttar Pradesh Karnataka sugar price {readable}"),
    ("en", "Uttar Pradesh sugar ex-mill price {readable}"),
    ("en", "UP sugar ex-mill rate {readable}"),
    ("en", "Uttar Pradesh mill sugar price {readable}"),
    ("en", "M-grade sugar ex-mill Uttar Pradesh {readable}"),
    ("en", "North India sugar ex-mill price {readable}"),
    ("hi", "मुजफ्फरनगर चीनी मिल भाव {day} जुलाई {year}"),
    ("hi", "उत्तर प्रदेश चीनी एक्स मिल कीमत {day} जुलाई {year}"),
    ("en", "India sugar carryover stock ending stock {readable}"),
    ("en", "India sugar closing stock ISMA NFCSF {readable}"),
    ("en", "India sugar ending stocks consumption ratio {readable}"),
)
INDIA_PRICE_INVENTORY_SOURCE_GUIDE = (
    "ChiniMandi domestic sugar prices",
    "ISMA / Indian Sugar & Bio-energy Manufacturers Association",
    "NFCSF / National Federation of Cooperative Sugar Factories",
    "Department of Food and Public Distribution, Government of India",
    "reliable commodity and agriculture media with dated market quotes",
)
DATE_FORMAT_EXAMPLES = (
    "July 19, 2026",
    "19 July 2026",
    "19/07/2026",
    "2026-07-19",
    "19 de julho de 2026",
    "19 जुलाई 2026",
    "19 กรกฎาคม 2569",
    "19 جولائی 2026",
    "Hulyo 19 2026",
    "ngày 19 tháng 7 năm 2026",
    "19 июля 2026",
    "19 Juli 2026",
)
THAI_MAIN_CANE_PROVINCES = (
    "\u4e4c\u9686\u4ed6\u5c3c", "Udon Thani",
    "\u5b54\u656c", "Khon Kaen",
    "\u5475\u53fb", "\u90a3\u7a7a\u53fb\u5dee\u662f\u739b", "Nakhon Ratchasima",
    "\u731c\u4e5f\u84ec", "Chaiyaphum",
    "\u52a0\u62c9\u4fe1", "Kalasin",
    "\u9ece\u5e9c", "Loei",
    "\u90a3\u7a7a\u6c99\u65fa", "Nakhon Sawan",
    "\u7518\u70f9\u78a7", "Kamphaeng Phet",
    "\u7d20\u53ef\u6cf0", "Sukhothai",
    "\u5f6d\u4e16\u6d1b", "Phitsanulok",
    "\u5317\u78a7", "Kanchanaburi",
    "\u534e\u5bcc\u91cc", "Lopburi",
    "\u7d20\u6500\u6b66\u91cc", "Suphanburi",
    "\u731c\u7eb3", "Chai Nat",
    "\u6c99\u7f34", "Sa Kaeo",
    "\u6625\u6b66\u91cc", "Chonburi",
)
THAI_WEATHER_TERMS = (
    "\u5929\u6c14", "\u6c14\u8c61", "\u964d\u96e8", "\u96e8", "\u96f7\u9635\u96e8",
    "\u5e72\u65f1", "\u6d2a\u6d9d", "\u79ef\u6c34", "rain", "rainfall", "thunderstorm", "flood", "drought",
)
THAI_RAIN_INCREASE_TERMS = (
    "\u964d\u96e8\u589e\u52a0", "\u964d\u96e8\u5c06\u589e\u52a0", "\u96e8\u91cf\u589e\u52a0",
    "\u964d\u96e8\u589e\u591a", "\u964d\u96e8\u660e\u663e\u589e\u591a", "\u964d\u96e8\u6539\u5584",
    "\u5892\u60c5", "\u6709\u5229\u4e8e\u6539\u5584", "\u5f3a\u964d\u96e8",
    "\u5f3a\u5230\u5f88\u5f3a\u964d\u96e8", "\u66b4\u96e8\u9884\u8b66", "\u964d\u96e8\u8303\u56f4",
    "\u964d\u96e8\u5f3a\u5ea6",
)
THAI_LOW_COVERAGE_TERMS = (
    "20%", "\u7ea620%", "\u8f83\u5206\u6563", "\u5206\u6563", "\u8986\u76d6\u7387\u8f83\u4f4e",
    "\u8986\u76d6\u7387\u4f4e", "\u5c40\u5730", "\u5c11\u91cf",
)
THAI_DAMAGE_TERMS = (
    "\u5df2\u9020\u6210", "\u9020\u6210\u4e25\u91cd\u6d2a\u6d9d", "\u4e25\u91cd\u6d2a\u6d9d",
    "\u7518\u8517\u5012\u4f0f", "\u519c\u7530\u88ab\u6df9", "\u4f5c\u7269\u53d7\u635f",
    "\u9884\u8ba1\u51cf\u4ea7", "\u53d7\u707e", "\u635f\u5931", "\u6839\u7cfb\u53d7\u635f",
)
THAI_DROUGHT_TERMS = ("\u964d\u96e8\u51cf\u5c11", "\u964d\u96e8\u4e0d\u8db3", "\u6301\u7eed\u5e72\u65f1", "\u5e72\u65f1", "\u504f\u5e72")
THAI_HARVEST_TERMS = ("\u6536\u5272", "\u538b\u69a8", "\u8fd0\u8f93", "\u5165\u69a8", "\u5f00\u69a8", "\u6536\u69a8")
THAI_WEATHER_EVENT_TERMS = (
    "\u964d\u96e8", "\u96e8\u91cf", "\u96f7\u9635\u96e8", "\u5e72\u65f1", "\u6d2a\u6d9d", "\u79ef\u6c34",
    "rain", "rainfall", "thunderstorm", "flood", "drought",
)
GLOBAL_SEARCH_TEMPLATES = (
    "global sugar industry news {readable}",
    "sugar production export policy {readable}",
    "sugarcane ethanol mills {readable}",
    "sugar import export tariff quota {readable}",
    "sugar price government policy {readable}",
    "sugar industry news {day} {month_name} {year}",
    "sugarcane news {day} {month_name} {year}",
    "ethanol sugar mills {day} {month_name} {year}",
)
COUNTRY_SEARCH_TEMPLATES = {
    "巴西": (
        ("en", "Brazil sugar industry news {readable}"),
        ("en", "Brazil sugarcane ethanol export {readable}"),
        ("pt-BR", "Brasil açúcar etanol {day} julho {year}"),
        ("pt-BR", "Brasil setor sucroenergético {day} de julho de {year}"),
        ("pt-BR", "usinas cana açúcar etanol {date_slash}"),
    ),
    "印度": (
        ("en", "India sugar industry news {readable}"),
        ("en", "India sugarcane ethanol mills {readable}"),
        ("en", "India sugar news {day} {month_name} {year}"),
        ("en", "India sugarcane rainfall {readable}"),
        ("en", "India sugar belt rainfall forecast {readable}"),
        ("en", "Uttar Pradesh sugarcane rain forecast {readable}"),
        ("en", "Maharashtra sugarcane rainfall {readable}"),
        ("en", "Karnataka sugarcane rainfall {readable}"),
        ("en", "India monsoon sugar production {readable}"),
        ("en", "IMD rainfall forecast sugarcane states {readable}"),
        ("en", "heavy rainfall sugarcane India {readable}"),
        ("en", "excess rainfall cane crop India {readable}"),
        ("en", "deficient rainfall sugarcane India {readable}"),
        ("hi", "भारत चीनी उद्योग {day} जुलाई {year}"),
        ("hi", "गन्ना चीनी मिल इथेनॉल {day} जुलाई {year}"),
        ("hi", "उत्तर प्रदेश गन्ना बारिश {day} जुलाई {year}"),
        ("hi", "महाराष्ट्र गन्ना बारिश {day} जुलाई {year}"),
        ("hi", "कर्नाटक गन्ना बारिश {day} जुलाई {year}"),
    ),
    "泰国": (
        ("en", "Thailand sugar industry news {readable}"),
        ("en", "Thailand sugarcane mills ethanol {readable}"),
        ("en", "Thailand sugar news {day} {month_name} {year}"),
        ("th", "ประเทศไทย น้ำตาล อ้อย {day} กรกฎาคม {buddhist_year}"),
        ("th", "ข่าวอ้อย น้ำตาล {day} กรกฎาคม {buddhist_year}"),
        ("th", "อุตสาหกรรมอ้อยและน้ำตาล {day} กรกฎาคม {buddhist_year}"),
        ("th", "โรงงานน้ำตาล เอทานอล {day} กรกฎาคม {buddhist_year}"),
    ),
    "中国": (
        ("zh-CN", "中国糖业新闻 {year}年{month}月{day}日"),
        ("zh-CN", "中国食糖 {year}年{month}月{day}日"),
        ("zh-CN", "中国白糖 {year}年{month}月{day}日"),
        ("zh-CN", "中国甘蔗 {year}年{month}月{day}日"),
        ("zh-CN", "中国甜菜糖 {year}年{month}月{day}日"),
        ("zh-CN", "食糖产销数据 {year}年{month}月{day}日"),
        ("zh-CN", "食糖进口 {year}年{month}月{day}日"),
        ("zh-CN", "食糖进口配额 {year}年{month}月{day}日"),
        ("zh-CN", "糖浆预混粉进口 {year}年{month}月{day}日"),
        ("zh-CN", "广西糖业 {year}年{month}月{day}日"),
        ("zh-CN", "云南糖业 {year}年{month}月{day}日"),
        ("zh-CN", "郑州白糖期货 {year}年{month}月{day}日"),
        ("zh-CN", "郑糖主力合约 {year}年{month}月{day}日"),
        ("zh-CN", "白糖现货价格 {year}年{month}月{day}日"),
        ("zh-CN", "制糖集团公告 {year}年{month}月{day}日"),
        ("en", "China sugar industry {readable}"),
        ("en", "China sugar production {readable}"),
        ("en", "China sugar imports {readable}"),
        ("en", "China sugarcane beet sugar {readable}"),
        ("en", "China white sugar futures {readable}"),
        ("en", "China sugar syrup imports {readable}"),
    ),
    "巴基斯坦": (
        ("en", "Pakistan sugar industry {readable}"),
        ("en", "Pakistan sugarcane sugar mills {readable}"),
        ("en", "Pakistan sugar export import price {readable}"),
        ("en", "Pakistan sugar policy {day} {month_name} {year}"),
        ("ur", "پاکستان چینی صنعت {day} جولائی {year}"),
        ("ur", "گنا چینی ملز {day} جولائی {year}"),
        ("ur", "چینی برآمد درآمد {day} جولائی {year}"),
    ),
    "菲律宾": (
        ("en", "Philippines sugar industry {readable}"),
        ("en", "Philippines sugar production import {readable}"),
        ("en", "Philippines Sugar Regulatory Administration {readable}"),
        ("en", "Philippines sugarcane mills {day} {month_name} {year}"),
        ("fil", "industriya ng asukal Hulyo {day} {year}"),
        ("fil", "produksyon ng tubo at asukal Hulyo {day} {year}"),
        ("fil", "importasyon ng asukal Hulyo {day} {year}"),
    ),
    "越南": (
        ("en", "Vietnam sugar industry {readable}"),
        ("en", "Vietnam sugar import tariff {readable}"),
        ("en", "Vietnam sugarcane production {readable}"),
        ("en", "Vietnam sugar anti-dumping {day} {month_name} {year}"),
        ("vi", "ngành đường Việt Nam ngày {day} tháng 7 năm {year}"),
        ("vi", "mía đường Việt Nam {date_slash}"),
        ("vi", "nhập khẩu đường {date_slash}"),
        ("vi", "thuế chống bán phá giá đường {date_slash}"),
    ),
    "俄罗斯": (
        ("en", "Russia sugar industry {readable}"),
        ("en", "Russia sugar beet production {readable}"),
        ("en", "Russia sugar export price {readable}"),
        ("en", "Russian sugar market {day} {month_name} {year}"),
        ("ru", "сахарная промышленность России {day} июля {year}"),
        ("ru", "сахарная свекла {day} июля {year}"),
        ("ru", "производство сахара Россия {day} июля {year}"),
        ("ru", "экспорт сахара Россия {day} июля {year}"),
    ),
    "印度尼西亚": (
        ("en", "Indonesia sugar industry {readable}"),
        ("en", "Indonesia sugar import production {readable}"),
        ("en", "Indonesia sugar self-sufficiency {readable}"),
        ("en", "Indonesia sugarcane mills {day} {month_name} {year}"),
        ("id", "industri gula Indonesia {day} Juli {year}"),
        ("id", "produksi gula dan tebu {day} Juli {year}"),
        ("id", "impor gula Indonesia {day} Juli {year}"),
        ("id", "swasembada gula {day} Juli {year}"),
        ("id", "pabrik gula {day} Juli {year}"),
    ),
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build Sugar News Excel and dashboard data.")
    parser.add_argument("--date", help="Target news date in YYYY-MM-DD. Defaults to Beijing yesterday.")
    parser.add_argument("--task-root", help="Existing Sugar News task root. Defaults to ../Sugar News.")
    parser.add_argument("--skip-if-success", action="store_true", help="Skip if public status already marks target date successful.")
    parser.add_argument("--offline-only", action="store_true", help="Do not attempt fallback online discovery; require verified JSON.")
    parser.add_argument("--allow-rss-autogen", action="store_true", help="Generate a conservative verified JSON from RSS if no curated verified JSON exists.")
    return parser.parse_args()


def beijing_now() -> datetime:
    fixed = os.getenv("SUGAR_NEWS_NOW")
    if fixed:
        return datetime.fromisoformat(fixed).astimezone(SHANGHAI)
    return datetime.now(SHANGHAI)


def target_date(value: str | None) -> str:
    if value:
        return datetime.strptime(value, "%Y-%m-%d").date().isoformat()
    return (beijing_now().date() - timedelta(days=1)).isoformat()


def task_root_from_args(value: str | None) -> Path:
    root = Path(value or os.getenv("SUGAR_NEWS_ROOT", str(DEFAULT_TASK_ROOT))).resolve()
    return root


def date_parts(date_text: str) -> tuple[str, str]:
    yyyy, mm, _ = date_text.split("-")
    return yyyy, mm


def ensure_task_dirs(task_root: Path, date_text: str) -> None:
    yyyy, mm = date_parts(date_text)
    for rel in (
        Path("data") / "verified_news" / yyyy / mm,
        Path("logs") / yyyy / mm,
        Path("reports") / yyyy / mm,
    ):
        (task_root / rel).mkdir(parents=True, exist_ok=True)


def verified_json_path(task_root: Path, date_text: str) -> Path:
    yyyy, mm = date_parts(date_text)
    return task_root / "data" / "verified_news" / yyyy / mm / f"sugar_news_{date_text}.json"


def search_log_path(task_root: Path, date_text: str) -> Path:
    yyyy, mm = date_parts(date_text)
    return task_root / "logs" / yyyy / mm / f"search_log_{date_text}.json"


def write_log_path(task_root: Path, date_text: str) -> Path:
    yyyy, mm = date_parts(date_text)
    return task_root / "logs" / yyyy / mm / f"write_log_{date_text}.json"


def excel_path(task_root: Path, date_text: str) -> Path:
    yyyy, mm = date_parts(date_text)
    return task_root / "reports" / yyyy / mm / f"Sugar News {date_text}.xlsx"


def public_report_path(date_text: str) -> Path:
    yyyy, mm = date_parts(date_text)
    return PUBLIC_DATA_ROOT / "reports" / yyyy / mm / f"{date_text}.json"


def public_index_path() -> Path:
    return PUBLIC_DATA_ROOT / "index.json"


def public_status_path() -> Path:
    return PUBLIC_DATA_ROOT / "status.json"


def success_exists(date_text: str) -> bool:
    path = public_status_path()
    if not path.exists():
        return False
    with path.open("r", encoding="utf-8") as f:
        status = json.load(f)
    return status.get("latestNewsDate") == date_text and status.get("lastRunStatus") == "success"


def google_news_rss_url(query: str) -> str:
    return "https://news.google.com/rss/search?q=" + quote_plus(query) + "&hl=en-US&gl=US&ceid=US:en"


def fetch_rss(query: str, timeout: int = 15) -> list[dict]:
    req = Request(google_news_rss_url(query), headers={"User-Agent": "Mozilla/5.0"})
    with urlopen(req, timeout=timeout) as resp:
        body = resp.read()
    root = ET.fromstring(body)
    items = []
    for node in root.findall("./channel/item"):
        title = node.findtext("title") or ""
        link = node.findtext("link") or ""
        published = node.findtext("pubDate") or ""
        desc = node.findtext("description") or ""
        items.append({"title": title, "link": link, "published": published, "description": desc})
    return items


def fallback_discovery(date_text: str, task_root: Path) -> None:
    """Record auditable search attempts.

    The cloud job needs a durable trail even when a fully verified newsroom-style
    dataset cannot be produced automatically. This fallback intentionally does
    not publish unverified RSS items as facts.
    """
    dt = datetime.strptime(date_text, "%Y-%m-%d")
    buddhist_year = dt.year + 543
    readable = dt.strftime("%B %-d %Y") if os.name != "nt" else dt.strftime("%B %#d %Y")
    context = {
        "readable": readable,
        "day": dt.day,
        "month_name": dt.strftime("%B"),
        "month": dt.month,
        "year": dt.year,
        "date_slash": dt.strftime("%d/%m/%Y"),
        "buddhist_year": buddhist_year,
    }
    searches = []
    searches.extend(("全球", "en", template.format(**context)) for template in GLOBAL_SEARCH_TEMPLATES)
    for country, templates in COUNTRY_SEARCH_TEMPLATES.items():
        searches.extend((country, language, template.format(**context)) for language, template in templates)
    searches.extend(("印度指标", language, template.format(**context)) for language, template in INDIA_PRICE_INVENTORY_SEARCH_TEMPLATES)
    log = {
        "target_date": date_text,
        "run_date": beijing_now().date().isoformat(),
        "search_tool": "Google News RSS fallback via urllib",
        "note": "RSS search results are logged for audit. Items are not published unless a verified JSON is created.",
        "date_format_examples": DATE_FORMAT_EXAMPLES,
        "india_price_inventory_sources": INDIA_PRICE_INVENTORY_SOURCE_GUIDE,
        "other_country_rule": "Other-country news is unlimited; each concrete country keeps an independent object/list and must never be collapsed into a single 其他 key.",
        "searches": [],
        "pipeline_counts": {
            "global_initial_candidates": 0,
            "country_supplement_candidates": 0,
            "candidate_news_after_search": 0,
            "date_verified_or_continuing_impact": 0,
            "relevance_passed": 0,
            "importance_passed": 0,
            "deduped": 0,
            "structured_data_count": 0,
            "passed_to_excel": 0,
        },
    }
    total = 0
    global_total = 0
    country_total = 0
    for country, language, query in searches:
        entry = {
            "country": country,
            "language": language,
            "keywords": query,
            "request_status": "pending",
            "returned_count": 0,
            "retained_count": 0,
            "filtered": [],
        }
        try:
            items = fetch_rss(query)
            entry["request_status"] = "executed"
            entry["returned_count"] = len(items)
            total += len(items)
            if country == "全球":
                global_total += len(items)
            else:
                country_total += len(items)
            entry["sample_results"] = items[:5]
            for result in items[:5]:
                entry["filtered"].append({
                    "country": country,
                    "title": result.get("title"),
                    "news_date": result.get("published"),
                    "source": "Google News RSS",
                    "url": result.get("link"),
                    "stage": "source_page_verification",
                    "reason": "RSS result requires source-page date/body verification before publication.",
                })
        except Exception as exc:
            entry["request_status"] = "failed"
            entry["error"] = str(exc)[:500]
        log["searches"].append(entry)
    log["pipeline_counts"]["global_initial_candidates"] = global_total
    log["pipeline_counts"]["country_supplement_candidates"] = country_total
    log["pipeline_counts"]["candidate_news_after_search"] = total
    path = search_log_path(task_root, date_text)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(log, f, ensure_ascii=False, indent=2)


def rss_source_from_title(title: str) -> tuple[str, str]:
    parts = title.rsplit(" - ", 1)
    if len(parts) == 2:
        return parts[0].strip(), parts[1].strip()
    return title.strip(), "Google News RSS"


def rss_item_date(item: dict) -> str | None:
    try:
        value = parsedate_to_datetime(item.get("published", ""))
    except Exception:
        return None
    return value.astimezone(SHANGHAI).date().isoformat()


def impact_for_rss(country: str, title: str) -> str:
    text = title.lower()
    if country in {"印度", "泰国"} and any(term in text for term in ("rain", "rainfall", "monsoon", "heavy rain", "weather")):
        if any(term in text for term in ("damage", "flood damage", "crop loss", "drought", "deficit")):
            return "偏多糖价：天气不利可能削弱甘蔗生长、运输或糖料供应。"
        return "偏空糖价：生长阶段降雨增加有利于改善甘蔗水分条件和单产，可能增加未来糖料供应。"
    if any(term in text for term in ("ethanol", "blend", "biofuel")):
        return "偏多糖价：乙醇需求或政策推进可能增加糖料制醇吸引力，减少部分制糖供应。"
    if any(term in text for term in ("record output", "surplus", "higher production", "import")):
        return "偏空糖价：供应或进口增加可能提高市场可用糖源。"
    if any(term in text for term in ("export ban", "quota", "tariff", "shortage")):
        return "偏多糖价：贸易限制或供应扰动可能减少国际市场可用糖源。"
    return "中性：该信息需要继续跟踪，短期对当期糖产量和出口量的直接影响有限。"


def autogenerate_verified_from_rss(task_root: Path, date_text: str) -> Path:
    dt = datetime.strptime(date_text, "%Y-%m-%d")
    readable = dt.strftime("%B %-d %Y") if os.name != "nt" else dt.strftime("%B %#d %Y")
    context = {
        "readable": readable,
        "day": dt.day,
        "month_name": dt.strftime("%B"),
        "month": dt.month,
        "year": dt.year,
        "date_slash": dt.strftime("%d/%m/%Y"),
        "buddhist_year": dt.year + 543,
    }
    country_templates = {
        country: templates
        for country, templates in COUNTRY_SEARCH_TEMPLATES.items()
        if country in {"巴西", "印度", "泰国", "中国"}
    }
    country_templates["印度指标"] = INDIA_PRICE_INVENTORY_SEARCH_TEMPLATES
    country_templates["其他国家"] = tuple(("en", template) for template in GLOBAL_SEARCH_TEMPLATES)
    relevance = (
        "sugar", "sugarcane", "cane", "ethanol", "biofuel", "molasses", "raw sugar",
        "white sugar", "rain", "rainfall", "monsoon", "drought", "flood", "tariff",
        "quota", "export", "import", "mill", "crushing",
    )
    concrete_other = ("Indonesia", "Pakistan", "Philippines", "Vietnam", "Russia", "EU", "United States", "Mexico")
    items = []
    seen = set()
    search_log = {
        "target_date": date_text,
        "run_date": beijing_now().date().isoformat(),
        "search_tool": "Google News RSS autogeneration",
        "note": "Generated automatically because curated verified JSON was missing. Each item keeps RSS source, publication date, and source link.",
        "india_price_inventory_sources": INDIA_PRICE_INVENTORY_SOURCE_GUIDE,
        "searches": [],
    }
    for country, templates in country_templates.items():
        retained_for_country = 0
        for language, template in templates:
            query = template.format(**context)
            entry = {"country": country, "language": language, "keywords": query, "request_status": "pending", "returned_count": 0, "retained_count": 0, "filtered": []}
            try:
                rss_items = fetch_rss(query, timeout=20)
                entry["request_status"] = "executed"
                entry["returned_count"] = len(rss_items)
            except Exception as exc:
                entry["request_status"] = "failed"
                entry["error"] = str(exc)[:500]
                search_log["searches"].append(entry)
                continue
            if country == "印度指标":
                entry["sample_results"] = rss_items[:5]
                for result in rss_items[:5]:
                    entry["filtered"].append({
                        "title": result.get("title"),
                        "news_date": result.get("published"),
                        "source": "Google News RSS",
                        "url": result.get("link"),
                        "stage": "price_inventory_verification",
                        "reason": "Price and stock indicators require source-page date, quote type, unit, and comparable-date verification before dashboard publication.",
                    })
                search_log["searches"].append(entry)
                continue
            for rss in rss_items[:10]:
                item_date = rss_item_date(rss)
                title_raw = rss.get("title", "").strip()
                if item_date != date_text:
                    entry["filtered"].append({"title": title_raw, "reason": "publication date is not target date", "published": rss.get("published")})
                    continue
                title_clean, source = rss_source_from_title(title_raw)
                haystack = f"{title_clean} {rss.get('description', '')}".lower()
                if not any(term in haystack for term in relevance):
                    entry["filtered"].append({"title": title_raw, "reason": "not sugar/rainfall/ethanol relevant"})
                    continue
                concrete_country = country
                country_group = country
                if country == "其他国家":
                    found = next((name for name in concrete_other if name.lower() in haystack), None)
                    if not found:
                        entry["filtered"].append({"title": title_raw, "reason": "other-country item lacks concrete priority country"})
                        continue
                    concrete_country = {
                        "Indonesia": "印度尼西亚",
                        "Pakistan": "巴基斯坦",
                        "Philippines": "菲律宾",
                        "Vietnam": "越南",
                        "Russia": "俄罗斯",
                        "EU": "欧盟",
                        "United States": "美国",
                        "Mexico": "墨西哥",
                    }[found]
                key = re.sub(r"\W+", "", title_clean.lower())[:120]
                if key in seen:
                    continue
                seen.add(key)
                link = rss.get("link", "").strip()
                impact = impact_for_rss(country_group, title_clean)
                news = f"{item_date} {source}报道：{title_clean}。来源：{source}（{link}）"
                items.append({
                    "country_group": country_group,
                    "country": concrete_country,
                    "title": title_clean[:80],
                    "news": news,
                    "impact": impact,
                    "source_name": source,
                    "source_url": link,
                    "published_date_local": date_text,
                    "event_date": date_text,
                    "date_status": "verified",
                    "dedupe_key": f"rss_{key}",
                    "importance": max(50, 90 - retained_for_country * 5),
                })
                retained_for_country += 1
                entry["retained_count"] += 1
                if retained_for_country >= 2:
                    break
            search_log["searches"].append(entry)
            if retained_for_country >= 2:
                break
    if not items:
        raise FileNotFoundError("RSS autogeneration found no publishable Sugar News items")
    path = verified_json_path(task_root, date_text)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump({
            "target_date": date_text,
            "run_date": beijing_now().date().isoformat(),
            "search_tool": "Google News RSS autogeneration",
            "items": items,
        }, f, ensure_ascii=False, indent=2)
    log_path = search_log_path(task_root, date_text)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    search_log["pipeline_counts"] = {"structured_data_count": len(items), "passed_to_excel": len(items)}
    with log_path.open("w", encoding="utf-8") as f:
        json.dump(search_log, f, ensure_ascii=False, indent=2)
    return path


def load_verified_or_fail(task_root: Path, date_text: str, offline_only: bool, allow_rss_autogen: bool = False) -> dict:
    path = verified_json_path(task_root, date_text)
    if not path.exists() and not offline_only:
        fallback_discovery(date_text, task_root)
        if allow_rss_autogen:
            path = autogenerate_verified_from_rss(task_root, date_text)
    if not path.exists():
        raise FileNotFoundError(
            f"Missing verified Sugar News data: {path}. "
            "The job stopped before Excel/dashboard publication to avoid publishing blank or unverified content."
        )
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if data.get("target_date") != date_text:
        raise ValueError(f"target_date mismatch in {path}")
    return data


def _contains_any(text: str, terms: tuple[str, ...]) -> bool:
    lowered = text.lower()
    return any(term.lower() in lowered for term in terms)


def validate_india_weather_impact(item: dict, idx: int) -> None:
    if item.get("country_group") != "印度":
        return

    fact_text = " ".join(str(item.get(field, "")) for field in ("title", "news"))
    text = f"{fact_text} {item.get('impact', '')}"
    if not _contains_any(text, INDIA_WEATHER_TERMS):
        return

    in_main_area = _contains_any(fact_text, INDIA_MAIN_CANE_REGIONS)
    if not in_main_area:
        if not item["impact"].startswith("影响有限："):
            raise ValueError(f"India weather item {idx} is outside main cane regions and should be impact-limited")
        return

    if _contains_any(fact_text, INDIA_HARVEST_TERMS):
        if not item["impact"].startswith("偏多糖价："):
            raise ValueError(f"India weather item {idx} indicates harvest/crushing disruption and should be bullish")
        return

    if _contains_any(fact_text, INDIA_DAMAGE_TERMS):
        if not item["impact"].startswith("偏多糖价："):
            raise ValueError(f"India weather item {idx} indicates confirmed damage and should be bullish")
        return

    if _contains_any(fact_text, INDIA_DROUGHT_TERMS):
        if not item["impact"].startswith("偏多糖价："):
            raise ValueError(f"India weather item {idx} indicates drought/rain shortage and should be bullish")
        return

    if _contains_any(fact_text, INDIA_RAIN_BENEFIT_TERMS):
        if not item["impact"].startswith("偏空糖价："):
            raise ValueError(f"India weather item {idx} indicates growing-season rainfall support and should be bearish")


def validate_thai_weather_impact(item: dict, idx: int) -> None:
    if item.get("country_group") != "泰国":
        return

    fact_text = " ".join(str(item.get(field, "")) for field in ("title", "news"))
    text = f"{fact_text} {item.get('impact', '')}"
    if not _contains_any(text, THAI_WEATHER_TERMS):
        return
    if not _contains_any(fact_text, THAI_WEATHER_EVENT_TERMS):
        return

    in_main_area = (
        _contains_any(fact_text, THAI_MAIN_CANE_PROVINCES)
        or "东北部" in fact_text
        or "中部" in fact_text
    )
    if not in_main_area:
        if not item["impact"].startswith("影响有限："):
            raise ValueError(f"Thai weather item {idx} is outside main cane areas and should be impact-limited")
        return

    has_low_coverage = _contains_any(fact_text, THAI_LOW_COVERAGE_TERMS)
    if _contains_any(fact_text, THAI_HARVEST_TERMS):
        if not item["impact"].startswith("偏多糖价："):
            raise ValueError(f"Thai weather item {idx} indicates harvest disruption and should be bullish")
        return

    if _contains_any(fact_text, THAI_DAMAGE_TERMS):
        return

    if _contains_any(fact_text, THAI_DROUGHT_TERMS):
        if not item["impact"].startswith("偏多糖价："):
            raise ValueError(f"Thai weather item {idx} indicates drought/rain shortage and should be bullish")
        return

    if _contains_any(fact_text, THAI_RAIN_INCREASE_TERMS) and not has_low_coverage:
        if not item["impact"].startswith("偏空糖价："):
            raise ValueError(f"Thai weather item {idx} indicates growing-season rainfall improvement and should be bearish")


def normalize_items(data: dict) -> list[dict]:
    items = data.get("items") or []
    seen = set()
    normalized = []
    for idx, item in enumerate(items, start=1):
        for field in ("country_group", "country", "news", "impact", "source_name", "source_url", "published_date_local"):
            if not item.get(field):
                raise ValueError(f"Verified item {idx} missing {field}")
        if not item["impact"].startswith(IMPACT_PREFIXES):
            raise ValueError(f"Verified item {idx} has invalid impact prefix")
        if any(text in item["news"] or text in item["impact"] for text in PLACEHOLDERS):
            raise ValueError(f"Verified item {idx} contains placeholder wording")
        if re.search(r"\bLMT\b|lmt", item["news"]):
            raise ValueError(f"Verified item {idx} contains raw LMT/lmt unit")
        if "来源：" not in item["news"] or item["source_url"] not in item["news"]:
            raise ValueError(f"Verified item {idx} missing B-column source link")
        if item["country_group"] == "其他国家" and item["country"] == "其他":
            raise ValueError("Other-country rows must use the concrete country/region name, not 其他")
        if item["country"] == "中国" and item["country_group"] != "中国":
            raise ValueError("China news must use country_group=中国 and must not be stored as other-country news")
        if item["country_group"] == "中国" and item["country"] != "中国":
            raise ValueError("country_group=中国 rows must use country=中国")
        if item["published_date_local"] != data["target_date"] and item.get("date_status") != "continuing_impact":
            raise ValueError(f"Verified item {idx} date is not target date or continuing impact")
        validate_india_weather_impact(item, idx)
        validate_thai_weather_impact(item, idx)
        dedupe_key = item.get("dedupe_key") or re.sub(r"\s+", "", item["news"][:100])
        if dedupe_key in seen:
            raise ValueError(f"Duplicate verified news: {dedupe_key}")
        seen.add(dedupe_key)
        row = dict(item)
        row["_order"] = idx
        normalized.append(row)
    return sorted(normalized, key=lambda x: (GROUP_ORDER.get(x["country_group"], 3), -int(x.get("importance", 0)), x["_order"]))


def copy_row_style(source_ws, source_row: int, target_ws, target_row: int) -> None:
    for col in range(1, 4):
        source = source_ws.cell(source_row, col)
        target = target_ws.cell(target_row, col)
        if source.has_style:
            target._style = copy(source._style)
        target.number_format = source.number_format
        target.protection = copy(source.protection)
        target.alignment = copy(source.alignment)
        target.fill = copy(source.fill)
        target.border = copy(source.border)
        target.font = copy(source.font)


def write_excel(task_root: Path, date_text: str, items: list[dict]) -> Path:
    template = task_root / "templates" / "新闻格式.xlsx"
    if not template.exists():
        raise FileNotFoundError(f"Missing template: {template}")
    out = excel_path(task_root, date_text)
    out.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(template, out)

    wb = load_workbook(out)
    ws = wb.active
    if [ws.cell(1, c).value for c in range(1, 4)] != ["国家", "新闻", "影响"]:
        raise ValueError("Excel template headers must be 国家/新闻/影响")
    if ws.max_row > 1:
        ws.delete_rows(2, ws.max_row - 1)

    template_wb = load_workbook(template)
    template_ws = template_wb.active
    source_row = 2 if template_ws.max_row >= 2 else 1
    for row, item in enumerate(items, start=2):
        copy_row_style(template_ws, source_row, ws, row)
        ws.cell(row, 1).value = item["country"]
        ws.cell(row, 2).value = item["news"]
        ws.cell(row, 3).value = item["impact"]
        ws.cell(row, 1).alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        ws.cell(row, 2).alignment = Alignment(horizontal="left", vertical="center", wrap_text=True)
        ws.cell(row, 3).alignment = Alignment(horizontal="left", vertical="center", wrap_text=True)
        ws.row_dimensions[row].height = max(72, min(180, 24 + 0.55 * max(len(item["news"]), len(item["impact"]))))
    for col in ("B", "C"):
        ws.column_dimensions[col].width = max(ws.column_dimensions[col].width or 0, 55)
    wb.save(out)
    return out


def read_excel_rows(path: Path) -> list[dict]:
    wb = load_workbook(path)
    ws = wb.active
    rows = []
    for row in range(2, ws.max_row + 1):
        country = ws.cell(row, 1).value
        news = ws.cell(row, 2).value
        impact = ws.cell(row, 3).value
        if country or news or impact:
            rows.append({"row": row, "country": country, "news": news, "impact": impact})
    return rows


def split_impact(value: str) -> tuple[str, str]:
    for prefix in IMPACT_PREFIXES:
        if value.startswith(prefix):
            return prefix[:-1], value[len(prefix):]
    raise ValueError(f"Invalid impact value: {value}")


def _number(value):
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).replace(",", "").strip()
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    return float(match.group(0)) if match else None


def _round(value, digits: int = 2):
    return None if value is None else round(float(value), digits)


def price_from_quintal(value) -> float | None:
    number = _number(value)
    return _round(number / 100, 4) if number is not None else None


def lakh_tonnes_to_wan_tonnes(value) -> float | None:
    number = _number(value)
    return _round(number * 10, 2) if number is not None else None


def million_tonnes_to_wan_tonnes(value) -> float | None:
    number = _number(value)
    return _round(number * 100, 2) if number is not None else None


def normalize_price_metric(metric: dict | None, metric_type: str) -> dict:
    metric = dict(metric or {})
    status = metric.get("status") or ("ok" if metric.get("priceInrPerQuintal") or metric.get("rangeInrPerQuintal") else "pending")
    result = {
        "metricType": metric_type,
        "status": status,
        "statusText": metric.get("statusText") or ("数据待更新" if status != "ok" else ""),
        "priceDate": metric.get("priceDate") or metric.get("dataDate"),
        "grade": metric.get("grade"),
        "market": metric.get("market"),
        "quoteType": metric.get("quoteType"),
        "originalUnit": metric.get("originalUnit") or "₹/quintal",
        "sourceName": metric.get("sourceName"),
        "sourceUrl": metric.get("sourceUrl"),
        "publishedDate": metric.get("publishedDate"),
        "fetchedAt": metric.get("fetchedAt") or beijing_now().isoformat(timespec="seconds"),
        "note": metric.get("note"),
    }
    price_q = _number(metric.get("priceInrPerQuintal"))
    price_kg = _number(metric.get("priceInrPerKg"))
    if price_q is None and price_kg is not None:
        price_q = price_kg * 100
    if price_kg is None and price_q is not None:
        price_kg = price_from_quintal(price_q)
    result["priceInrPerQuintal"] = _round(price_q, 2)
    result["priceInrPerKg"] = _round(price_kg, 4)
    result["retailPriceInrPerKg"] = _round(_number(metric.get("retailPriceInrPerKg")), 2)

    range_q = metric.get("rangeInrPerQuintal") or {}
    low_q = _number(range_q.get("low") if isinstance(range_q, dict) else None)
    high_q = _number(range_q.get("high") if isinstance(range_q, dict) else None)
    if low_q is not None or high_q is not None:
        result["rangeInrPerQuintal"] = {"low": _round(low_q, 2), "high": _round(high_q, 2)}
        result["rangeInrPerKg"] = {"low": price_from_quintal(low_q), "high": price_from_quintal(high_q)}
    previous_range_q = metric.get("previousRangeInrPerQuintal") or {}
    if isinstance(previous_range_q, dict) and (previous_range_q.get("low") is not None or previous_range_q.get("high") is not None):
        prev_low = _number(previous_range_q.get("low"))
        prev_high = _number(previous_range_q.get("high"))
        result["previousRangeInrPerQuintal"] = {"low": _round(prev_low, 2), "high": _round(prev_high, 2)}
        result["previousRangeInrPerKg"] = {"low": price_from_quintal(prev_low), "high": price_from_quintal(prev_high)}

    previous_q = _number(metric.get("previousInrPerQuintal"))
    if previous_q is not None:
        result["previousInrPerQuintal"] = _round(previous_q, 2)
        result["previousInrPerKg"] = price_from_quintal(previous_q)
    change_q = _number(metric.get("changeInrPerQuintal"))
    if change_q is None and price_q is not None and previous_q is not None:
        change_q = price_q - previous_q
    result["changeInrPerQuintal"] = _round(change_q, 2)
    result["changeInrPerKg"] = price_from_quintal(change_q)
    change_pct = _number(metric.get("changePct"))
    if change_pct is None and change_q is not None and previous_q:
        change_pct = change_q / previous_q * 100
    result["changePct"] = _round(change_pct, 2)
    result["direction"] = metric.get("direction") or ("up" if change_q and change_q > 0 else "down" if change_q and change_q < 0 else "flat" if change_q == 0 else "unknown")
    if metric.get("gstStatus"):
        result["gstStatus"] = metric.get("gstStatus")
    return result


def normalize_stock_metric(metric: dict | None) -> dict:
    metric = dict(metric or {})
    status = metric.get("status") or ("ok" if any(metric.get(k) is not None for k in ("stockWanTonnes", "stockLakhTonnes", "stockMillionTonnes")) else "pending")
    stock_wan = _number(metric.get("stockWanTonnes"))
    if stock_wan is None and metric.get("stockLakhTonnes") is not None:
        stock_wan = lakh_tonnes_to_wan_tonnes(metric.get("stockLakhTonnes"))
    if stock_wan is None and metric.get("stockMillionTonnes") is not None:
        stock_wan = million_tonnes_to_wan_tonnes(metric.get("stockMillionTonnes"))
    previous_wan = _number(metric.get("previousForecastWanTonnes"))
    if previous_wan is None and metric.get("previousForecastLakhTonnes") is not None:
        previous_wan = lakh_tonnes_to_wan_tonnes(metric.get("previousForecastLakhTonnes"))
    yoy_wan = _number(metric.get("yoyChangeWanTonnes"))
    if yoy_wan is None and metric.get("yoyChangeLakhTonnes") is not None:
        yoy_wan = lakh_tonnes_to_wan_tonnes(metric.get("yoyChangeLakhTonnes"))
    revision_wan = _number(metric.get("revisionWanTonnes"))
    if revision_wan is None and stock_wan is not None and previous_wan is not None:
        revision_wan = stock_wan - previous_wan
    return {
        "metricType": "carryoverStock",
        "status": status,
        "statusText": metric.get("statusText") or ("数据待更新" if status != "ok" else ""),
        "dataDate": metric.get("dataDate"),
        "season": metric.get("season"),
        "stockWanTonnes": _round(stock_wan, 2),
        "stockLakhTonnes": _round(stock_wan / 10, 2) if stock_wan is not None else _round(_number(metric.get("stockLakhTonnes")), 2),
        "stockMillionTonnes": _round(stock_wan / 100, 2) if stock_wan is not None else _round(_number(metric.get("stockMillionTonnes")), 2),
        "previousForecastWanTonnes": _round(previous_wan, 2),
        "revisionWanTonnes": _round(revision_wan, 2),
        "yoyChangeWanTonnes": _round(yoy_wan, 2),
        "stockUseRatio": _round(_number(metric.get("stockUseRatio")), 2),
        "consumptionMonths": _round(_number(metric.get("consumptionMonths")), 2),
        "reason": metric.get("reason"),
        "sourceName": metric.get("sourceName"),
        "sourceUrl": metric.get("sourceUrl"),
        "publishedDate": metric.get("publishedDate"),
        "fetchedAt": metric.get("fetchedAt") or beijing_now().isoformat(timespec="seconds"),
        "note": metric.get("note"),
    }


def pending_metric(metric_type: str) -> dict:
    base = {
        "metricType": metric_type,
        "status": "pending",
        "statusText": "数据待更新",
        "fetchedAt": beijing_now().isoformat(timespec="seconds"),
        "note": "未获取到已完成日期、口径和来源核验的可靠数据；不编造价格或库存。",
    }
    if metric_type == "carryoverStock":
        base.update({"dataDate": None, "stockWanTonnes": None})
    else:
        base.update({"priceDate": None, "priceInrPerQuintal": None, "priceInrPerKg": None})
    return base


def latest_previous_india_metrics(date_text: str) -> dict | None:
    reports_root = PUBLIC_DATA_ROOT / "reports"
    if not reports_root.exists():
        return None
    candidates = []
    for path in reports_root.rglob("*.json"):
        try:
            with path.open("r", encoding="utf-8") as f:
                payload = json.load(f)
        except Exception:
            continue
        if payload.get("newsDate", "") >= date_text:
            continue
        metrics = payload.get("indiaMetrics")
        if metrics:
            candidates.append((payload.get("newsDate"), metrics))
    if not candidates:
        return None
    return sorted(candidates, key=lambda pair: pair[0], reverse=True)[0][1]


def latest_india_metrics_snapshot() -> dict | None:
    path = PUBLIC_DATA_ROOT / "india_metrics" / "latest.json"
    if not path.exists():
        return None
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def india_metrics_from_snapshot(snapshot: dict | None) -> dict:
    if not snapshot:
        return {}
    domestic = snapshot.get("domesticSugarPrice") or {}
    up_ex = snapshot.get("upExMillPrice") or {}
    stock = snapshot.get("carryoverStock") or {}
    result = {
        "dataDate": snapshot.get("targetDate"),
        "sourceStatus": "dynamic_fetch",
        "fetchLog": snapshot.get("fetchLog", []),
    }
    if domestic:
        result["domesticSugarPrice"] = {
            "status": domestic.get("status", "ok"),
            "priceDate": domestic.get("data_date"),
            "grade": "Sugar",
            "market": "All India Average",
            "quoteType": "official wholesale and retail",
            "priceInrPerQuintal": domestic.get("wholesale_price_inr_per_quintal"),
            "priceInrPerKg": domestic.get("wholesale_price_inr_per_kg"),
            "retailPriceInrPerKg": domestic.get("retail_price_inr_per_kg"),
            "previousInrPerQuintal": domestic.get("previous_value"),
            "changeInrPerQuintal": domestic.get("change_value"),
            "changePct": domestic.get("change_percent"),
            "originalUnit": "₹/quintal and ₹/kg",
            "sourceName": domestic.get("source_name"),
            "sourceUrl": domestic.get("source_url"),
            "publishedDate": domestic.get("data_date"),
            "fetchedAt": domestic.get("fetched_at"),
        }
    if up_ex:
        result["upExMillPrice"] = {
            "status": up_ex.get("status", "ok"),
            "priceDate": up_ex.get("data_date"),
            "grade": "M/30",
            "market": "Uttar Pradesh / Muzaffarnagar",
            "quoteType": "ex-mill",
            "rangeInrPerQuintal": {"low": up_ex.get("up_ex_mill_min_inr_per_quintal"), "high": up_ex.get("up_ex_mill_max_inr_per_quintal")},
            "previousRangeInrPerQuintal": {"low": up_ex.get("previous_min"), "high": up_ex.get("previous_max")},
            "changeInrPerQuintal": up_ex.get("change_value"),
            "direction": up_ex.get("change_direction"),
            "gstStatus": up_ex.get("gst_status"),
            "originalUnit": "₹/quintal",
            "sourceName": up_ex.get("source_name"),
            "sourceUrl": up_ex.get("source_url"),
            "publishedDate": up_ex.get("data_date"),
            "fetchedAt": up_ex.get("fetched_at"),
        }
    if stock:
        result["carryoverStock"] = {
            "status": stock.get("status", "ok"),
            "dataDate": stock.get("forecast_date") or stock.get("data_date"),
            "season": stock.get("season"),
            "stockWanTonnes": stock.get("closing_stock_ten_thousand_tonnes"),
            "stockLakhTonnes": stock.get("closing_stock_lakh_tonnes"),
            "stockMillionTonnes": stock.get("closing_stock_million_tonnes"),
            "previousForecastWanTonnes": stock.get("previous_forecast_value"),
            "revisionWanTonnes": stock.get("forecast_revision"),
            "yoyChangeWanTonnes": stock.get("year_on_year_change"),
            "sourceName": stock.get("forecast_organization") or stock.get("source_name"),
            "sourceUrl": stock.get("source_url"),
            "publishedDate": stock.get("forecast_date"),
            "fetchedAt": stock.get("fetched_at"),
        }
    forecasts = snapshot.get("carryoverStockForecasts") or []
    if forecasts:
        result["carryoverStockForecasts"] = forecasts
    return result


def normalize_india_metrics(data: dict, date_text: str) -> dict:
    raw = data.get("indiaMetrics") or data.get("india_metrics")
    snapshot_raw = india_metrics_from_snapshot(latest_india_metrics_snapshot())
    previous = latest_previous_india_metrics(date_text) or {}
    source = raw if isinstance(raw, dict) else snapshot_raw if snapshot_raw else previous if isinstance(previous, dict) else {}
    domestic = source.get("domesticSugarPrice") if isinstance(source, dict) else None
    up_ex_mill = source.get("upExMillPrice") if isinstance(source, dict) else None
    stock = source.get("carryoverStock") if isinstance(source, dict) else None
    payload = {
        "title": "印度糖价与库存",
        "dataDate": (source.get("dataDate") if isinstance(source, dict) else None) or date_text,
        "updatedAt": beijing_now().isoformat(timespec="seconds"),
        "sourceStatus": "verified" if raw else "dynamic_fetch" if snapshot_raw else "carried_forward" if previous else "pending",
        "domesticSugarPrice": normalize_price_metric(domestic, "domesticSugarPrice") if domestic else pending_metric("domesticSugarPrice"),
        "upExMillPrice": normalize_price_metric(up_ex_mill, "upExMillPrice") if up_ex_mill else pending_metric("upExMillPrice"),
        "carryoverStock": normalize_stock_metric(stock) if stock else pending_metric("carryoverStock"),
    }
    if not raw and snapshot_raw:
        payload["note"] = source.get("note") or "印度糖价与库存指标来自本次动态抓取；未通过口径核验的库存候选不发布数值。"
        if source.get("fetchLog"):
            payload["fetchLog"] = source.get("fetchLog")
        if source.get("carryoverStockForecasts"):
            payload["carryoverStockForecasts"] = source.get("carryoverStockForecasts")
    elif not raw and previous:
        payload["note"] = "本期未发现新的已核验印度糖价或结转库存数据，沿用最近一期有效数据并保留原始数据日期。"
    elif not raw:
        payload["note"] = "本期未获取到已完成日期、口径和来源核验的印度糖价与结转库存数据。"
    else:
        payload["note"] = source.get("note")
    return payload


def latest_brazil_metrics_snapshot() -> dict | None:
    path = PUBLIC_DATA_ROOT / "brazil_metrics" / "latest.json"
    if not path.exists():
        return None
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def first_log_url(metric: dict | None) -> tuple[str | None, str | None]:
    if not isinstance(metric, dict):
        return None, None
    for entry in metric.get("fetchLog") or []:
        if entry.get("url"):
            return entry.get("source"), entry.get("url")
    return metric.get("source_name") or metric.get("dataset_name"), metric.get("source_url")


def normalize_brazil_metric(metric: dict | None, metric_type: str) -> dict:
    metric = metric if isinstance(metric, dict) else {}
    status = metric.get("status") or "pending"
    source_name, source_url = first_log_url(metric)
    base = {
        "metricType": metric_type,
        "status": status if status in {"ok", "pending", "stale"} else "pending",
        "statusText": metric.get("statusText") or ("数据待更新" if status != "ok" else ""),
        "dataDate": metric.get("data_date") or metric.get("reference_period") or metric.get("published_at"),
        "fetchedAt": metric.get("fetched_at") or beijing_now().isoformat(timespec="seconds"),
        "sourceName": metric.get("source_name") or metric.get("dataset_name") or source_name,
        "sourceUrl": metric.get("source_url") or source_url,
        "previousYearValue": metric.get("previous_year_value"),
        "yearOnYearChange": metric.get("year_on_year_change"),
        "yearOnYearChangePercent": metric.get("year_on_year_change_percent"),
        "yoyStatus": metric.get("yoy_status") or ("ok" if metric.get("year_on_year_change_percent") is not None else "insufficient"),
        "note": metric.get("note"),
    }
    if metric_type == "sugarPremium":
        value = metric.get("premium_discount_cents_per_lb")
        base.update({
            "product": metric.get("product"),
            "port": metric.get("port"),
            "pricingBasis": metric.get("pricing_basis"),
            "futuresContract": metric.get("futures_contract"),
            "premiumDiscountCentsPerLb": value,
            "premiumLabel": "升水" if isinstance(value, (int, float)) and value >= 0 else "贴水" if isinstance(value, (int, float)) else None,
            "unit": "美分/磅",
        })
    elif metric_type == "sugarStock":
        base.update({
            "product": metric.get("product") or "食糖",
            "stockValue": metric.get("sugar_stock_value"),
            "stockUnit": metric.get("stock_unit"),
            "datasetName": metric.get("dataset_name"),
            "season": metric.get("season"),
            "referenceDate": metric.get("reference_date") or metric.get("reference_period"),
            "stockTotalTonnes": metric.get("stock_total_tonnes"),
            "stockTotalTenThousandTonnes": metric.get("stock_total_ten_thousand_tonnes"),
            "previousPeriodDate": metric.get("previous_period_date"),
            "previousPeriodStock": metric.get("previous_period_stock"),
            "halfMonthChange": metric.get("half_month_change"),
            "halfMonthChangePercent": metric.get("half_month_change_percent"),
            "previousYearDate": metric.get("previous_year_date"),
            "previousYearStock": metric.get("previous_year_stock"),
            "documentNumber": metric.get("document_number"),
            "documentTitle": metric.get("document_title"),
            "fileHash": metric.get("file_hash"),
        })
    else:
        base.update({
            "ethanolType": metric.get("ethanol_type"),
            "hydrousEthanolStock": metric.get("hydrous_ethanol_stock"),
            "anhydrousEthanolStock": metric.get("anhydrous_ethanol_stock"),
            "totalEthanolStock": metric.get("total_ethanol_stock"),
            "stockUnit": metric.get("stock_unit"),
            "datasetName": metric.get("dataset_name"),
        })
    return base


def normalize_brazil_metrics(date_text: str) -> dict:
    snapshot = latest_brazil_metrics_snapshot() or {}
    return {
        "title": "巴西糖价与库存",
        "dataDate": snapshot.get("targetDate") or date_text,
        "updatedAt": snapshot.get("updatedAt") or beijing_now().isoformat(timespec="seconds"),
        "sourceStatus": "dynamic_fetch" if snapshot else "pending",
        "sugarPremium": normalize_brazil_metric(snapshot.get("sugarPremium"), "sugarPremium"),
        "sugarStock": normalize_brazil_metric(snapshot.get("sugarStock"), "sugarStock"),
        "ethanolStock": normalize_brazil_metric(snapshot.get("ethanolStock"), "ethanolStock"),
        "note": "巴西糖价与库存指标来自动态检索；未完成来源、口径和字段核验的数据不发布数值。",
        "fetchLog": snapshot.get("fetchLog", []),
    }


def build_dashboard_payload(date_text: str, items: list[dict], excel_file: Path, verified_data: dict | None = None) -> dict:
    grouped: dict[str, list[dict]] = defaultdict(list)
    country_order: list[tuple[int, int, str]] = []
    for item in items:
        impact_type, impact_text = split_impact(item["impact"])
        grouped[item["country"]].append({
            "news": re.sub(r"\s*来源：.*$", "", item["news"]).strip(),
            "impactType": impact_type,
            "impact": impact_text.strip(),
            "sourceName": item["source_name"],
            "sourceUrl": item["source_url"],
            "publishedDateLocal": item["published_date_local"],
            "eventDate": item.get("event_date"),
        })
        country_order.append((GROUP_ORDER.get(item["country_group"], 3), -int(item.get("importance", 0)), item["country"]))

    countries = []
    seen = set()
    for _, _, country in sorted(country_order, key=lambda pair: (pair[0], pair[1], country_order.index(pair))):
        if country in seen:
            continue
        seen.add(country)
        if grouped[country]:
            countries.append({"country": country, "items": grouped[country]})

    return {
        "newsDate": date_text,
        "updatedAt": beijing_now().isoformat(timespec="seconds"),
        "timezone": "Asia/Shanghai",
        "excelFile": str(excel_file.relative_to(WORKSPACE_ROOT)).replace("\\", "/"),
        "brazilMetrics": normalize_brazil_metrics(date_text),
        "indiaMetrics": normalize_india_metrics(verified_data or {}, date_text),
        "countries": countries,
    }


def write_dashboard_data(date_text: str, payload: dict) -> tuple[Path, Path]:
    report_path = public_report_path(date_text)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    with report_path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    reports = []
    reports_root = PUBLIC_DATA_ROOT / "reports"
    if reports_root.exists():
        for path in reports_root.rglob("*.json"):
            with path.open("r", encoding="utf-8") as f:
                entry = json.load(f)
            reports.append({
                "newsDate": entry["newsDate"],
                "updatedAt": entry.get("updatedAt"),
                "path": "/" + str(path.relative_to(PROJECT_ROOT)).replace("\\", "/"),
                "count": sum(len(c.get("items", [])) for c in entry.get("countries", [])),
            })
    reports.sort(key=lambda x: x["newsDate"], reverse=True)
    index = {
        "latestNewsDate": reports[0]["newsDate"] if reports else None,
        "updatedAt": beijing_now().isoformat(timespec="seconds"),
        "reports": reports,
    }
    index_path = public_index_path()
    index_path.parent.mkdir(parents=True, exist_ok=True)
    with index_path.open("w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, indent=2)
    return report_path, index_path


def validate_all(date_text: str, items: list[dict], excel_file: Path, report_path: Path, index_path: Path) -> dict:
    excel_rows = read_excel_rows(excel_file)
    expected_pairs = {(item["country"], item["news"]) for item in items}
    actual_pairs = {(row["country"], row["news"]) for row in excel_rows}
    if expected_pairs != actual_pairs:
        missing = expected_pairs - actual_pairs
        extra = actual_pairs - expected_pairs
        raise ValueError(f"Excel mismatch: missing={missing}; extra={extra}")

    with report_path.open("r", encoding="utf-8") as f:
        report = json.load(f)
    with index_path.open("r", encoding="utf-8") as f:
        index = json.load(f)
    dashboard_count = sum(len(c.get("items", [])) for c in report.get("countries", []))
    expected_china = sum(1 for item in items if item["country_group"] == "中国" or item["country"] == "中国")
    actual_china = sum(
        len(c.get("items", []))
        for c in report.get("countries", [])
        if c.get("country") == "中国"
    )
    if dashboard_count != len(items):
        raise ValueError(f"Dashboard count mismatch: {dashboard_count} != {len(items)}")
    if report.get("newsDate") != date_text:
        raise ValueError("Dashboard report date mismatch")
    if index.get("latestNewsDate") < date_text:
        raise ValueError("Dashboard index latest date is older than target date")
    if any(not c.get("items") for c in report.get("countries", [])):
        raise ValueError("Dashboard contains empty country section")
    if any(c.get("country") == "其他" for c in report.get("countries", [])):
        raise ValueError("Dashboard must not collapse other countries into a single 其他 section")
    if actual_china != expected_china:
        raise ValueError(f"China dashboard count mismatch: {actual_china} != {expected_china}")
    brazil_metrics = report.get("brazilMetrics")
    if not isinstance(brazil_metrics, dict):
        raise ValueError("Dashboard missing brazilMetrics")
    for field in ("sugarPremium", "sugarStock", "ethanolStock"):
        metric = brazil_metrics.get(field)
        if not isinstance(metric, dict):
            raise ValueError(f"brazilMetrics missing {field}")
        if metric.get("status") not in {"ok", "pending", "stale"}:
            raise ValueError(f"brazilMetrics {field} has invalid status")
        if metric.get("status") == "ok":
            if field == "sugarPremium" and metric.get("premiumDiscountCentsPerLb") is None:
                raise ValueError("sugarPremium ok status requires premiumDiscountCentsPerLb")
            if field == "sugarStock" and metric.get("stockValue") is None:
                raise ValueError("sugarStock ok status requires stockValue")
            if field == "sugarStock" and "MAPA" not in str(metric.get("sourceName")):
                raise ValueError("sugarStock must use MAPA, not ANP")
            if field == "ethanolStock" and metric.get("totalEthanolStock") is None:
                raise ValueError("ethanolStock ok status requires totalEthanolStock")
    india_metrics = report.get("indiaMetrics")
    if not isinstance(india_metrics, dict):
        raise ValueError("Dashboard missing indiaMetrics")
    for field in ("domesticSugarPrice", "upExMillPrice", "carryoverStock"):
        metric = india_metrics.get(field)
        if not isinstance(metric, dict):
            raise ValueError(f"indiaMetrics missing {field}")
        if metric.get("status") not in {"ok", "pending", "stale"}:
            raise ValueError(f"indiaMetrics {field} has invalid status")
        if metric.get("status") == "ok":
            if field == "carryoverStock" and metric.get("stockWanTonnes") is None:
                raise ValueError("carryoverStock ok status requires stockWanTonnes")
            if field != "carryoverStock" and metric.get("priceInrPerQuintal") is None and not metric.get("rangeInrPerQuintal"):
                raise ValueError(f"{field} ok status requires price or range")

    group_positions = []
    for row in excel_rows:
        if row["country"] == "巴西":
            group_positions.append(0)
        elif row["country"] == "印度":
            group_positions.append(1)
        elif row["country"] == "泰国":
            group_positions.append(2)
        elif row["country"] == "中国":
            group_positions.append(3)
        else:
            group_positions.append(4)
    checks = {
        "verified_count": len(items),
        "excel_count": len(excel_rows),
        "dashboard_count": dashboard_count,
        "excel_matches_verified": True,
        "dashboard_matches_verified": True,
        "country_order_ok": group_positions == sorted(group_positions),
        "no_empty_country_sections": True,
        "counts_by_country": dict(Counter(item["country"] for item in items)),
        "china_count": expected_china,
        "other_country_count": sum(1 for item in items if item["country_group"] == "其他国家"),
    }
    return checks


def write_status(date_text: str, status: str, details: dict, error: str | None = None) -> None:
    path = public_status_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "latestNewsDate": date_text if status == "success" else None,
        "lastRunAt": beijing_now().isoformat(timespec="seconds"),
        "lastRunStatus": status,
        "timezone": "Asia/Shanghai",
        "details": details,
    }
    if error:
        payload["error"] = error[:1000]
    if path.exists() and status != "success":
        with path.open("r", encoding="utf-8") as f:
            old = json.load(f)
        payload["latestNewsDate"] = old.get("latestNewsDate")
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def write_task_log(task_root: Path, date_text: str, payload: dict) -> None:
    path = write_log_path(task_root, date_text)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def refresh_india_metrics(date_text: str) -> dict:
    try:
        from india_sugar_metrics import collect
        snapshot = collect(date_text)
        return {"status": "success", "snapshotUpdatedAt": snapshot.get("updatedAt")}
    except Exception as exc:
        return {"status": "failed", "error": str(exc)[:1000]}


def refresh_brazil_metrics(date_text: str) -> dict:
    try:
        from brazil_sugar_metrics import collect
        snapshot = collect(date_text)
        return {"status": "success", "snapshotUpdatedAt": snapshot.get("updatedAt")}
    except Exception as exc:
        return {"status": "failed", "error": str(exc)[:1000]}


def main() -> int:
    args = parse_args()
    date_text = target_date(args.date)
    task_root = task_root_from_args(args.task_root)
    ensure_task_dirs(task_root, date_text)

    if args.skip_if_success and success_exists(date_text):
        print(json.dumps({"status": "skipped", "reason": "already_success", "newsDate": date_text}, ensure_ascii=False))
        return 0

    try:
        brazil_metrics_refresh = refresh_brazil_metrics(date_text)
        india_metrics_refresh = refresh_india_metrics(date_text)
        data = load_verified_or_fail(task_root, date_text, offline_only=args.offline_only, allow_rss_autogen=args.allow_rss_autogen)
        items = normalize_items(data)
        excel_file = write_excel(task_root, date_text, items)
        payload = build_dashboard_payload(date_text, items, excel_file, data)
        report_path, index_path = write_dashboard_data(date_text, payload)
        checks = validate_all(date_text, items, excel_file, report_path, index_path)
        log_payload = {
            "target_date": date_text,
            "generated_at": beijing_now().isoformat(timespec="seconds"),
            "status": "success",
            "verified_news_file": str(verified_json_path(task_root, date_text)),
            "excel_file": str(excel_file),
            "dashboard_report": str(report_path),
            "dashboard_index": str(index_path),
            "brazil_metrics_refresh": brazil_metrics_refresh,
            "india_metrics_refresh": india_metrics_refresh,
            "checks": checks,
        }
        write_task_log(task_root, date_text, log_payload)
        write_status(date_text, "success", checks)
        print(json.dumps(log_payload, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        error = str(exc)
        details = {"target_date": date_text, "task_root": str(task_root)}
        write_task_log(task_root, date_text, {
            "target_date": date_text,
            "generated_at": beijing_now().isoformat(timespec="seconds"),
            "status": "failed",
            "error": error,
        })
        write_status(date_text, "failed", details, error=error)
        print(json.dumps({"status": "failed", "newsDate": date_text, "error": error}, ensure_ascii=False, indent=2), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
