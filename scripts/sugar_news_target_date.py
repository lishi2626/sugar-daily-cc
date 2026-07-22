from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo


def main() -> None:
    event_path = os.environ.get("GITHUB_EVENT_PATH")
    requested = ""
    if event_path and os.path.exists(event_path):
        with open(event_path, "r", encoding="utf-8") as handle:
            requested = ((json.load(handle).get("inputs") or {}).get("date") or "").strip()

    if requested:
        print(requested)
        return

    try:
        shanghai_tz = ZoneInfo("Asia/Shanghai")
    except Exception:
        shanghai_tz = timezone(timedelta(hours=8), name="Asia/Shanghai")
    target_date = datetime.now(shanghai_tz).date() - timedelta(days=1)
    print(target_date.isoformat())


if __name__ == "__main__":
    main()
