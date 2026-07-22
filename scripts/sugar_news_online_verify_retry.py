from __future__ import annotations

import argparse
import subprocess
import sys
import time


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", required=True)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--attempts", type=int, default=18)
    parser.add_argument("--sleep-seconds", type=int, default=20)
    args = parser.parse_args()

    for attempt in range(1, args.attempts + 1):
        result = subprocess.run(
            [
                sys.executable,
                "scripts/verify_sugar_news_dashboard.py",
                "--date",
                args.date,
                "--base-url",
                args.base_url,
            ],
            text=True,
            capture_output=True,
        )
        if result.returncode == 0:
            print(result.stdout)
            return 0
        print(f"Vercel not ready attempt {attempt}/{args.attempts}: {result.stderr or result.stdout}")
        if attempt < args.attempts:
            time.sleep(args.sleep_seconds)
    print("Vercel Sugar News verification failed after retry window", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
