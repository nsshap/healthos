"""
Supabase client and database operations for Health OS.
Single source of truth for recipes, daily logs, and Oura data.
"""
import os
import time
from datetime import date, timedelta

from supabase import create_client, Client

_client: Client | None = None

# ─────────────────────── Recipe cache ─────────────────────────
_recipe_cache: list[dict] = []
_recipe_cache_ts: float = 0.0
_RECIPE_CACHE_TTL = 300  # 5 minutes


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
    global _recipe_cache, _recipe_cache_ts
    if time.time() - _recipe_cache_ts < _RECIPE_CACHE_TTL:
        return _recipe_cache
    r = get_client().table("recipes").select("*").order("name").execute()
    _recipe_cache = r.data or []
    _recipe_cache_ts = time.time()
    return _recipe_cache


def invalidate_recipe_cache() -> None:
    global _recipe_cache_ts
    _recipe_cache_ts = 0.0


def lookup_recipe(query: str) -> dict | None:
    """
    Find recipe by name or alias.
    Scores each candidate and returns the best match above threshold.
    - Exact match → score 1.0
    - query is substring of candidate → score = len(query)/len(candidate)
    - candidate is substring of query AND len(candidate) >= 4
      AND ratio >= 0.4 → score = len(candidate)/len(query)
    Minimum score to accept: 0.5
    """
    query_lower = query.lower().strip()
    if not query_lower:
        return None

    best_recipe = None
    best_score = 0.0

    for r in get_all_recipes():
        candidates = [r.get("name", "").lower()] + [
            a.lower() for a in (r.get("aliases") or [])
        ]
        for c in candidates:
            if not c:
                continue
            if query_lower == c:
                score = 1.0
            elif query_lower in c:
                score = len(query_lower) / len(c)
            elif c in query_lower and len(c) >= 4 and len(c) / len(query_lower) >= 0.4:
                score = len(c) / len(query_lower)
            else:
                continue

            if score > best_score:
                best_score = score
                best_recipe = r

    return best_recipe if best_score >= 0.5 else None


def insert_recipe(data: dict) -> dict:
    r = get_client().table("recipes").insert(data).execute()
    invalidate_recipe_cache()
    return r.data[0] if r.data else {}


def update_recipe_by_id(recipe_id: int, changes: dict) -> dict:
    r = get_client().table("recipes").update(changes).eq("id", recipe_id).execute()
    invalidate_recipe_cache()
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


# ─────────────────────── Research Items ───────────────────────


def get_research_ids() -> set[str]:
    """Вернуть все существующие id статей (для дедупликации)."""
    r = get_client().table("research_items").select("id").execute()
    return {row["id"] for row in (r.data or [])}


def insert_research_items(items: list[dict]) -> None:
    """Записать новые статьи пачкой."""
    if not items:
        return
    get_client().table("research_items").insert(items).execute()


def get_research_items_since(since_iso: str, min_score: int = 0) -> list[dict]:
    """Вернуть статьи начиная с даты since_iso с score >= min_score."""
    r = (
        get_client()
        .table("research_items")
        .select("*")
        .gte("fetched_at", since_iso)
        .gte("score", min_score)
        .order("score", desc=True)
        .execute()
    )
    return r.data or []


def get_research_stats() -> dict:
    """Статистика: всего статей, за неделю, по источникам."""
    from datetime import datetime, timedelta
    week_ago = (datetime.utcnow() - timedelta(days=7)).isoformat()
    all_r = get_client().table("research_items").select("id", count="exact").execute()
    week_r = (
        get_client()
        .table("research_items")
        .select("source, score")
        .gte("fetched_at", week_ago)
        .execute()
    )
    week_items = week_r.data or []
    by_source: dict[str, int] = {}
    high_score = 0
    for item in week_items:
        src = item["source"]
        by_source[src] = by_source.get(src, 0) + 1
        if (item.get("score") or 0) >= 6:
            high_score += 1
    return {
        "total": all_r.count or 0,
        "week_count": len(week_items),
        "week_high_score": high_score,
        "by_source": by_source,
    }
