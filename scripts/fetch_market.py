#!/usr/bin/env python3
from __future__ import annotations
"""
市场数据获取模块。
日报市场表现使用展示合约:
  - 郑州白糖 SR: 新浪 SR0（郑糖主力合约）
  - ICE 原糖: 新浪 RS（ICE原糖主力合约）

SR2609 仍用于交易观点/基本面判断的目标合约，不再用于市场表现取价。

数据流:
  1. 新浪 SR0 / RS → 行情主来源
  2. 泛糖科技 → 巴西糖进口利润
  3. 本地备用 CSV → 现货、基差补充；网络失败时可补行情

用法:
  python -m scripts.fetch_market
  python -m scripts.fetch_market --date 2026-06-13
"""

import argparse
import csv
import json
import logging
import re
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen
from urllib.error import URLError

import yaml

# ── 路径 ────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

logger = logging.getLogger("fetch_market")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

with open(PROJECT_ROOT / "config.yaml", "r", encoding="utf-8") as f:
    config = yaml.safe_load(f)

TARGET = config["target_contract"]
TARGET_CODE = TARGET["code"]  # SR2609
TARGET_YEAR = TARGET["year"]   # 2026
TARGET_MONTH = TARGET["month"] # 09

REJECT_PATTERNS = [
    r"^SR0$",        # 连续合约
    r"^SR\d{2}$",    # SR09 等两位数年份（旧格式）
    r"^SR\d{3}$",    # SR509 等三位数
    r"^SR(?:19|20|21|22|23|24|25)\d{2}$",  # 2025及以前年份
]


def beijing_now() -> datetime:
    return datetime.now(timezone(timedelta(hours=8)))


def is_weekend(date_str: str) -> bool:
    """检查日期是否为周六或周日。"""
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d")
        return d.weekday() >= 5  # 5=周六, 6=周日
    except ValueError:
        return False


def is_valid_trading_date(date_str: str) -> bool:
    """行情日期必须是真实交易日（非周末）。"""
    if not date_str:
        return False
    if is_weekend(date_str):
        return False
    return True


def is_recent_enough(date_str: str, target_date: str | None, max_age_days: int | None = None) -> bool:
    if not date_str or not target_date:
        return True
    try:
        td = datetime.strptime(date_str, "%Y-%m-%d")
        tgt = datetime.strptime(target_date, "%Y-%m-%d")
    except ValueError:
        return False
    if max_age_days is None:
        max_age_days = config["freshness"]["market_days"]
    return abs((tgt - td).days) <= max_age_days


def validate_contract(contract_code: str) -> tuple[bool, str]:
    """
    校验合约代码是否为 SR2609。
    返回 (is_valid, reason)。
    """
    code = str(contract_code).strip().upper()

    # 精确匹配
    if code == TARGET_CODE:
        return True, ""

    # 拒绝已知无效格式
    for pattern in REJECT_PATTERNS:
        if re.match(pattern, code):
            return False, f"合约 {code} 已过期或格式无效，当前目标合约仅接受 {TARGET_CODE}"

    # 不在拒绝列表中但也不是目标合约
    return False, f"合约 {code} 不等于目标合约 {TARGET_CODE}"


def make_field(value: Any, data_date: str, source_name: str, source_url: str) -> dict:
    return {
        "value": value,
        "data_date": str(data_date),
        "source_name": source_name,
        "source_url": source_url,
        "fetched_at": beijing_now().strftime("%Y-%m-%d %H:%M:%S"),
    }


# ============================================================
# 新浪财经 API（仅尝试 SR2609）
# ============================================================

SINA_HEADERS = {
    "User-Agent": config["market_data"].get("user_agent", "Mozilla/5.0"),
    "Referer": "https://finance.sina.com.cn",
}

# SR0 和旧合约黑名单 — 即使新浪返回也不使用
FORBIDDEN_CONTRACTS = {
    "白糖连续", "白糖主连", "SR0", "SR主连",
    "SR09", "SR509", "SR2509", "SR2409",
    "SR07", "SR507", "SR2507", "SR2407",
    "SR01", "SR501", "SR2501", "SR2401",
}
FORBIDDEN_CONTRACT_CODES = {"SR0", "SR09", "SR509", "SR2509", "SR2409", "SR2507", "SR2407", "SR2501", "SR2401",
                            "SR07", "SR507", "SR01", "SR501"}


def http_get(url: str, timeout: int = 15) -> str | None:
    req = Request(url, headers=SINA_HEADERS)
    for attempt in range(config["market_data"].get("max_retries", 2) + 1):
        if attempt > 0:
            time.sleep(2)
        try:
            with urlopen(req, timeout=timeout) as resp:
                return resp.read().decode("gbk", errors="replace")
        except (URLError, Exception) as e:
            logger.warning("请求失败 (尝试 %d): %s — %s", attempt + 1, url, e)
    return None


