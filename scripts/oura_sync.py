#!/usr/bin/env python3
"""
Oura Daily Sync — Health OS
────────────────────────────
Сохраняет ВСЕ данные из Oura API в два места:
  • data/oura/{YYYY-MM-DD}.yaml  — полный Oura-лог
  • data/tactical/logs/{YYYY-MM-DD}.yaml  — обновляет раздел sleep

Доступные данные:
  sleep     — часы, стадии, HRV, ЧСС, SpO2, латентность, беспокойство, дыхание
  readiness — score + contributors + температура тела
  activity  — шаги, km, zone2, калории, сидячее время (batch-запрос: range only)
  stress    — уровень стресса за день
  resilience — уровень устойчивости
  vascular_age — биологический возраст сосудов

Примечание: menstrual_cycle — API 404 (не включено в настройках Oura или
не доступно). Включи Cycle Insights в приложении Oura → Settings → Features.

Usage:
  python3 scripts/oura_sync.py              # сегодня
  python3 scripts/oura_sync.py 2026-03-05   # конкретная дата
  python3 scripts/oura_sync.py --days 7     # последние 7 дней
  python3 scripts/oura_sync.py --days 90    # последние 90 дней (бэкфилл)

Cron (каждый день в 09:00):
  0 9 * * * "/Users/natka/Desktop/Cursor/Health OS/bot/venv/bin/python3" "/Users/natka/Desktop/Cursor/Health OS/scripts/oura_sync.py" >> "/Users/natka/Desktop/Cursor/Health OS/data/oura/sync.log" 2>&1
"""

import json
import sys
import os
import yaml
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError

BASE = Path(__file__).parent.parent
OURA_DIR = BASE / "data/oura"
LOGS_DIR = BASE / "data/tactical/logs"
TOKEN_FILE = BASE / "config/oura_token.txt"
API_BASE = "https://api.ouraring.com/v2/usercollection"


# ─── Auth ─────────────────────────────────────────────────────────────────────

def get_token() -> str:
    token = os.environ.get("OURA_TOKEN", "")
    if token:
        return token.strip()
    if TOKEN_FILE.exists():
        t = TOKEN_FILE.read_text().strip()
        if t and not t.startswith("#"):
            return t
    print("ERROR: токен Oura не найден.")
    print(f"  Положи токен в {TOKEN_FILE}  или  export OURA_TOKEN=...")
    sys.exit(1)


def api_get(endpoint: str, start: str, end: str, token: str) -> list:
    url = f"{API_BASE}/{endpoint}?start_date={start}&end_date={end}"
    req = Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urlopen(req, timeout=15) as r:
            return json.loads(r.read()).get("data", [])
    except HTTPError as e:
        if e.code == 401:
            print("ERROR: неверный токен Oura (401). Обнови токен.")
            sys.exit(1)
        if e.code == 404:
            return []
        print(f"  WARN: {endpoint} → HTTP {e.code}")
        return []
    except URLError as e:
        print(f"ERROR: нет доступа к Oura API — {e.reason}")
        sys.exit(1)


def _first(lst: list) -> dict:
    return next((x for x in lst if x is not None), {}) or {}


def _by_date(lst: list, key: str = "day") -> dict:
    """Index list of API items by date field."""
    return {item[key]: item for item in lst if item.get(key)}


# ─── Helpers ──────────────────────────────────────────────────────────────────

def fmt_hhmm(iso_str: str):
    if not iso_str:
        return None
    try:
        return datetime.fromisoformat(iso_str).astimezone().strftime("%H:%M")
    except Exception:
        return iso_str[11:16] if len(iso_str) > 15 else None


def score_quality(score) -> str:
    if score is None:
        return "ok"
    return "good" if score >= 80 else ("ok" if score >= 60 else "poor")


def resilience_label(level: str) -> str:
    return {
        "inadequate": "inadequate", "adequate": "adequate",
        "solid": "solid", "strong": "strong", "exceptional": "exceptional",
    }.get(level or "", level or "unknown")


def _remove_none(d):
    if isinstance(d, dict):
        return {k: _remove_none(v) for k, v in d.items() if v is not None}
    if isinstance(d, list):
        return [_remove_none(i) for i in d]
    return d


# ─── Pre-fetch batch data for a date range ───────────────────────────────────

