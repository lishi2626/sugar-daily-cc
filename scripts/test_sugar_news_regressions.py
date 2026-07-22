from __future__ import annotations

import json
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from openpyxl import load_workbook

from sugar_news_pipeline import COUNTRY_SEARCH_TEMPLATES, is_india_indirect_sugar_relevant


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TASK_ROOT = PROJECT_ROOT.parent / "Sugar News"
TARGET_DATE = "2026-07-20"
REQUIRED_DEDUPE_KEYS = {
    "india_aista_sugar_supply_shortage_unwarranted_20260720",
    "india_no_plan_ethanol_blend_above_e20_20260720",
    "india_cane_states_monsoon_heavy_rain_forecast_20260720",
}


def read_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def test_india_relevance_helpers() -> None:
    assert is_india_indirect_sugar_relevant(
        "India says no plan now to raise ethanol mix in gasoline above 20%; "
        "maize accounts for 37% of ethanol feedstock while molasses and sugar syrup remain cane-based inputs."
    )
    assert is_india_indirect_sugar_relevant(
        "OMC ethanol procurement tender includes B-heavy molasses, C-heavy molasses and sugar syrup supplies."
    )


def test_india_search_templates_cover_e20_reuters() -> None:
    queries = "\n".join(template for _language, template in COUNTRY_SEARCH_TEMPLATES["印度"])
    for expected in (
        "India E20 petrol",
        "India ethanol above 20 percent",
        "India grain ethanol",
        "India maize ethanol",
        "India OMC ethanol tender",
        "site:reuters.com India E20",
        "site:reuters.com India ethanol",
        "site:reuters.com India molasses",
    ):
        assert expected in queries


def test_no_fixed_country_cap_in_autogen() -> None:
    source = (PROJECT_ROOT / "scripts" / "sugar_news_pipeline.py").read_text(encoding="utf-8")
    assert "retained_for_country >= 2" not in source


def test_ist_utc_beijing_date_handling() -> None:
    try:
        ist = ZoneInfo("Asia/Kolkata")
    except Exception:
        ist = timezone(timedelta(hours=5, minutes=30), name="Asia/Kolkata")
    try:
        shanghai = ZoneInfo("Asia/Shanghai")
    except Exception:
        shanghai = timezone(timedelta(hours=8), name="Asia/Shanghai")
    local_article_time = datetime(2026, 7, 20, 0, 30, tzinfo=ist)
    assert local_article_time.date().isoformat() == TARGET_DATE
    assert local_article_time.astimezone(timezone.utc).date().isoformat() == "2026-07-19"
    assert datetime(2026, 7, 21, 6, 0, tzinfo=shanghai).date().isoformat() == "2026-07-21"


def test_verified_news_contains_required_india_items() -> None:
    data = read_json(TASK_ROOT / "data" / "verified_news" / "2026" / "07" / f"sugar_news_{TARGET_DATE}.json")
    keys = {item.get("dedupe_key") for item in data["items"]}
    assert REQUIRED_DEDUPE_KEYS <= keys
    country_counts = Counter(item.get("country_group") for item in data["items"])
    assert country_counts["印度"] >= 4


def test_excel_dashboard_consistency() -> None:
    report = read_json(PROJECT_ROOT / "public" / "sugar-news" / "data" / "reports" / "2026" / "07" / f"{TARGET_DATE}.json")
    excel_path = TASK_ROOT / "reports" / "2026" / "07" / f"Sugar News {TARGET_DATE}.xlsx"
    workbook = load_workbook(excel_path)
    sheet = workbook.active
    excel_rows = [
        (row[0], row[1], row[2])
        for row in sheet.iter_rows(min_row=2, values_only=True)
        if row[0] and row[1]
    ]
    dashboard_rows = [
        (country["country"], item["news"], f'{item["impactType"]}：{item["impact"]}')
        for country in report["countries"]
        for item in country["items"]
    ]
    assert len(excel_rows) == len(dashboard_rows)
    assert Counter(row[0] for row in excel_rows)["印度"] >= 4
    dashboard_text = "\n".join(news for _country, news, _impact in dashboard_rows)
    for phrase in ("AISTA", "E20以上", "Hardoi和Unnao"):
        assert phrase in dashboard_text


def test_india_metrics_price_changes_and_stock_source_rules() -> None:
    report = read_json(PROJECT_ROOT / "public" / "sugar-news" / "data" / "reports" / "2026" / "07" / f"{TARGET_DATE}.json")
    metrics = report["indiaMetrics"]
    for field in ("domesticWholesalePrice", "domesticRetailPrice", "upExMillPrice"):
        metric = metrics[field]
        assert metric["status"] == "ok"
        assert metric["previousDataDate"]
        assert metric["changePct"] is not None
        if field == "domesticRetailPrice":
            assert metric["changeInrPerKg"] is not None
        else:
            assert metric["changeInrPerQuintal"] is not None
        if field == "upExMillPrice":
            low = metric["rangeInrPerQuintal"]["low"]
            high = metric["rangeInrPerQuintal"]["high"]
            assert metric["midpointInrPerQuintal"] == (low + high) / 2

    stock = metrics["carryoverStock"]
    if stock["status"] == "ok":
        source = stock.get("organization") or stock.get("sourceName") or ""
        assert any(token in source for token in ("Government of India", "Department of Food", "ISMA", "NFCSF", "印度政府"))
    for forecast in metrics.get("carryoverStockForecasts", []):
        assert forecast.get("sourceTier") == "market_forecast_comparison_only"


def main() -> None:
    tests = [
        test_india_relevance_helpers,
        test_india_search_templates_cover_e20_reuters,
        test_no_fixed_country_cap_in_autogen,
        test_ist_utc_beijing_date_handling,
        test_verified_news_contains_required_india_items,
        test_excel_dashboard_consistency,
        test_india_metrics_price_changes_and_stock_source_rules,
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"FAIL {exc}", file=sys.stderr)
        raise
