"""
Supabase client and database operations for Health OS.
Single source of truth for recipes, daily logs, and Oura data.
"""
import os
from datetime import date, timedelta

from supabase import create_client, Client

_client: Client | None = None


def get_client() -> Client:
    global _client
    if _client is None:
        url = os.environ.get("SUPABASE_URL", "")
        key = os.environ.get("SUPABASE_KEY", "")
        if not url or not key:
            raise RuntimeError("SUPABASE_URL and SUPABASE_KEY must be set in environment")
        _client = create_client(url, key)
    return _client


# ─────────────────────────── Recipes ──────────────────────────


def get_all_recipes() -> list[dict]:
    r = get_client().table("recipes").select("*").order("name").execute()
    return r.data or []


def lookup_recipe(query: str) -> dict | None:
    """Find recipe by name or alias (substring match)."""
    query_lower = query.lower().strip()
    for r in get_all_recipes():
        candidates = [r.get("name", "").lower()] + [
            a.lower() for a in (r.get("aliases") or [])
        ]
        if any(query_lower in c or c in query_lower for c in candidates):
            return r
    return None


def insert_recipe(data: dict) -> dict:
    r = get_client().table("recipes").insert(data).execute()
    return r.data[0] if r.data else {}


def update_recipe_by_id(recipe_id: int, changes: dict) -> dict:
    r = get_client().table("recipes").update(changes).eq("id", recipe_id).execute()
    return r.data[0] if r.data else {}


# ─────────────────────────── Daily Logs ───────────────────────


def get_log(d: str = None) -> dict:
    """Return log for date d, or an empty template if none exists."""
    d = d or date.today().isoformat()
    r = get_client().table("daily_logs").select("*").eq("date", d).execute()
    if r.data:
        row = r.data[0]
        row.setdefault("meals", [])
        row.setdefault("training", [])
        return row
    return {
        "date": d,
        "weight_morning": None,
        "meals": [],
        "training": [],
        "sleep": None,
        "notes": "",
    }


def upsert_log(d: str, data: dict) -> dict:
    """Insert or update log for date d."""
    payload = {k: v for k, v in data.items() if k != "id"}
    payload["date"] = d
    r = (
        get_client()
        .table("daily_logs")
        .upsert(payload, on_conflict="date")
        .execute()
    )
    return r.data[0] if r.data else payload


def get_week_logs() -> list[dict]:
    """Return logs for the last 7 days (oldest first)."""
    start = (date.today() - timedelta(days=7)).isoformat()
    r = (
        get_client()
        .table("daily_logs")
        .select("*")
        .gte("date", start)
        .order("date")
        .execute()
    )
    return r.data or []


# ─────────────────────────── Oura Data ────────────────────────


def upsert_oura(d: str, data: dict) -> None:
    """Insert or update raw Oura data for date d."""
    get_client().table("oura_data").upsert(
        {"date": d, "data": data}, on_conflict="date"
    ).execute()


def get_oura(d: str) -> dict:
    r = get_client().table("oura_data").select("data").eq("date", d).execute()
    return r.data[0]["data"] if r.data else {}
