from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from urllib.request import Request, urlopen


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Verify Sugar News dashboard data.")
    parser.add_argument("--date", required=True, help="Expected news date YYYY-MM-DD")
    parser.add_argument("--base-url", help="Remote base URL, e.g. https://sugar-daily-cc.vercel.app")
    return parser.parse_args()


def load_url(url: str) -> str:
    req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urlopen(req, timeout=20) as resp:
        if resp.status != 200:
            raise RuntimeError(f"{url} returned HTTP {resp.status}")
        return resp.read().decode("utf-8")


def load_json(path_or_url: str):
    if path_or_url.startswith("http"):
        return json.loads(load_url(path_or_url))
    with Path(path_or_url).open("r", encoding="utf-8") as f:
        return json.load(f)


def verify_payload(payload: dict, expected_date: str) -> dict:
    if payload.get("newsDate") != expected_date:
        raise AssertionError(f"newsDate mismatch: {payload.get('newsDate')} != {expected_date}")
    countries = payload.get("countries") or []
    if any(not c.get("items") for c in countries):
        raise AssertionError("Dashboard payload contains empty country section")
    count = 0
    positions = []
    for country in countries:
        name = country["country"]
        if name == "巴西":
            positions.append(0)
        elif name == "印度":
            positions.append(1)
        elif name == "泰国":
            positions.append(2)
        elif name == "中国":
            positions.append(3)
        else:
            positions.append(4)
        for item in country.get("items", []):
            count += 1
            for field in ("news", "impactType", "impact", "sourceName", "sourceUrl"):
                if not item.get(field):
                    raise AssertionError(f"Item missing {field}")
            if item["impactType"] not in {"偏多糖价", "偏空糖价", "中性", "影响有限"}:
                raise AssertionError(f"Invalid impactType: {item['impactType']}")
            if any(text in item["news"] for text in ("暂无新闻", "暂无最新数据", "暂无最新对比数据")):
                raise AssertionError("Placeholder wording found")
    if positions != sorted(positions):
        raise AssertionError("Country order mismatch")
    india_metrics = payload.get("indiaMetrics")
    if not isinstance(india_metrics, dict):
        raise AssertionError("Dashboard payload missing indiaMetrics")
    for field in ("domesticSugarPrice", "upExMillPrice", "carryoverStock"):
        metric = india_metrics.get(field)
        if not isinstance(metric, dict):
            raise AssertionError(f"indiaMetrics missing {field}")
        if metric.get("status") not in {"ok", "pending", "stale"}:
            raise AssertionError(f"Invalid {field} status: {metric.get('status')}")
        if metric.get("status") == "ok":
            if field == "carryoverStock" and metric.get("stockWanTonnes") is None:
                raise AssertionError("carryoverStock requires stockWanTonnes")
            if field != "carryoverStock" and metric.get("priceInrPerQuintal") is None and not metric.get("rangeInrPerQuintal"):
                raise AssertionError(f"{field} requires price or range")
    return {"newsDate": expected_date, "countryCount": len(countries), "itemCount": count}


def main() -> int:
    args = parse_args()
    if args.base_url:
        base = args.base_url.rstrip("/")
        html = load_url(f"{base}/sugar-news")
        if "Sugar News" not in html:
            raise AssertionError("Remote /sugar-news page missing title")
        index = load_json(f"{base}/public/sugar-news/data/index.json")
        if index.get("latestNewsDate") != args.date:
            raise AssertionError(f"Remote index latest date mismatch: {index.get('latestNewsDate')} != {args.date}")
        report = next((r for r in index.get("reports", []) if r.get("newsDate") == args.date), None)
        if not report:
            raise AssertionError("Expected date missing from remote index")
        payload = load_json(base + report["path"])
    else:
        index_path = PROJECT_ROOT / "public" / "sugar-news" / "data" / "index.json"
        index = load_json(str(index_path))
        if index.get("latestNewsDate") != args.date:
            raise AssertionError(f"Local index latest date mismatch: {index.get('latestNewsDate')} != {args.date}")
        report = next((r for r in index.get("reports", []) if r.get("newsDate") == args.date), None)
        if not report:
            raise AssertionError("Expected date missing from local index")
        payload_path = PROJECT_ROOT / report["path"].lstrip("/")
        payload = load_json(str(payload_path))
    result = verify_payload(payload, args.date)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
