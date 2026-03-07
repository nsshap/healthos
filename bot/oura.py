"""
Oura Ring API integration.
Fetches sleep, readiness, activity, workouts, stress, SpO2 for Health OS.
Writes results to Supabase (oura_data + daily_logs tables).
"""
import asyncio
import os
from datetime import date, datetime
from pathlib import Path

import httpx

import db

BASE = Path(__file__).parent.parent
TOKEN_FILE = BASE / "config/oura_token.txt"
API_BASE = "https://api.ouraring.com/v2/usercollection"


def _token() -> str:
    if t := os.environ.get("OURA_TOKEN", ""):
        return t.strip()
    return TOKEN_FILE.read_text().strip()


async def _get(endpoint: str, params: dict) -> dict:
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(
            f"{API_BASE}/{endpoint}",
            headers={"Authorization": f"Bearer {_token()}"},
            params=params,
        )
        r.raise_for_status()
        return r.json()


async def _one(endpoint: str, d: str) -> dict:
    """Fetch single-day data from a daily endpoint."""
    data = await _get(endpoint, {"start_date": d, "end_date": d})
    items = data.get("data", [])
    return items[0] if items else {}


def _duration_min(start: str, end: str) -> int:
    if not start or not end:
        return 0
    s = datetime.fromisoformat(start)
    e = datetime.fromisoformat(end)
    return round((e - s).total_seconds() / 60)


def _hhmm(iso_str: str) -> str | None:
    """Extract local HH:MM from ISO datetime."""
    if not iso_str:
        return None
    try:
        return datetime.fromisoformat(iso_str).strftime("%H:%M")
    except Exception:
        return None


def _score_to_quality(score) -> str:
    if score is None:
        return "ok"
    if score >= 80:
        return "good"
    if score >= 60:
        return "ok"
    return "poor"


# ─────────────────────── Fetch functions ───────────────────────

async def fetch_sleep(d: str) -> dict:
    """Detailed sleep session + daily sleep score."""
    session_resp, score = await asyncio.gather(
        _get("sleep", {"start_date": d, "end_date": d}),
        _one("daily_sleep", d),
        return_exceptions=True,
    )

    result: dict = {}

    if isinstance(score, dict) and score:
        result["score"] = score.get("score")
        result["contributors"] = score.get("contributors", {})

    if isinstance(session_resp, dict):
        sessions = session_resp.get("data", [])
        if sessions:
            # Main sleep = longest session
            s = max(sessions, key=lambda x: x.get("total_sleep_duration") or 0)
            total = s.get("total_sleep_duration") or 0
            result.update({
                "hours": round(total / 3600, 1),
                "efficiency": s.get("efficiency"),
                "avg_hrv": s.get("average_hrv"),
                "avg_hr": round(s.get("average_heart_rate") or 0),
                "resting_hr": s.get("lowest_heart_rate"),
                "deep_min": round((s.get("deep_sleep_duration") or 0) / 60),
                "rem_min": round((s.get("rem_sleep_duration") or 0) / 60),
                "light_min": round((s.get("light_sleep_duration") or 0) / 60),
                "awake_min": round((s.get("awake_time") or 0) / 60),
                "bedtime_start": _hhmm(s.get("bedtime_start")),
                "bedtime_end": _hhmm(s.get("bedtime_end")),
            })

    return result


async def fetch_readiness(d: str) -> dict:
    data = await _one("daily_readiness", d)
    if not data:
        return {}
    c = data.get("contributors", {})
    return {
        "score": data.get("score"),
        "hrv_balance": c.get("hrv_balance"),
        "resting_hr_score": c.get("resting_heart_rate"),
        "sleep_balance": c.get("sleep_balance"),
        "activity_balance": c.get("activity_balance"),
        "recovery_index": c.get("recovery_index"),
        "body_temp_deviation": data.get("temperature_deviation"),
    }


async def fetch_activity(d: str) -> dict:
    data = await _one("daily_activity", d)
    if not data:
        return {}
    return {
        "score": data.get("score"),
        "steps": data.get("steps"),
        "active_calories": data.get("active_calories"),
        "total_calories": data.get("total_calories"),
        "zone2_min": round((data.get("medium_activity_time") or 0) / 60),
        "high_activity_min": round((data.get("high_activity_time") or 0) / 60),
        "walking_km": round((data.get("equivalent_walking_distance") or 0) / 1000, 1),
        "sedentary_hours": round((data.get("sedentary_time") or 0) / 3600, 1),
    }


async def fetch_workouts(d: str) -> list:
    data = await _get("workout", {"start_date": d, "end_date": d})
    result = []
    for w in data.get("data", []):
        result.append({
            "activity": w.get("activity"),
            "duration_min": _duration_min(w.get("start_datetime"), w.get("end_datetime")),
            "calories": round(w.get("calories") or 0),
            "distance_km": round((w.get("distance") or 0) / 1000, 2),
            "intensity": w.get("intensity"),
        })
    return result


async def fetch_stress(d: str) -> dict:
    data = await _one("daily_stress", d)
    if not data:
        return {}
    return {
        "summary": data.get("day_summary"),
        "stress_min": round((data.get("stress_high") or 0) / 60),
        "recovery_min": round((data.get("recovery_high") or 0) / 60),
    }


async def fetch_spo2(d: str) -> dict:
    data = await _one("daily_spo2", d)
    if not data:
        return {}
    return {
        "avg_spo2": round(data.get("spo2_percentage", {}).get("average") or 0, 1),
        "breathing_disturbance_index": data.get("breathing_disturbance_index"),
    }


# ─────────────────────── Main sync function ────────────────────

async def sync_all(d: str = None) -> dict:
    """Fetch all Oura data for the given date in parallel."""
    d = d or date.today().isoformat()

    sleep, readiness, activity, workouts, stress, spo2 = await asyncio.gather(
        fetch_sleep(d),
        fetch_readiness(d),
        fetch_activity(d),
        fetch_workouts(d),
        fetch_stress(d),
        fetch_spo2(d),
        return_exceptions=True,
    )

    def _safe(v):
        return v if not isinstance(v, Exception) else {"error": str(v)}

    return {
        "date": d,
        "sleep": _safe(sleep),
        "readiness": _safe(readiness),
        "activity": _safe(activity),
        "workouts": _safe(workouts),
        "stress": _safe(stress),
        "spo2": _safe(spo2),
    }


# ─────────────────────── Tool handler ──────────────────────────

async def handle_tool(args: dict) -> dict:
    """
    Called from bot.py when Claude invokes the sync_oura tool.
    Fetches data, saves to Supabase (oura_data + daily_logs).
    """
    d = args.get("date") or date.today().isoformat()
    data = await sync_all(d)

    # Always save raw Oura data to oura_data table
    db.upsert_oura(d, data)

    if args.get("write_to_log", True):
        sleep = data.get("sleep", {})
        readiness = data.get("readiness", {})

        if sleep.get("hours"):
            log = db.get_log(d)
            log["sleep"] = {
                "hours": sleep.get("hours"),
                "quality": _score_to_quality(readiness.get("score")),
                "bed_time": sleep.get("bedtime_start"),
                "wake_time": sleep.get("bedtime_end"),
                "oura": {
                    "sleep_score": sleep.get("score"),
                    "readiness_score": readiness.get("score"),
                    "hrv": sleep.get("avg_hrv"),
                    "resting_hr": sleep.get("resting_hr"),
                    "deep_min": sleep.get("deep_min"),
                    "rem_min": sleep.get("rem_min"),
                    "efficiency": sleep.get("efficiency"),
                },
            }
            db.upsert_log(d, log)
            data["log_updated"] = True

    return data
