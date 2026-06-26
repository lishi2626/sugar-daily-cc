#!/usr/bin/env python3
from __future__ import annotations

"""Shared Sugar Daily market-performance rules.

Both the daily report and the interactive dashboard must call this module for
market-performance values. The dashboard fetches data independently; it must
not read generated Sugar Daily report JSON/Markdown as a data source.
"""

from datetime import datetime, timezone, timedelta
from typing import Any

from fetch_market import collect_market_data


def beijing_now() -> datetime:
    return datetime.now(timezone(timedelta(hours=8)))


def fv(field: dict | None, fmt_spec: str = ".2f") -> str:
    if field is None or field.get("value") is None:
        return "N/A"
    try:
        return format(float(field["value"]), fmt_spec)
    except (ValueError, TypeError):
        return str(field["value"])


def fv_int(field: dict | None) -> str:
    return fv(field, ".0f")


def daily_change_phrase(change_pct: float) -> str:
    if change_pct > 0:
        return f"日涨幅{abs(change_pct):.2f}%"
    if change_pct < 0:
        return f"日跌幅{abs(change_pct):.2f}%"
    return "日涨跌幅0.00%"


def build_market_summary(market: dict) -> str:
    """Build the exact report-style market-performance sentence."""
    zz_display = market.get("zz_display_name", "郑糖主力合约")
    zz_close = fv_int(market.get("zz_close"))
    zz_chg = float(fv(market.get("zz_change_pct"), ".2f").replace("N/A", "0"))
    parts = [f"{zz_display}收{zz_close}元/吨，{daily_change_phrase(zz_chg)}。"]

    ice_display = market.get("ice_display_name", "ICE原糖主力合约")
    ice_close_val = fv(market.get("ice_close"), ".2f")
    if ice_close_val != "N/A":
        ice_chg = float(fv(market.get("ice_change_pct"), ".2f").replace("N/A", "0"))
        parts.append(f"{ice_display}收{ice_close_val}美分/磅，{daily_change_phrase(ice_chg)}。")

    basis_val_str = fv_int(market.get("basis"))
    if basis_val_str != "N/A":
        parts.append(f"广西白糖现货与{zz_display}基差为{basis_val_str}元/吨。")

    brazil_field = market.get("brazil_profit", {})
    brazil_val = fv_int(brazil_field)
    if brazil_val != "N/A":
        parts.append(f"配额外巴西糖加工完税估算利润为{brazil_val}元/吨。")

    return " ".join(parts)


def _num(field: dict | None) -> float | None:
    if not field or field.get("value") is None:
        return None
    try:
        return float(field["value"])
    except (TypeError, ValueError):
        return None


def _field_date(field: dict | None, fallback: str = "") -> str:
    if not field:
        return fallback
    return str(field.get("data_date") or fallback)


def _field_source(field: dict | None) -> str:
    if not field:
        return ""
    return str(field.get("source_name") or "")


def _field_url(field: dict | None) -> str:
    if not field:
        return ""
    return str(field.get("source_url") or "")


def _fallback_info(field: dict | None, target_date: str | None, errors: list[str]) -> tuple[bool, str]:
    data_date = _field_date(field)
    fallback_used = bool(target_date and data_date and data_date != target_date)
    reasons = []
    if fallback_used:
        reasons.append(f"target_date={target_date}, data_date={data_date}")
    if errors:
        reasons.extend(errors)
    return fallback_used, "; ".join(reasons)


def build_market_performance_items(market: dict, target_date: str | None = None) -> list[dict[str, Any]]:
    """Build the three dashboard KPI items using Sugar Daily field rules."""
    errors = [str(item) for item in market.get("errors", []) if item]
    trade_date = str(market.get("trade_date") or "")

    zz_close = market.get("zz_close")
    zz_change = market.get("zz_change_pct")
    zz_value = _num(zz_close)
    zz_change_value = _num(zz_change)
    zz_fallback, zz_reason = _fallback_info(zz_close, target_date, errors)

    ice_close = market.get("ice_close")
    ice_change = market.get("ice_change_pct")
    ice_value = _num(ice_close)
    ice_change_value = _num(ice_change)
    ice_fallback, ice_reason = _fallback_info(ice_close, target_date, errors)

    profit = market.get("brazil_profit")
    profit_value = _num(profit)
    profit_fallback, profit_reason = _fallback_info(profit, target_date, errors)

    return [
        {
            "name": market.get("zz_display_name", "郑糖主力合约"),
            "value": zz_value,
            "unit": "元/吨",
            "changePct": zz_change_value,
            "displayValue": "N/A" if zz_value is None else f"{zz_value:.0f}元/吨",
            "displayChange": "" if zz_change_value is None else daily_change_phrase(zz_change_value),
            "dataDate": _field_date(zz_close, trade_date),
            "source": _field_source(zz_close),
            "sourceUrl": _field_url(zz_close),
            "fallbackUsed": zz_fallback,
            "fallbackReason": zz_reason,
        },
        {
            "name": market.get("ice_display_name", "ICE原糖主力合约"),
            "value": ice_value,
            "unit": "美分/磅",
            "changePct": ice_change_value,
            "displayValue": "N/A" if ice_value is None else f"{ice_value:.2f}美分/磅",
            "displayChange": "" if ice_change_value is None else daily_change_phrase(ice_change_value),
            "dataDate": _field_date(ice_close, trade_date),
            "source": _field_source(ice_close),
            "sourceUrl": _field_url(ice_close),
            "fallbackUsed": ice_fallback,
            "fallbackReason": ice_reason,
        },
        {
            "name": "配额外巴西糖加工完税估算利润",
            "value": profit_value,
            "unit": "元/吨",
            "displayValue": "N/A" if profit_value is None else f"{profit_value:.0f}元/吨",
            "displayChange": "",
            "dataDate": _field_date(profit, trade_date),
            "source": _field_source(profit),
            "sourceUrl": _field_url(profit),
            "fallbackUsed": profit_fallback,
            "fallbackReason": profit_reason,
        },
    ]


def build_market_performance_payload(
    target_date: str | None = None,
    *,
    fetch_time: str | None = None,
    trigger: str = "dashboard",
) -> dict[str, Any]:
    """Fetch market performance independently with Sugar Daily rules."""
    market = collect_market_data(target_date)
    now_text = fetch_time or beijing_now().strftime("%Y-%m-%d %H:%M:%S")
    if not market.get("ok"):
        raise RuntimeError("; ".join(str(item) for item in market.get("errors", [])) or "market data unavailable")

    items = build_market_performance_items(market, target_date)
    if len(items) != 3:
        raise RuntimeError(f"marketPerformance must contain 3 items, got {len(items)}")
    missing = [item["name"] for item in items if item.get("value") is None]
    if missing:
        raise RuntimeError("marketPerformance has empty values: " + ", ".join(missing))

    dates = [str(item.get("dataDate") or "") for item in items if item.get("dataDate")]
    data_date = max(dates) if dates else str(market.get("trade_date") or "")
    return {
        "fetchTime": now_text,
        "dataDate": data_date,
        "tradeDate": str(market.get("trade_date") or ""),
        "ruleSource": "same_as_sugar_daily_market_performance",
        "trigger": trigger,
        "rawText": build_market_summary(market),
        "items": items,
        "fallbackUsed": any(bool(item.get("fallbackUsed")) for item in items),
        "fallbackReasons": [item.get("fallbackReason", "") for item in items if item.get("fallbackReason")],
    }