def prefetch_range(start: str, end: str, token: str) -> dict:
    """
    Fetch activity, workouts, and sleep sessions for the whole range at once.
    Returns dict with keys: activity_by_date, workouts_by_date, sleep_raw.

    Note: daily_activity only works as a range query, not single-day.
    """
    activity_items = api_get("daily_activity", start, end, token)
    workout_items  = api_get("workout",         start, end, token)

    # Sleep sessions: fetch prev day too so night sessions are captured
    prev = (datetime.strptime(start, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
    sleep_items = api_get("sleep", prev, end, token)

    return {
        "activity":  _by_date(activity_items),
        "workouts":  workout_items,
        "sleep_raw": sleep_items,
    }


# ─── Parse one day ────────────────────────────────────────────────────────────

def build_day(d: str, token: str, batch: dict = None) -> dict:
    """
    Build full data dict for date d.
    If batch is provided, uses pre-fetched activity/sleep from it.
    Otherwise fetches everything fresh (single-day mode).
    """
    prev = (datetime.strptime(d, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
    result: dict = {"date": d}

    # ── Sleep sessions ─────────────────────────────────────────────────────
    if batch:
        sleep_sessions_all = [s for s in batch["sleep_raw"] if s.get("day") == d]
    else:
        raw = api_get("sleep", prev, d, token)
        sleep_sessions_all = [s for s in raw if s.get("day") == d]

    # Separate long sleep from naps
    long_sessions = [s for s in sleep_sessions_all if s.get("type") == "long_sleep"]
    nap_sessions  = [s for s in sleep_sessions_all if s.get("type") != "long_sleep"]

    # Daily scores
    daily_sleep  = _first(api_get("daily_sleep",    d, d, token))
    readiness    = _first(api_get("daily_readiness", d, d, token))
    stress       = _first(api_get("daily_stress",    d, d, token))
    resilience   = _first(api_get("daily_resilience",d, d, token))
    cardio_age   = _first(api_get("daily_cardiovascular_age", d, d, token))
    spo2         = _first(api_get("daily_spo2",      d, d, token))

    # ── Parse sleep ────────────────────────────────────────────────────────
    sleep_score = daily_sleep.get("score")
    sleep_block: dict = {"score": sleep_score, "quality": score_quality(sleep_score)}

    sessions = long_sessions or sleep_sessions_all  # fallback if no long_sleep tagged
    if sessions:
        main_s    = max(sessions, key=lambda s: s.get("total_sleep_duration") or 0)
        total_sec = sum(s.get("total_sleep_duration") or 0 for s in sessions)

        sleep_block.update({
            "hours":      round(total_sec / 3600, 1),
            "bed_time":   fmt_hhmm(main_s.get("bedtime_start")),
            "wake_time":  fmt_hhmm(main_s.get("bedtime_end")),
            "efficiency": main_s.get("efficiency"),
            "latency_min": round((main_s.get("latency") or 0) / 60) or None,
            "restless_periods": main_s.get("restless_periods"),
            "breath_avg": round(main_s.get("average_breath") or 0, 1) or None,
            "stages_min": {
                "deep":  round(sum(s.get("deep_sleep_duration")  or 0 for s in sessions) / 60),
                "rem":   round(sum(s.get("rem_sleep_duration")   or 0 for s in sessions) / 60),
                "light": round(sum(s.get("light_sleep_duration") or 0 for s in sessions) / 60),
                "awake": round(sum(s.get("awake_time")           or 0 for s in sessions) / 60),
            },
            "biometrics": {
                "hrv_avg":    main_s.get("average_hrv"),
                "hr_avg":     round(main_s.get("average_heart_rate") or 0) or None,
                "hr_lowest":  main_s.get("lowest_heart_rate"),
                "spo2":       round((spo2.get("spo2_percentage") or {}).get("average") or 0, 1) or None,
                "breathing_disturbance": spo2.get("breathing_disturbance_index"),
            },
        })

    # Naps
    if nap_sessions:
        sleep_block["naps"] = [
            {
                "start": fmt_hhmm(n.get("bedtime_start")),
                "duration_min": round((n.get("total_sleep_duration") or 0) / 60),
            }
            for n in nap_sessions if (n.get("total_sleep_duration") or 0) > 600
        ] or None

    result["sleep"] = sleep_block

    # ── Readiness ──────────────────────────────────────────────────────────
    if readiness:
        result["readiness"] = {
            "score":   readiness.get("score"),
            "quality": score_quality(readiness.get("score")),
            "temperature_deviation":       readiness.get("temperature_deviation"),
            "temperature_trend_deviation": readiness.get("temperature_trend_deviation"),
            "contributors": readiness.get("contributors", {}),
        }

    # ── Stress & Resilience ────────────────────────────────────────────────
    stress_block: dict = {}
    if stress:
        stress_block["summary"] = stress.get("day_summary")
        sh = stress.get("stress_high") or 0
        rh = stress.get("recovery_high") or 0
        if sh: stress_block["stress_high_min"]    = round(sh / 60)
        if rh: stress_block["recovery_high_min"]  = round(rh / 60)
    if resilience:
        stress_block["resilience"] = resilience_label(resilience.get("level"))
        contrib = resilience.get("contributors", {})
        if contrib:
            stress_block["resilience_contributors"] = {
                "sleep_recovery":   contrib.get("sleep_recovery"),
                "daytime_recovery": contrib.get("daytime_recovery"),
                "stress_load":      contrib.get("stress"),
            }
    if stress_block:
        result["stress_recovery"] = stress_block

    # ── Activity (from batch or single fetch) ──────────────────────────────
    if batch:
        act = batch["activity"].get(d, {}) or {}
    else:
        # Single-day fallback: try a small range around d
        act_list = api_get("daily_activity", prev, d, token)
        act = next((x for x in act_list if x.get("day") == d), {}) or {}

    if act:
        result["activity"] = {
            "score":            act.get("score"),
            "steps":            act.get("steps"),
            "walk_km":          round((act.get("equivalent_walking_distance") or 0) / 1000, 1) or None,
            "active_calories":  act.get("active_calories"),
            "total_calories":   act.get("total_calories"),
            "zone2_min":        round((act.get("medium_activity_time") or 0) / 60) or None,
            "high_activity_min":round((act.get("high_activity_time") or 0) / 60) or None,
            "sedentary_hours":  round((act.get("sedentary_time") or 0) / 3600, 1) or None,
            "inactivity_alerts": act.get("inactivity_alerts"),
        }

    # ── Workouts ───────────────────────────────────────────────────────────
    if batch:
        day_workouts = [w for w in batch["workouts"] if w.get("day") == d]
    else:
        day_workouts = api_get("workout", d, d, token)

    if day_workouts:
        result["workouts"] = []
        for w in day_workouts:
            entry = {"activity": w.get("activity"), "intensity": w.get("intensity")}
            if w.get("start_datetime") and w.get("end_datetime"):
                try:
                    s = datetime.fromisoformat(w["start_datetime"])
                    e = datetime.fromisoformat(w["end_datetime"])
                    entry["start"] = fmt_hhmm(w["start_datetime"])
                    entry["duration_min"] = round((e - s).total_seconds() / 60)
                except Exception:
                    pass
            if w.get("calories"):  entry["calories"]    = round(w["calories"])
            if w.get("distance"):  entry["distance_km"] = round(w["distance"] / 1000, 2)
            result["workouts"].append(entry)

    # ── Cardiovascular age ─────────────────────────────────────────────────
    if cardio_age.get("vascular_age"):
        result["cardiovascular"] = {"vascular_age": cardio_age["vascular_age"]}

    return result


# ─── Write files ──────────────────────────────────────────────────────────────

def save_oura_log(data: dict) -> Path:
    OURA_DIR.mkdir(parents=True, exist_ok=True)
    path = OURA_DIR / f"{data['date']}.yaml"
    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(_remove_none(data), f, allow_unicode=True,
                  default_flow_style=False, sort_keys=False)
    return path


def update_tactical_log(data: dict):
    d = data["date"]
    sleep = data.get("sleep", {})
    if not sleep.get("hours"):
        return None

    path = LOGS_DIR / f"{d}.yaml"
    LOGS_DIR.mkdir(parents=True, exist_ok=True)

    if path.exists():
        with open(path, encoding="utf-8") as f:
            log = yaml.safe_load(f) or {}
    else:
        log = {"date": d, "weight_morning": None, "meals": [],
               "training": [], "sleep": None, "notes": ""}

    readiness = data.get("readiness", {})
    bio = sleep.get("biometrics", {}) or {}
    log["sleep"] = _remove_none({
        "hours":    sleep.get("hours"),
        "quality":  sleep.get("quality", "ok"),
        "bed_time": sleep.get("bed_time"),
        "wake_time":sleep.get("wake_time"),
        "oura": {
            "sleep_score":     sleep.get("score"),
            "readiness_score": readiness.get("score"),
            "hrv":             bio.get("hrv_avg"),
            "resting_hr":      bio.get("hr_lowest"),
            "deep_min":        sleep.get("stages_min", {}).get("deep"),
            "rem_min":         sleep.get("stages_min", {}).get("rem"),
            "efficiency":      sleep.get("efficiency"),
            "latency_min":     sleep.get("latency_min"),
            "restless_periods":sleep.get("restless_periods"),
            "breath_avg":      sleep.get("breath_avg"),
        },
    })

    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(log, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
    return path


# ─── Warnings ─────────────────────────────────────────────────────────────────

def print_warnings(data: dict):
    sleep    = data.get("sleep", {})
    readiness= data.get("readiness", {})
    bio      = sleep.get("biometrics", {}) or {}
    hours    = sleep.get("hours")
    r_score  = readiness.get("score")
    temp     = readiness.get("temperature_deviation")
    spo2     = bio.get("spo2")
    bdi      = bio.get("breathing_disturbance")

    if hours and hours < 6:
        print(f"  ⚠️  Сон {hours}ч < 6ч → MAINTENANCE калории")
    elif hours and hours < 7:
        print(f"  ⚠️  Сон {hours}ч < 7ч → следи за энергией")
    if r_score and r_score < 70:
        print(f"  ⚠️  Readiness {r_score} < 70 → снизь интенсивность")
    if temp is not None and temp > 0.5:
        print(f"  ⚠️  Температура +{temp:.1f}°C → возможна болезнь")
    if spo2 and spo2 < 95:
        print(f"  ⚠️  SpO2 {spo2}% < 95% → проверь апноэ")
    if bdi and bdi > 5:
        print(f"  ⚠️  Нарушения дыхания ({bdi}) → обсуди с врачом")


# ─── Main ─────────────────────────────────────────────────────────────────────

def sync_one(d: str, token: str, batch: dict = None, verbose: bool = True) -> bool:
    if verbose:
        print(f"  [{d}] ", end="", flush=True)
    data = build_day(d, token, batch)
    save_oura_log(data)
    update_tactical_log(data)
    has_sleep = bool(data.get("sleep", {}).get("hours"))
    if verbose:
        sleep_h  = data.get("sleep", {}).get("hours", "—")
        r_score  = data.get("readiness", {}).get("score", "—")
        steps    = data.get("activity", {}).get("steps", "—") if data.get("activity") else "—"
        zone2    = data.get("activity", {}).get("zone2_min", "—") if data.get("activity") else "—"
        print(f"сон {sleep_h}ч  readiness {r_score}  шаги {steps}  zone2 {zone2}мин")
        print_warnings(data)
    return has_sleep


def main():
    args = sys.argv[1:]
    token = get_token()

    if args and args[0] == "--days":
        n = int(args[1]) if len(args) > 1 else 7
        today = date.today()
        start = (today - timedelta(days=n - 1)).isoformat()
        end   = today.isoformat()
        dates = [(today - timedelta(days=i)).isoformat() for i in range(n - 1, -1, -1)]

        print(f"Oura Sync — бэкфилл {n} дней ({start} → {end})")
        print("  Загружаю данные активности одним запросом...")
        batch = prefetch_range(start, end, token)
        print(f"  Активность: {len(batch['activity'])} дней  |  Тренировки: {len(batch['workouts'])}  |  Сон-сессии: {len(batch['sleep_raw'])}")
        print()

        found = sum(sync_one(d, token, batch) for d in dates)
        print(f"\nГотово: {found}/{len(dates)} дней с данными сна")
        return

    d = args[0] if args else date.today().isoformat()
    print(f"Oura Sync — {d}")
    # For single day, still use range batch
    prev = (datetime.strptime(d, "%Y-%m-%d") - timedelta(days=1)).isoformat()
    batch = prefetch_range(prev, d, token)
    sync_one(d, token, batch)
    print("Готово.")


if __name__ == "__main__":
    main()
