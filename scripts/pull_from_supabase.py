#!/usr/bin/env python3
"""
Pull data from Supabase to local YAML files.

Useful for syncing Supabase changes back to local files
so Claude Code can work with fresh data.

Usage:
  python3 scripts/pull_from_supabase.py              # all
  python3 scripts/pull_from_supabase.py --only recipes
  python3 scripts/pull_from_supabase.py --only logs
  python3 scripts/pull_from_supabase.py --only logs --days 30
"""

import sys
import os
import yaml
from pathlib import Path
from datetime import date, timedelta
from dotenv import load_dotenv

BASE = Path(__file__).parent.parent
load_dotenv(BASE / ".env")

sys.path.insert(0, str(BASE / "bot"))
import db


def pull_recipes():
    recipes = db.get_all_recipes()
    out_path = BASE / "data/tactical/nutrition/recipes.yaml"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # Strip Supabase-internal fields before saving
    clean = []
    for r in recipes:
        entry = {k: v for k, v in r.items() if k != "id" and v is not None}
        clean.append(entry)

    with open(out_path, "w", encoding="utf-8") as f:
        yaml.dump({"recipes": clean}, f, allow_unicode=True,
                  default_flow_style=False, sort_keys=False)
    print(f"  ✓ {len(clean)} recipes → {out_path.relative_to(BASE)}")


def pull_logs(days: int = 90):
    start = (date.today() - timedelta(days=days)).isoformat()
    r = db.get_client().table("daily_logs").select("*").gte("date", start).order("date").execute()
    logs = r.data or []

    logs_dir = BASE / "data/tactical/logs"
    logs_dir.mkdir(parents=True, exist_ok=True)

    for log in logs:
        d = log.get("date")
        if not d:
            continue
        entry = {k: v for k, v in log.items() if k not in ("id", "updated_at") and v is not None}
        path = logs_dir / f"{d}.yaml"
        with open(path, "w", encoding="utf-8") as f:
            yaml.dump(entry, f, allow_unicode=True,
                      default_flow_style=False, sort_keys=False)

    print(f"  ✓ {len(logs)} logs → data/tactical/logs/")


def main():
    only = None
    days = 90

    if "--only" in sys.argv:
        idx = sys.argv.index("--only")
        if idx + 1 < len(sys.argv):
            only = sys.argv[idx + 1]

    if "--days" in sys.argv:
        idx = sys.argv.index("--days")
        if idx + 1 < len(sys.argv):
            days = int(sys.argv[idx + 1])

    print("Supabase → локальные файлы")
    print()

    if only is None or only == "recipes":
        print("Рецепты:")
        pull_recipes()
        print()

    if only is None or only == "logs":
        print(f"Логи (последние {days} дней):")
        pull_logs(days)
        print()

    print("Готово.")


if __name__ == "__main__":
    main()
