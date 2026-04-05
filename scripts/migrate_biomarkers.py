"""
Migrate biomarkers.yaml → Supabase biomarkers table.
Run from project root: python3 scripts/migrate_biomarkers.py
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "bot"))

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env")
load_dotenv(Path(__file__).parent.parent / "bot" / ".env")

import yaml
from supabase import create_client

BASE = Path(__file__).parent.parent

def get_client():
    url = os.environ["SUPABASE_URL"]
    key = os.environ["SUPABASE_KEY"]
    return create_client(url, key)


def parse_date(key: str) -> str | None:
    """Convert shorthand date keys to ISO dates."""
    mapping = {
        "dec2024": "2024-12-28",
        "jul2025": "2025-07-01",
        "jan2025": "2025-01-01",
        "mar2020": "2020-03-21",
    }
    if key in mapping:
        return mapping[key]
    # Handle bare year like "2020"
    if key.isdigit() and len(key) == 4:
        return f"{key}-01-01"
    return None


def extract_rows(biomarkers: dict) -> list[dict]:
    rows = []

    for category, markers in biomarkers.items():
        if not isinstance(markers, dict):
            continue
        # Skip top-level metadata keys
        if category in ("last_updated", "needs_update", "serology"):
            continue

        for marker_name, marker_data in markers.items():
            if not isinstance(marker_data, dict):
                continue

            unit = marker_data.get("unit")
            status = marker_data.get("status")
            reference_lab = str(marker_data.get("reference_lab", "") or "")
            attia_optimal = str(marker_data.get("attia_optimal", "") or "")
            notes = marker_data.get("notes", "")
            if isinstance(notes, str):
                notes = notes.strip()

            # Marker has multiple date-keyed values (e.g. dec2024, jul2025)
            date_values = {k: v for k, v in marker_data.items() if parse_date(k)}
            if date_values:
                for date_key, value in date_values.items():
                    iso_date = parse_date(date_key)
                    if isinstance(value, (int, float)):
                        rows.append({
                            "marker": marker_name,
                            "category": category,
                            "value": float(value),
                            "date": iso_date,
                            "unit": unit,
                            "status": status,
                            "reference_lab": reference_lab or None,
                            "attia_optimal": attia_optimal or None,
                            "notes": notes or None,
                        })

            # Marker has a single value + explicit date field
            elif "value" in marker_data and "date" in marker_data:
                raw_date = str(marker_data["date"])
                # Normalize bare year to full date
                if raw_date.isdigit() and len(raw_date) == 4:
                    raw_date = f"{raw_date}-01-01"
                value = marker_data["value"]
                if isinstance(value, (int, float)):
                    rows.append({
                        "marker": marker_name,
                        "category": category,
                        "value": float(value),
                        "date": raw_date,
                        "unit": unit,
                        "status": status,
                        "reference_lab": reference_lab or None,
                        "attia_optimal": attia_optimal or None,
                        "notes": notes or None,
                    })

            # Marker has a single value without explicit date — use last_updated
            elif "value" in marker_data:
                value = marker_data["value"]
                fallback_date = str(biomarkers.get("last_updated", "2025-07-01"))
                if isinstance(value, (int, float)):
                    rows.append({
                        "marker": marker_name,
                        "category": category,
                        "value": float(value),
                        "date": fallback_date,
                        "unit": unit,
                        "status": status,
                        "reference_lab": reference_lab or None,
                        "attia_optimal": attia_optimal or None,
                        "notes": notes or None,
                    })

    return rows


def main():
    path = BASE / "data/strategic/biomarkers.yaml"
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)

    rows = extract_rows(data)
    print(f"Extracted {len(rows)} rows from biomarkers.yaml")

    client = get_client()

    inserted = 0
    skipped = 0
    for row in rows:
        try:
            client.table("biomarkers").upsert(row, on_conflict="marker,date").execute()
            print(f"  ✓ {row['marker']} ({row['date']}) = {row['value']} {row['unit'] or ''}")
            inserted += 1
        except Exception as e:
            print(f"  ✗ {row['marker']} ({row['date']}): {e}")
            skipped += 1

    print(f"\nДone: {inserted} upserted, {skipped} failed.")


if __name__ == "__main__":
    main()
