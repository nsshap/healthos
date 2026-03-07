#!/usr/bin/env python3
"""
Push Health OS data from local YAML files to Supabase.

Safe to re-run: uses upsert (no duplicates).

Usage:
  python3 scripts/migrate_to_supabase.py                   # всё
  python3 scripts/migrate_to_supabase.py --only recipes    # рецепты
  python3 scripts/migrate_to_supabase.py --only logs       # ежедневные логи
  python3 scripts/migrate_to_supabase.py --only oura       # данные Oura
  python3 scripts/migrate_to_supabase.py --only biomarkers # биомаркеры/анализы
"""

import sys
import os
import yaml
from pathlib import Path
from dotenv import load_dotenv

# Load .env from project root
BASE = Path(__file__).parent.parent
load_dotenv(BASE / ".env")

# Add bot/ to path so we can import db
sys.path.insert(0, str(BASE / "bot"))
import db


def migrate_recipes():
    path = BASE / "data/tactical/nutrition/recipes.yaml"
    if not path.exists():
        print("  recipes.yaml not found, skipping")
        return

    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    recipes = data.get("recipes", [])

    print(f"  Migrating {len(recipes)} recipes...")
    ok = 0
    for r in recipes:
        try:
            # Remove internal list index (YAML list position)
            entry = {k: v for k, v in r.items() if v is not None}
            db.get_client().table("recipes").upsert(entry, on_conflict="name").execute()
            ok += 1
        except Exception as e:
            print(f"    WARN: {r.get('name')} — {e}")
    print(f"  ✓ {ok}/{len(recipes)} recipes migrated")


def migrate_logs():
    logs_dir = BASE / "data/tactical/logs"
    if not logs_dir.exists():
        print("  logs/ not found, skipping")
        return

    files = sorted(logs_dir.glob("*.yaml"))
    print(f"  Migrating {len(files)} daily logs...")
    ok = 0
    for f in files:
        try:
            with open(f, encoding="utf-8") as fh:
                log = yaml.safe_load(fh) or {}
            d = log.get("date") or f.stem
            payload = {
                "date": str(d),
                "weight_morning": log.get("weight_morning"),
                "meals": log.get("meals") or [],
                "training": log.get("training") or [],
                "sleep": log.get("sleep"),
                "notes": log.get("notes") or "",
            }
            db.get_client().table("daily_logs").upsert(payload, on_conflict="date").execute()
            ok += 1
        except Exception as e:
            print(f"    WARN: {f.name} — {e}")
    print(f"  ✓ {ok}/{len(files)} logs migrated")


def migrate_oura():
    oura_dir = BASE / "data/oura"
    if not oura_dir.exists():
        print("  data/oura/ not found, skipping")
        return

    files = sorted(f for f in oura_dir.glob("*.yaml"))
    print(f"  Migrating {len(files)} Oura records...")
    ok = 0
    for f in files:
        try:
            with open(f, encoding="utf-8") as fh:
                data = yaml.safe_load(fh) or {}
            d = data.get("date") or f.stem
            db.get_client().table("oura_data").upsert(
                {"date": str(d), "data": data}, on_conflict="date"
            ).execute()
            ok += 1
        except Exception as e:
            print(f"    WARN: {f.name} — {e}")
    print(f"  ✓ {ok}/{len(files)} Oura records migrated")


def migrate_biomarkers():
    path = BASE / "data/strategic/biomarkers.yaml"
    if not path.exists():
        print("  biomarkers.yaml not found, skipping")
        return

    import json
    from datetime import date as _date, datetime as _datetime

    def _jsonify(obj):
        """Recursively convert date/datetime objects to ISO strings."""
        if isinstance(obj, (_date, _datetime)):
            return obj.isoformat()
        if isinstance(obj, dict):
            return {k: _jsonify(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [_jsonify(i) for i in obj]
        return obj

    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    db.get_client().table("settings").upsert(
        {"key": "biomarkers", "value": _jsonify(data)}, on_conflict="key"
    ).execute()
    print("  ✓ biomarkers.yaml → settings[biomarkers]")


def main():
    only = None
    if "--only" in sys.argv:
        idx = sys.argv.index("--only")
        if idx + 1 < len(sys.argv):
            only = sys.argv[idx + 1]

    print("Health OS → Supabase")
    print(f"  SUPABASE_URL: {os.environ.get('SUPABASE_URL', 'NOT SET')[:40]}...")
    print()

    if only is None or only == "recipes":
        print("Recipes:")
        migrate_recipes()
        print()

    if only is None or only == "logs":
        print("Daily logs:")
        migrate_logs()
        print()

    if only is None or only == "oura":
        print("Oura data:")
        migrate_oura()
        print()

    if only == "biomarkers":
        print("Biomarkers:")
        migrate_biomarkers()
        print()

    print("Done.")


if __name__ == "__main__":
    main()