def parse_sina_domestic(raw_text: str, symbol: str) -> dict | None:
    """解析新浪国内期货响应，只接受 SR2609。"""
    match = re.search(r'"([^"]*)"', raw_text)
    if not match:
        return None
    fields = match.group(1).split(",")
    if len(fields) < 10:
        return None

    name = fields[0].strip()

    # ── 检查合约名是否在禁止列表中 ──
    if name in FORBIDDEN_CONTRACTS:
        logger.warning("新浪返回合约'%s'在禁止列表中，拒绝使用", name)
        return None

    # ── 从合约名提取代码并校验 ──
    code, month = _extract_contract(name, symbol)

    if code in FORBIDDEN_CONTRACT_CODES:
        logger.warning("提取的合约代码 %s 在禁止列表中，拒绝使用", code)
        return None

    is_valid, reason = validate_contract(code)
    if not is_valid:
        logger.warning("新浪返回合约 %s (%s): %s", name, code, reason)
        return None

    data = {
        "contract_name": name,
        "contract_code": code,
        "contract_month": month,
        "prev_close": _to_float(fields[2]),
        "close": _to_float(fields[3]),
        "high": _to_float(fields[4]),
        "low": _to_float(fields[5]),
        "open": _to_float(fields[1]),
        "_symbol": symbol,
    }

    # 提取日期（字段17）
    if len(fields) > 17:
        candidate = fields[17].strip()
        if re.match(r"\d{4}-\d{2}-\d{2}", candidate):
            data["trade_date"] = candidate

    # 如果交易日期缺失或解析失败
    if "trade_date" not in data or not data["trade_date"]:
        for fld in reversed(fields):
            fld = fld.strip()
            if re.match(r"\d{4}-\d{2}-\d{2}", fld):
                data["trade_date"] = fld
                break

    return data


def _extract_contract(name: str, symbol: str) -> tuple[str, str]:
    """从合约名称提取代码和月份。"""
    name = str(name).strip()

    # SR2609 格式
    match = re.match(r"([A-Za-z]+)(\d{2,4})", name)
    if match:
        code = match.group(1).upper()
        month = match.group(2)
        if len(month) == 4:  # 2609 → 月份取后2位
            month = month[2:]
        return code, month

    # 中文名从 symbol 推断
    if re.search(r"[一-鿿]", name):
        sym_match = re.match(r"([A-Za-z]+)", symbol)
        code = sym_match.group(1).upper() if sym_match else "SR"
        return code, ""

    return name, ""


def _to_float(s: str) -> float:
    try:
        return float(s.strip())
    except (ValueError, TypeError):
        return 0.0


def _try_float(val) -> float | None:
    """尝试转换为 float，失败返回 None。"""
    if val is None:
        return None
    s = str(val).strip()
    if not s:
        return None
    try:
        return float(s)
    except (ValueError, TypeError):
        return None


def fetch_from_sina(target_date: str | None = None) -> dict:
    """
    仅从新浪尝试获取 SR2609 数据。
    不尝试 SR0 或任何其他合约。
    """
    sina_cfg = config["market_data"]["sources"]["sina_finance"]
    if not sina_cfg.get("enabled", True):
        return {"ok": False, "data": None, "errors": ["新浪数据源已禁用"]}

    zz_url = sina_cfg.get("zhengzhou_sugar_url", "")
    if not zz_url:
        return {"ok": False, "data": None, "errors": ["未配置新浪URL"]}

    timeout = sina_cfg.get("timeout", 15)
    raw = http_get(zz_url, timeout)

    if not raw:
        return {"ok": False, "data": None, "errors": [f"新浪 {TARGET_CODE} 接口无响应"]}

    if len(raw) < 50 or '=""' in raw:
        return {"ok": False, "data": None, "errors": [f"新浪 {TARGET_CODE} 数据不可用（合约可能尚未挂牌）"]}

    data = parse_sina_domestic(raw, symbol=TARGET_CODE)
    if not data:
        return {"ok": False, "data": None, "errors": [f"新浪 {TARGET_CODE} 解析失败或被拒绝"]}

    # 校验交易日期
    trade_date = data.get("trade_date", "")
    if not trade_date:
        return {"ok": False, "data": None, "errors": [f"新浪 {TARGET_CODE} 响应中未找到交易日期"]}

    if not is_valid_trading_date(trade_date):
        return {"ok": False, "data": None, "errors": [f"新浪 {TARGET_CODE} 交易日期 {trade_date} 为周末，无效"]}

    # 检查数据时效
    if target_date:
        try:
            td = datetime.strptime(trade_date, "%Y-%m-%d")
            tgt = datetime.strptime(target_date, "%Y-%m-%d")
            max_age = config["freshness"]["market_days"]
            if abs((tgt - td).days) > max_age:
                return {
                    "ok": False, "data": None,
                    "errors": [f"新浪 {TARGET_CODE} 数据日期 {trade_date} 距今超过 {max_age} 天，数据过旧"]
                }
        except ValueError:
            pass

    logger.info("新浪 %s 数据获取成功, 交易日: %s", TARGET_CODE, trade_date)
    return {
        "ok": True,
        "data": data,
        "errors": [],
        "_source_label": sina_cfg["name"],
        "_source_url": zz_url,
    }


