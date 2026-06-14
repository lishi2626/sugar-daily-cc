#!/usr/bin/env python3
from __future__ import annotations
"""
数据台账CSV管理模块 — sugar_daily_data.csv 的唯一读写入口。
所有每日数据统一存储在此CSV，不再使用 verified_fundamentals.json 或 raw/ 目录。

record_key = country + region + indicator + data_date + season (slugified)
更新规则: 同 key 则更新，不同值标记 conflict，失败不写入虚假数据。
"""

import csv
import re
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CSV_PATH = PROJECT_ROOT / "data" / "sugar_daily_data.csv"

FIELDS = [
    "record_key", "category", "country", "region", "indicator",
    "value", "text_value", "unit", "data_date", "published_at",
    "source_name", "source_url", "source_status", "source_channel",
    "official_level", "data_type", "season", "status", "fetched_at", "used_in_report",
    "period_start", "period_end", "period_type", "table_number",
    "coverage_scope", "impact_direction", "confidence",
    "attribution", "sugar_mix_yoy_change_pp",
    "institution", "view_season", "view_direction", "surplus_deficit_value",
    "comparison_date", "comparison_type", "previous_value", "current_value",
]

logger = logging.getLogger("update_data_csv")


def beijing_now() -> datetime:
    return datetime.now(timezone(timedelta(hours=8)))


def make_key(country: str, region: str, indicator: str, data_date: str, season: str = "") -> str:
    """生成唯一 record_key。"""
    parts = [country, region, indicator, data_date]
    if season:
        parts.append(season)
    key = "_".join(str(p).strip().replace(" ", "_").replace("/", "_") for p in parts if p)
    key = re.sub(r"[^\w\-_]", "", key)
    return key.lower()


def _ensure_csv():
    """确保CSV文件存在且有表头。"""
    if not CSV_PATH.exists():
        CSV_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(CSV_PATH, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=FIELDS)
            writer.writeheader()


def read_all_rows() -> list[dict]:
    """读取所有行。"""
    _ensure_csv()
    with open(CSV_PATH, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        return list(reader)


def read_valid_rows(category: str | None = None, country: str | None = None) -> list[dict]:
    """读取可入报告工作流的行，可按 category/country 过滤。"""
    rows = read_all_rows()
    report_statuses = {
        "valid", "fresh", "valid_cached", "no_new_data",
        "not_published", "needs_verification",
    }
    valid = [r for r in rows if r.get("status", "") in report_statuses]
    if category:
        valid = [r for r in valid if r.get("category", "") == category]
    if country:
        valid = [r for r in valid if r.get("country", "") == country]
    return valid


def get_latest_by_indicator(indicator: str, country: str = "", region: str = "") -> dict | None:
    """获取某个指标最新一条有效记录。"""
    rows = read_valid_rows()
    candidates = [
        r for r in rows
        if r.get("indicator", "") == indicator
        and (not country or r.get("country", "") == country)
        and (not region or r.get("region", "") == region)
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda r: r.get("data_date", ""), reverse=True)
    return candidates[0]


def upsert_rows(new_rows: list[dict]) -> tuple[int, int, list[str]]:
    """
    插入或更新行。返回 (inserted, updated, conflicts)。
    同 record_key → 更新；值不同 → 标记 conflict。
    """
    _ensure_csv()
    existing = read_all_rows()
    existing_map = {r.get("record_key", ""): i for i, r in enumerate(existing)}
    now_str = beijing_now().strftime("%Y-%m-%d %H:%M:%S")

    inserted = 0
    updated = 0
    conflicts = []

    for row in new_rows:
        key = row.get("record_key", "")
        if not key:
            continue
        if row.get("status") == "superseded":
            if key in existing_map:
                existing.pop(existing_map[key])
                existing_map = {r.get("record_key", ""): i for i, r in enumerate(existing)}
                updated += 1
            continue
        row["fetched_at"] = now_str
        # 确保字段齐全
        for f in FIELDS:
            if f not in row:
                row[f] = ""

        if key in existing_map:
            old = existing[existing_map[key]]
            old_val = old.get("value", "") or old.get("text_value", "")
            new_val = row.get("value", "") or row.get("text_value", "")
            old_source = old.get("source_name", "")
            new_source = row.get("source_name", "")
            # 值相同且来源/状态相同 → 跳过
            if (str(old_val) == str(new_val) and old_source == new_source
                    and old.get("status") == row.get("status")):
                continue
            # 值不同但来源相同，或从本地备用CSV升级为外部公开来源 → 更新
            if old_source == new_source or old_source == "本地备用CSV":
                existing[existing_map[key]] = row
                updated += 1
            else:
                # 不同来源不同值 → conflict
                row["status"] = "conflict"
                existing[existing_map[key]] = row
                conflicts.append(key)
                updated += 1
        else:
            existing.append(row)
            existing_map[key] = len(existing) - 1
            inserted += 1

    # 写回
    with open(CSV_PATH, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(existing)

    logger.info("CSV更新: inserted=%d updated=%d conflicts=%d", inserted, updated, len(conflicts))
    return inserted, updated, conflicts


def csv_row(**kwargs) -> dict:
    """构造一行标准化数据。"""
    row = {f: "" for f in FIELDS}
    row.update(kwargs)
    if not row.get("record_key"):
        row["record_key"] = make_key(
            row.get("country", ""), row.get("region", ""),
            row.get("indicator", ""), row.get("data_date", ""),
            row.get("season", ""))
    return row


def cleanup_old_records():
    """清理过期记录。"""
    rows = read_all_rows()
    now = beijing_now().replace(tzinfo=None)
    kept = []
    removed = 0
    for r in rows:
        cat = r.get("category", "")
        data_date = r.get("data_date", "")
        try:
            dd = datetime.strptime(data_date, "%Y-%m-%d")
            age = (now - dd).days
        except ValueError:
            kept.append(r)
            continue

        if cat == "market" and age > 180:
            removed += 1; continue
        if r.get("data_type") == "weather" and age > 90:
            removed += 1; continue
        if cat in ("international", "domestic") and age > 730:
            removed += 1; continue
        kept.append(r)

    if removed:
        with open(CSV_PATH, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=FIELDS)
            writer.writeheader()
            writer.writerows(kept)
        logger.info("CSV清理: 删除 %d 条过期记录", removed)