# ============================================================
# 本地 CSV 备用
# ============================================================

def fetch_from_csv(target_date: str | None = None) -> dict:
    """
    从 inputs/market_fallback.csv 读取行情数据。
    严格校验 contract_code == SR2609 和交易日期有效性。
    """
    csv_cfg = config["market_data"]["sources"]["fallback_csv"]
    csv_path = PROJECT_ROOT / csv_cfg["path"]

    if not csv_path.exists():
        return {"ok": False, "data": None, "errors": [f"备用CSV不存在: {csv_path}"]}

    try:
        with open(csv_path, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
    except Exception as e:
        return {"ok": False, "data": None, "errors": [f"读取备用CSV失败: {e}"]}

    if not rows:
        return {"ok": False, "data": None, "errors": ["备用CSV为空"]}

    # 匹配日期
    row = rows[0]
    if target_date:
        for r in rows:
            if r.get("market_date", "").strip() == target_date:
                row = r
                break

    errors = []
    src_label = "本地备用CSV"
    src_url = f"file://{csv_path.resolve()}"

    # ── 校验 contract_code ──
    contract_code = row.get("contract_code", "").strip()
    is_valid, reason = validate_contract(contract_code)
    if not is_valid:
        return {"ok": False, "data": None, "errors": [f"CSV合约校验失败: {reason}"]}

    # ── 校验交易日期 ──
    market_date = row.get("market_date", "").strip()
    if not market_date:
        errors.append("CSV中 market_date 为空")
    elif not is_valid_trading_date(market_date):
        errors.append(f"CSV交易日期 {market_date} 为周末，不是真实交易日")

    # 时效检查
    if market_date and target_date:
        try:
            td = datetime.strptime(market_date, "%Y-%m-%d")
            tgt = datetime.strptime(target_date, "%Y-%m-%d")
            max_age = config["freshness"]["market_days"]
            if abs((tgt - td).days) > max_age:
                errors.append(f"CSV数据日期 {market_date} 距今超过 {max_age} 天")
        except ValueError:
            errors.append(f"CSV数据日期格式错误: {market_date}")

    if errors:
        return {"ok": False, "data": None, "errors": errors}

    # ── 构建数据 ──
    data = {
        "contract_code": contract_code,
        "contract_name": contract_code,
        "contract_month": TARGET_MONTH,
        "close": float(row.get("close", 0)),
        "prev_close": float(row.get("prev_close", 0)),
        "trade_date": market_date,
        "open": 0.0,
        "high": 0.0,
        "low": 0.0,
        "_symbol": "CSV",
    }

    # 美糖
    ice_code = row.get("ice_contract", "").strip()
    ice_data = None
    if ice_code and row.get("ice_close"):
        ice_data = {
            "contract_code": ice_code,
            "contract_name": ice_code,
            "close": float(row.get("ice_close", 0)),
            "prev_close": float(row.get("ice_prev_close", 0)),
            "trade_date": market_date,
        }

    extra = {
        "nanning_spot": float(row.get("nanning_spot", 0)),
        "brazil_profit": _try_float(row.get("quota_outside_brazil_profit")),
        "quota_outside_profit_data_date": row.get("quota_outside_profit_data_date", "").strip(),
        "quota_outside_profit_source": row.get("quota_outside_profit_source", "").strip(),
        "quota_outside_profit_reference_spot": row.get("quota_outside_profit_reference_spot", "").strip(),
    }

    logger.info("CSV数据校验通过: %s, 交易日=%s", contract_code, market_date)
    return {
        "ok": True,
        "data": data,
        "ice_data": ice_data,
        "errors": [],
        "_csv_extra": extra,
        "_source_label": src_label,
        "_source_url": src_url,
    }



# ============================================================
# 泛糖科技 — 食糖进口成本及利润估算
# ============================================================

HISUGAR_BASE = "https://www.hisugar.com"


def _fetch_hisugar_page(article_id: str) -> str | None:
    """获取泛糖文章页面 HTML。"""
    url = f"{HISUGAR_BASE}/home/articleContent?id={article_id}"
    try:
        req = Request(url, headers={
            "User-Agent": config["market_data"].get("user_agent", "Mozilla/5.0"),
        })
        with urlopen(req, timeout=15) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        logger.info("泛糖文章请求失败 id=%s: %s", article_id, e)
        return None


def _parse_title_date(raw: str) -> str | None:
    """从页面 <title> 中解析 title_date。不得从文章ID推断日期。"""
    m = re.search(r"<title[^>]*>(.*?)</title>", raw, re.I)
    if not m:
        return None
    title_text = m.group(1).strip()
    tm = re.search(r"(\d{8})食糖进口成本及利润估算", title_text)
    if not tm:
        return None
    s = tm.group(1)
    return f"{s[:4]}-{s[4:6]}-{s[6:8]}"


def _parse_body_date(raw: str) -> str | None:
    """从正文首句解析 body_data_date。格式: '2026年6月11日' → '2026-06-11'"""
    clean = _clean_html(raw)
    m = re.search(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日", clean)
    if not m:
        return None
    return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"


def _parse_published_at(raw: str) -> str:
    """从页面元信息提取发布时间。"""
    for pat in [r"(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2})", r"(\d{4}/\d{2}/\d{2}\s+\d{2}:\d{2})"]:
        m = re.search(pat, raw)
        if m:
            return m.group(1).replace("/", "-")
    return ""


def _clean_html(raw: str) -> str:
    """去除 HTML 标签和脚本，返回纯文本。"""
    c = re.sub(r"<script[^>]*>.*?</script>", "", raw, flags=re.I | re.DOTALL)
    c = re.sub(r"<style[^>]*>.*?</style>", "", c, flags=re.I | re.DOTALL)
    c = re.sub(r"<[^>]+>", " ", c)
    c = re.sub(r"&nbsp;", " ", c)
    return re.sub(r"\s+", " ", c).strip()


def _parse_profit_fields(raw: str) -> dict | None:
    """从 HTML 中解析进口成本和利润字段。返回 dict 或 None。"""
    clean = _clean_html(raw)
    m = re.search(
        r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日[，,]\s*"
        r"ICE原糖主力合约收盘价为\s*([\d.]+)\s*美分[/／]磅[，,]\s*"
        r"人民币汇率为\s*([\d.]+)",
        clean
    )
    if not m:
        return None
    body_date = f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    ice_close = float(m.group(4))
    usd_cny = float(m.group(5))

    cp = re.search(
        r"配额内巴西糖加工完税估算成本\s*([\d.]+)\s*元[/／]吨[，,]\s*"
        r"配额外巴西糖加工完税估算成本为\s*([\d.]+)\s*元[/／]吨[；;]\s*"
        r"与日照白糖现货价比[，,]?\s*"
        r"配额内巴西糖加工完税估算利润为\s*([\d.]+)\s*元[/／]吨[，,]\s*"
        r"配额外巴西糖加工完税估算利润为\s*([\d.]+)\s*元[/／]吨",
        clean
    )
    if not cp:
        cp = re.search(
            r"配额内.*?成本\s*([\d.]+)\s*元[/／]吨.*?"
            r"配额外.*?成本[为]?\s*([\d.]+)\s*元[/／]吨.*?"
            r"配额内.*?利润[为]?\s*([\d.]+)\s*元[/／]吨.*?"
            r"配额外.*?利润[为]?\s*([\d.]+)\s*元[/／]吨",
            clean
        )
        if not cp:
            return None

    return {
        "body_date": body_date,
        "ice_close": ice_close,
        "usd_cny": usd_cny,
        "quota_inside_cost": float(cp.group(1)),
        "quota_outside_cost": float(cp.group(2)),
        "quota_inside_profit": float(cp.group(3)),
        "quota_outside_profit": float(cp.group(4)),
    }


def _discover_hisugar_articles() -> list[dict]:
    """
    从泛糖首页发现所有'食糖进口成本及利润估算'候选文章。
    返回列表 [{article_id, article_title, title_date}...]，按 title_date 降序。
    文章ID不得用于推断日期。
    """
    candidates = []
    try:
        req = Request(f"{HISUGAR_BASE}/home/homeIndex", headers={
            "User-Agent": config["market_data"].get("user_agent", "Mozilla/5.0"),
        })
        with urlopen(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        logger.warning("泛糖首页请求失败: %s", e)
        return candidates

    seen = set()
    for pattern in ["食糖进口成本及利润估算", "食糖进口成本", "进口成本及利润估算"]:
        idx = 0
        while True:
            idx = raw.find(pattern, idx)
            if idx == -1:
                break
            context = raw[max(0, idx - 300): idx + 300]
            for aid in re.findall(r'/home/articleContent\?id=(\d{22,24})', context):
                if aid not in seen:
                    seen.add(aid)
            idx += len(pattern)

    if not seen:
        logger.warning("泛糖首页未发现进口成本相关链接")
        return candidates

    for aid in seen:
        page = _fetch_hisugar_page(aid)
        if not page:
            continue
        title_match = re.search(r"<title[^>]*>(.*?)</title>", page, re.I)
        article_title = title_match.group(1).strip() if title_match else ""
        td = _parse_title_date(page)
        if td is None:
            continue
        candidates.append({"article_id": aid, "article_title": article_title, "title_date": td})

    candidates.sort(key=lambda x: x["title_date"], reverse=True)
    logger.info("泛糖: 发现 %d 篇候选文章, 最新 title_date=%s",
                len(candidates), candidates[0]["title_date"] if candidates else "N/A")
    return candidates


def fetch_hisugar_import_profit(target_date: str | None = None) -> dict | None:
    """
    从泛糖科技获取最新一期'食糖进口成本及利润估算'数据。

    步骤:
      1. 发现候选文章 → 2. 读取真实标题解析title_date →
      3. 按title_date排序选最新 → 4. 打开详情解析body_date和字段 →
      5. 强制校验 title_date == body_date → 6. 通过后返回
    文章ID禁止用于推断任何日期。
    """
    cfg = config.get("hisugar", {}).get("import_profit", {})
    if not cfg.get("enabled", True):
        return None

    candidates = _discover_hisugar_articles()
    if not candidates:
        logger.warning("泛糖: 未找到候选文章")
        return None

    for i, c in enumerate(candidates):
        logger.info("泛糖候选[%d]: id=%s title_date=%s", i, c["article_id"], c["title_date"])

    best = candidates[0]
    article_id = best["article_id"]

    raw = _fetch_hisugar_page(article_id)
    if not raw:
        return None

    title_date = _parse_title_date(raw)
    body_date = _parse_body_date(raw)
    published_at = _parse_published_at(raw)

    logger.info("泛糖选中: id=%s title_date=%s body_date=%s published_at=%s",
                article_id, title_date, body_date, published_at)

    # 强制一致性校验
    if title_date != body_date:
        logger.error("HISUGAR_DATE_MISMATCH: title_date=%s body_date=%s — 拒绝使用", title_date, body_date)
        return None

    if published_at and published_at[:10] < title_date:
        logger.warning("泛糖: published_at=%s 早于 title_date=%s", published_at, title_date)

    fields = _parse_profit_fields(raw)
    if not fields:
        logger.warning("泛糖: 利润字段解析失败 id=%s", article_id)
        save_raw_to_data(title_date.replace("-", ""), "hisugar_parse_fail", _clean_html(raw)[:3000])
        return None

    errors = []
    if fields["quota_outside_profit"] <= 0:
        errors.append(f"配额外利润 {fields['quota_outside_profit']} 无效")
    if fields["ice_close"] <= 0 or fields["ice_close"] > 50:
        errors.append(f"ICE价格 {fields['ice_close']} 异常")
    if "日照" not in _clean_html(raw):
        errors.append("正文未提及日照白糖现货价")

    status = "needs_verification" if errors else "verified"
    if errors:
        logger.warning("泛糖校验: %s", "; ".join(errors))

    save_raw_to_data(title_date.replace("-", ""), "hisugar", _clean_html(raw)[:5000])

    result = {
        "data_date": body_date,
        "title_date": title_date,
        "published_at": published_at,
        "ice_close": fields["ice_close"],
        "usd_cny": fields["usd_cny"],
        "quota_inside_cost": fields["quota_inside_cost"],
        "quota_outside_cost": fields["quota_outside_cost"],
        "quota_inside_profit": fields["quota_inside_profit"],
        "quota_outside_profit": fields["quota_outside_profit"],
        "profit_reference_spot": "日照白糖现货价",
        "source_name": cfg.get("source_name", "泛糖科技"),
        "source_url": f"{HISUGAR_BASE}/home/articleContent?id={article_id}",
        "article_id": article_id,
        "article_title": best["article_title"],
        "status": status,
    }

    logger.info("泛糖结果: title_date=%s ICE=%.2f 汇率=%.4f 配额外利润=%.0f 状态=%s",
                title_date, fields["ice_close"], fields["usd_cny"],
                fields["quota_outside_profit"], status)
    return result


def save_raw_to_data(date_str: str, label: str, content: str):
    """用户要求只维护CSV，不再保存原始响应。"""
    return


# ============================================================
# 数据合并
# ============================================================

# ============================================================
# ICE 原糖 — 新浪财经期货行情页
# ============================================================

def fetch_ice_from_sina() -> dict | None:
    """
    从新浪财经 ICE 原糖页面提取行情。
    https://finance.sina.com.cn/futures/quotes/RS.shtml
    返回 dict 或 None。
    """
    url = config.get("ice", {}).get("primary_url",
                                     "https://finance.sina.com.cn/futures/quotes/RS.shtml")
    try:
        req = Request(url, headers={
            "User-Agent": config["market_data"].get("user_agent", "Mozilla/5.0"),
        })
        with urlopen(req, timeout=20) as resp:
            raw = resp.read().decode("gbk", errors="replace")
    except Exception as e:
        logger.info("新浪ICE页面请求失败: %s", e)
        return None

    result = {"source_name": "新浪财经", "source_url": url}

    # 提取合约名称
    name_m = re.search(r'"name"\s*:\s*"([^"]+)"', raw)
    if name_m:
        result["contract_name"] = name_m.group(1)

    # 提取最新价
    for pat in [r'"lastPrice"\s*:\s*"([\d.]+)"',
                r'"price"\s*:\s*"([\d.]+)"',
                r'last\s*[=:]\s*[\'"]?([\d.]+)']:
        m = re.search(pat, raw, re.I)
        if m:
            result["close"] = float(m.group(1))
            break

    # 提取前收盘价
    for pat in [r'"preClose"\s*:\s*"([\d.]+)"',
                r'"prevclose"\s*:\s*"([\d.]+)"',
                r'"yclose"\s*:\s*"([\d.]+)"']:
        m = re.search(pat, raw, re.I)
        if m:
            result["prev_close"] = float(m.group(1))
            break

    if "close" not in result:
        logger.info("新浪ICE: 未提取到价格")
        return None

    logger.info("新浪ICE: contract=%s close=%s",
                result.get("contract_name", "?"),
                result.get("close", "?"))
    return result


# ============================================================
# 郑糖主力和ICE主力行情（展示用）
# ============================================================

def fetch_sr0_display() -> dict | None:
    """
    从新浪获取 SR0（郑糖连续/主力）行情，仅用于展示。
    返回的 display_name 固定为'郑糖主力合约'，不写 SR2609。
    """
    cfg = config["market_data"]["sources"]["sina_finance"]
    url = cfg.get("sr0_url", "http://hq.sinajs.cn/list=SR0")
    raw = http_get(url, cfg.get("timeout", 15))
    if not raw or len(raw) < 50:
        return None

    match = re.search(r'"([^"]*)"', raw)
    if not match:
        return None
    fields = match.group(1).split(",")
    if len(fields) < 6:
        return None

    if "nf_SR0" in url or (len(fields) > 17 and fields[1].isdigit()):
        # 新浪期货页 SR0.shtml 对应的实时接口为 nf_SR0。
        # 字段8为最新/收盘展示价，字段10为昨结/前值口径。
        close = _to_float(fields[8])
        prev = _to_float(fields[10])
    else:
        close = _to_float(fields[3])
        prev = _to_float(fields[2])
    chg_pct = ((close - prev) / prev * 100) if prev != 0 else 0.0
    trade_date = ""
    for idx in [17, -1]:
        try:
            d = fields[idx].strip()
            if re.match(r"\d{4}-\d{2}-\d{2}", d):
                trade_date = d
                break
        except IndexError:
            continue

    display_cfg = config["display_contract"]["zhengzhou"]
    src_name = display_cfg["source_name"]
    result = {
        "contract_code": "SR0",
        "display_name": display_cfg["display_name"],
        "close": round(close, 2),
        "prev_close": round(prev, 2),
        "change_pct": round(chg_pct, 2),
        "trade_date": trade_date,
        "source_name": src_name,
        "source_url": display_cfg.get("source_url", cfg.get("sr0_url", "")),
        "raw_url": cfg.get("sr0_url", ""),
        "_is_display_only": True,
        "_note": "SR0为连续主力行情，不等于SR2609",
    }
    logger.info("SR0展示行情: close=%.2f chg=%.2f%% date=%s", close, chg_pct, trade_date)
    return result


def fetch_rs_display() -> dict | None:
    """
    从新浪获取 RS（ICE原糖连续/主力）行情，仅用于展示。
    返回的 display_name 固定为'ICE原糖主力合约'。
    """
    cfg = config["market_data"]["sources"]["sina_finance"]
    url = cfg.get("rs_url", "http://hq.sinajs.cn/list=RS")
    raw = http_get(url, cfg.get("timeout", 15))
    if not raw or len(raw) < 50 or '=""' in raw:
        logger.info("新浪RS接口无数据（可能需用网页版）")
        return None

    match = re.search(r'"([^"]*)"', raw)
    if not match:
        return None
    fields = match.group(1).split(",")
    if len(fields) < 4:
        return None

    if "hf_RS" in url:
        # 新浪 RS.shtml 对应的实时接口为 hf_RS。
        # 格式: latest, ..., time, prev_close, ..., date, name, ...
        close = _to_float(fields[0])
        prev = _to_float(fields[7])
        trade_date = fields[12].strip() if len(fields) > 12 else ""
    else:
        # 旧 RS 接口格式: name, open, prev_close, close, high, low, ...
        close = _to_float(fields[3])
        prev = _to_float(fields[2])
        trade_date = ""
    chg_pct = ((close - prev) / prev * 100) if prev != 0 else 0.0

    display_cfg = config["display_contract"]["ice"]
    src_name = display_cfg["source_name"]
    result = {
        "contract_code": "RS",
        "display_name": display_cfg["display_name"],
        "close": round(close, 2),
        "prev_close": round(prev, 2),
        "change_pct": round(chg_pct, 2),
        "trade_date": trade_date,
        "source_name": src_name,
        "source_url": display_cfg.get("source_url", cfg.get("rs_url", "")),
        "raw_url": cfg.get("rs_url", ""),
        "_is_display_only": True,
        "_note": "RS为ICE原糖主力连续行情，具体交割月份未确认",
    }
    logger.info("RS展示行情: close=%.2f chg=%.2f%%", close, chg_pct)
    return result


def collect_market_data(target_date: str | None = None) -> dict:
    """
    获取市场数据。
    1. 新浪 SR0 → 郑糖主力展示行情
    2. 新浪 RS → ICE主力展示行情
    3. 泛糖科技 → 进口利润
    4. CSV → 南宁现货等补充字段
    """
    all_errors = []
    csv_extra = {}
    csv_zz_data = None
    csv_ice_data = None
    source_label = config["display_contract"]["zhengzhou"].get("source_name", "新浪财经")
    source_url = config["display_contract"]["zhengzhou"].get("source_url", "")

    # 1. CSV 先读作补充字段，不再校验为市场表现主合约
    csv_result = fetch_from_csv(target_date)
    if csv_result["ok"] and csv_result["data"]:
        csv_zz_data = csv_result["data"]
        csv_ice_data = csv_result.get("ice_data")
        csv_extra = csv_result.get("_csv_extra", {})
    else:
        all_errors.extend(csv_result.get("errors", []))

    # 2. 新浪 SR0/RS 为展示行情主来源
    sr0 = fetch_sr0_display()
    rs = fetch_rs_display()
    if sr0 and not is_recent_enough(sr0.get("trade_date", ""), target_date):
        all_errors.append(f"新浪SR0行情日期 {sr0.get('trade_date')} 超过时效阈值")
        logger.warning("新浪SR0行情日期过旧，拒绝使用: %s", sr0.get("trade_date"))
        sr0 = None
    if rs and rs.get("trade_date") and not is_recent_enough(rs.get("trade_date", ""), target_date):
        all_errors.append(f"新浪RS行情日期 {rs.get('trade_date')} 超过时效阈值")
        logger.warning("新浪RS行情日期过旧，拒绝使用: %s", rs.get("trade_date"))
        rs = None

    if sr0 is None and csv_zz_data is None:
        return {
            "ok": False,
            "errors": all_errors + ["新浪SR0不可用，CSV也无可用郑糖备用行情"],
            "trade_date": "",
            "_source_label": "",
            "_source_url": "",
        }

    trade_date = (sr0 or csv_zz_data or {}).get("trade_date", "")
    # 再次确认 trade_date 不是周末
    if trade_date and not is_valid_trading_date(trade_date):
        return {
            "ok": False,
            "errors": all_errors + [f"最终交易日期 {trade_date} 为周末，拒绝使用"],
            "trade_date": trade_date,
            "_source_label": source_label,
            "_source_url": source_url,
        }

    result = {
        "ok": True,
        "errors": all_errors,
        "trade_date": trade_date,
        "_source_label": source_label,
        "_source_url": source_url,
        "_from_sina": sr0 is not None or rs is not None,
    }

    # 郑糖 SR0 展示行情
    if sr0 and sr0.get("close"):
        result["zz_display_name"] = sr0["display_name"]
        result["zz_contract_code"] = make_field("SR0", sr0.get("trade_date", trade_date),
                                                sr0["source_name"], sr0["source_url"])
        result["zz_close"] = make_field(sr0["close"], sr0.get("trade_date", trade_date),
                                        sr0["source_name"], sr0["source_url"])
        result["zz_prev_close"] = make_field(sr0["prev_close"], sr0.get("trade_date", trade_date),
                                             sr0["source_name"], sr0["source_url"])
        result["zz_change_pct"] = make_field(sr0["change_pct"], sr0.get("trade_date", trade_date),
                                             sr0["source_name"], sr0["source_url"])
        result["_sr0_note"] = sr0.get("_note", "")
        logger.info("郑糖展示行情使用SR0: %.2f", sr0["close"])
    else:
        # SR0不可用时使用CSV值
        zz_data = csv_zz_data
        close = zz_data.get("close", 0)
        prev = zz_data.get("prev_close", 0)
        chg_pct = ((close - prev) / prev * 100) if prev != 0 else 0.0
        csv_source_label = csv_result.get("_source_label", "本地备用CSV")
        csv_source_url = csv_result.get("_source_url", "")
        result["zz_display_name"] = config["display_contract"]["zhengzhou"]["display_name"]
        result["zz_contract_code"] = make_field(zz_data.get("contract_code", "SR"), trade_date, csv_source_label, csv_source_url)
        result["zz_contract_month"] = make_field(zz_data.get("contract_month", str(TARGET_MONTH)), trade_date, csv_source_label, csv_source_url)
        result["zz_close"] = make_field(round(close, 2), trade_date, csv_source_label, csv_source_url)
        result["zz_prev_close"] = make_field(round(prev, 2), trade_date, csv_source_label, csv_source_url)
        result["zz_change_pct"] = make_field(round(chg_pct, 2), trade_date, csv_source_label, csv_source_url)
        logger.info("SR0不可用，郑糖展示使用CSV: %.2f", close)

    # ICE RS 展示行情
    if rs and rs.get("close"):
        result["ice_display_name"] = rs["display_name"]
        rs_date = rs.get("trade_date") or trade_date
        result["ice_contract_code"] = make_field("RS", rs_date, rs["source_name"], rs["source_url"])
        result["ice_close"] = make_field(rs["close"], rs_date, rs["source_name"], rs["source_url"])
        result["ice_prev_close"] = make_field(rs["prev_close"], rs_date, rs["source_name"], rs["source_url"])
        result["ice_change_pct"] = make_field(rs["change_pct"], rs_date, rs["source_name"], rs["source_url"])
        result["_ice_note"] = rs.get("_note", "")
        logger.info("ICE展示行情使用RS: %.2f", rs["close"])
    else:
        result["ice_display_name"] = config["display_contract"]["ice"]["display_name"]
        if csv_ice_data:
            ice_close = csv_ice_data.get("close", 0)
            ice_prev = csv_ice_data.get("prev_close", 0)
            ice_chg = ((ice_close - ice_prev) / ice_prev * 100) if ice_prev != 0 else 0.0
            csv_source_label = csv_result.get("_source_label", "本地备用CSV")
            csv_source_url = csv_result.get("_source_url", "")
            result["ice_contract_code"] = make_field(csv_ice_data.get("contract_code", ""), trade_date, csv_source_label, csv_source_url)
            result["ice_close"] = make_field(round(ice_close, 2), trade_date, csv_source_label, csv_source_url)
            result["ice_prev_close"] = make_field(round(ice_prev, 2), trade_date, csv_source_label, csv_source_url)
            result["ice_change_pct"] = make_field(round(ice_chg, 2), trade_date, csv_source_label, csv_source_url)
        else:
            for k in ["ice_contract_code", "ice_close", "ice_prev_close", "ice_change_pct"]:
                result[k] = make_field(None, "", "", "")

    # 现货
    if csv_extra.get("nanning_spot"):
        result["nanning_spot"] = make_field(csv_extra["nanning_spot"], trade_date, "本地备用CSV", source_url)
    else:
        result["nanning_spot"] = make_field(None, trade_date, "手动填入", "请在 inputs/market_fallback.csv 填入 nanning_spot")

    # 配额外巴西糖加工完税估算利润 — 优先使用泛糖科技
    hisugar_data = fetch_hisugar_import_profit(target_date)
    if hisugar_data and hisugar_data.get("quota_outside_profit"):
        profit_val = hisugar_data["quota_outside_profit"]
        profit_date = hisugar_data["data_date"]
        profit_src = hisugar_data["source_name"]
        profit_url = hisugar_data["source_url"]
        result["brazil_profit"] = make_field(profit_val, profit_date, profit_src, profit_url)
        result["_import_profit_meta"] = {
            "source": profit_src,
            "data_date": profit_date,
            "reference_spot": hisugar_data.get("profit_reference_spot", "日照白糖现货价"),
            "source_url": profit_url,
            "ice_close": hisugar_data.get("ice_close"),
            "usd_cny": hisugar_data.get("usd_cny"),
            "quota_outside_cost": hisugar_data.get("quota_outside_cost"),
            "quota_outside_profit": profit_val,
            "status": hisugar_data.get("status", ""),
        }
        logger.info("巴西进口利润来自泛糖科技: %.0f 元/吨 (数据日期 %s)", profit_val, profit_date)
    elif csv_extra.get("brazil_profit") and csv_extra.get("quota_outside_profit_data_date"):
        # CSV 备用（需同时有日期和来源）
        profit_val = csv_extra["brazil_profit"]
        csv_profit_date = csv_extra.get("quota_outside_profit_data_date", trade_date)
        csv_profit_src = csv_extra.get("quota_outside_profit_source", "本地备用CSV")
        csv_profit_spot = csv_extra.get("quota_outside_profit_reference_spot", "")
        if not csv_profit_spot:
            logger.warning("CSV利润数据缺少参考现货市场信息，跳过")
            result["brazil_profit"] = make_field(None, trade_date, "手动填入", "请在 inputs/market_fallback.csv 填入利润日期、来源和参考现货")
        else:
            result["brazil_profit"] = make_field(profit_val, csv_profit_date, csv_profit_src, source_url)
            result["_import_profit_meta"] = {
                "source": csv_profit_src,
                "data_date": csv_profit_date,
                "reference_spot": csv_profit_spot,
                "source_url": source_url,
                "quota_outside_profit": profit_val,
                "status": "from_csv",
            }
    else:
        result["brazil_profit"] = make_field(None, trade_date, "手动填入", "请在 inputs/market_fallback.csv 填入利润日期、来源和参考现货")

    # 基差
    zz_c = result["zz_close"]["value"]
    ns = result["nanning_spot"]["value"]
    if zz_c is not None and ns is not None:
        result["basis"] = make_field(round(ns - zz_c, 2), trade_date, "Python计算", "")
    else:
        result["basis"] = make_field(None, "", "", "")

    return result


# ============================================================
# CLI
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="获取白糖市场数据")
    parser.add_argument("--date", type=str, default=None, help="目标日期 YYYY-MM-DD")
    args = parser.parse_args()

    result = collect_market_data(args.date)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    sys.exit(0 if result["ok"] else 1)


if __name__ == "__main__":
    main()
