"""
Libre 3 integration via LibreLinkUp.

Abbott's LibreLinkUp is the "Share" feature inside LibreLink — designed for
caregivers. We log in with a *second* account that the user shared their
sensor data to, then poll the (community-reverse-engineered) REST API.

Endpoints used:
  POST /llu/auth/login            → JWT + user.id (+ optional region redirect)
  GET  /llu/connections           → list of patients you have access to + current glucose
  GET  /llu/connections/{pid}/graph → ~12h of readings, point every ~15 min

Glucose values come in mg/dL (ValueInMgPerDl). mmol/L = mg_dl / 18.0182.

Persistence: every reading is upserted into Supabase `glucose_readings`,
unique on `ts`. So re-sync is idempotent.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

import db

log = logging.getLogger(__name__)

BASE = Path(__file__).parent.parent
CRED_FILE = BASE / "config/libre_credentials.json"
TOKEN_CACHE = BASE / "config/libre_token.json"

DEFAULT_API_BASE = "https://api.libreview.io"
LOCAL_TZ = ZoneInfo("Europe/Amsterdam")

# Headers required by the LLU API. The `version` value must be ≥ whatever
# Abbott currently sets as minimumVersion server-side — when they raise it,
# any GET will return {"status": 920, "data": {"minimumVersion": "x.y.z"}}.
# Last bump: 2026-05-11 (4.12.0 → 4.16.0).
_BASE_HEADERS = {
    "product": "llu.ios",
    "version": "4.16.0",
    "content-type": "application/json",
    "accept-encoding": "gzip",
    "cache-control": "no-cache",
    "connection": "Keep-Alive",
}

# TrendArrow integer → semantic label
_TREND = {
    1: "falling_fast",
    2: "falling",
    3: "stable",
    4: "rising",
    5: "rising_fast",
}


# ─────────────────────── Credentials & token cache ───────────────


def _credentials() -> tuple[str, str]:
    """
    Return (email, password) for LibreLinkUp.
    Looks in env vars first (LIBRE_EMAIL/LIBRE_PASSWORD), then config file.
    """
    email = os.environ.get("LIBRE_EMAIL", "").strip()
    password = os.environ.get("LIBRE_PASSWORD", "").strip()
    if email and password:
        return email, password
    if CRED_FILE.exists():
        data = json.loads(CRED_FILE.read_text())
        return data["email"], data["password"]
    raise RuntimeError(
        "LibreLinkUp credentials not configured. Set LIBRE_EMAIL/LIBRE_PASSWORD "
        f"or create {CRED_FILE} with {{\"email\": ..., \"password\": ...}}."
    )


@dataclass
class _Session:
    api_base: str
    token: str
    user_id: str
    expires: int  # unix seconds

    @property
    def account_id_hash(self) -> str:
        return hashlib.sha256(self.user_id.encode()).hexdigest()

    def auth_headers(self) -> dict:
        return {
            **_BASE_HEADERS,
            "authorization": f"Bearer {self.token}",
            "account-id": self.account_id_hash,
        }


def _load_cached_session() -> _Session | None:
    if not TOKEN_CACHE.exists():
        return None
    try:
        d = json.loads(TOKEN_CACHE.read_text())
        s = _Session(**d)
        # Refresh 1h before expiry to be safe
        if s.expires - time.time() > 3600:
            return s
    except Exception:
        return None
    return None


def _save_session(s: _Session) -> None:
    TOKEN_CACHE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_CACHE.write_text(
        json.dumps(
            {
                "api_base": s.api_base,
                "token": s.token,
                "user_id": s.user_id,
                "expires": s.expires,
            },
            indent=2,
        )
    )


# ─────────────────────── Auth ─────────────────────────────────────


def _mask_email(e: str) -> str:
    """'natka@toloka.ai' -> 'na***@toloka.ai' for safe logging."""
    if "@" not in e:
        return "***"
    name, dom = e.split("@", 1)
    return f"{name[:2]}***@{dom}"


async def _login(client: httpx.AsyncClient) -> _Session:
    email, password = _credentials()
    api_base = DEFAULT_API_BASE

    log.info(
        "LibreLinkUp login: email=%s password_len=%d",
        _mask_email(email),
        len(password),
    )

    # Up to 2 attempts: the first may return a region redirect.
    for _ in range(2):
        r = await client.post(
            f"{api_base}/llu/auth/login",
            json={"email": email, "password": password},
            headers=_BASE_HEADERS,
        )
        r.raise_for_status()
        payload = r.json()
        status = payload.get("status")

        if status == 2:
            raise RuntimeError(
                f"LibreLinkUp отклонил креды для {_mask_email(email)}. "
                "Проверь в Railway: LIBRE_EMAIL/LIBRE_PASSWORD должны быть от "
                "ВТОРОГО аккаунта (LibreLinkUp), который ты регистрировала отдельным "
                "приложением и которому расшарила сенсор из основного LibreLink. "
                "Если использовала +alias (foo+libre@gmail.com) — проверь, что и при "
                "регистрации, и в Railway точно одинаковое написание, без лишних пробелов."
            )
        if status == 4:
            raise RuntimeError(
                f"LibreLinkUp аккаунт заблокирован или требует подтверждения условий. "
                f"Зайди в LibreLinkUp на телефоне ({_mask_email(email)}) и пройди шаги. "
                f"Полный ответ: {payload}"
            )
        if status not in (0, None):
            raise RuntimeError(f"Libre login failed (status={status}): {payload}")

        data = payload.get("data", {})

        # Region redirect: server tells us which regional endpoint to use.
        if data.get("redirect") and data.get("region"):
            region = data["region"]
            api_base = f"https://api-{region}.libreview.io"
            log.info("LibreLinkUp region redirect → %s", api_base)
            continue

        ticket = data.get("authTicket") or {}
        user = data.get("user") or {}
        token = ticket.get("token")
        user_id = user.get("id")
        expires = int(ticket.get("expires") or (time.time() + 3600))

        if not token or not user_id:
            raise RuntimeError(f"Libre login: malformed response: {payload}")

        s = _Session(api_base=api_base, token=token, user_id=user_id, expires=expires)
        _save_session(s)
        return s

    raise RuntimeError("Libre login: redirect loop")


async def _get_session(client: httpx.AsyncClient, force: bool = False) -> _Session:
    if not force:
        cached = _load_cached_session()
        if cached:
            return cached
    return await _login(client)


# ─────────────────────── API calls ────────────────────────────────


async def _api_get(client: httpx.AsyncClient, path: str) -> dict:
    """GET with auth, retrying once after re-login on 401."""
    s = await _get_session(client)
    for attempt in range(2):
        r = await client.get(f"{s.api_base}{path}", headers=s.auth_headers())
        if r.status_code == 401 and attempt == 0:
            log.info("LibreLinkUp 401, re-logging in")
            s = await _get_session(client, force=True)
            continue
        if r.status_code in (403, 401) or r.status_code >= 400:
            body_text = r.text[:500]
            # Try to parse Abbott's structured error.
            try:
                body_json = r.json()
            except Exception:
                body_json = {}
            inner_status = body_json.get("status")
            inner_data = body_json.get("data") or {}

            if inner_status == 920:
                required = inner_data.get("minimumVersion", "?")
                raise RuntimeError(
                    f"LibreLinkUp требует minimumVersion={required}, а мы шлём "
                    f"version={_BASE_HEADERS['version']}. Обнови строку "
                    f"`version` в bot/libre.py:_BASE_HEADERS на {required} (или выше) "
                    "и перезапусти бота."
                )
            if r.status_code == 403:
                raise RuntimeError(
                    f"LibreLinkUp 403 on {path}. Abbott принял логин, но запретил "
                    "доступ к данным. Возможные причины: "
                    "(1) invitation от основного LibreLink ещё не accepted в "
                    "LibreLinkUp на iPhone — проверь и нажми Accept; "
                    "(2) обновление Terms & Conditions — открой LibreLinkUp, "
                    "согласись; "
                    "(3) email второго аккаунта не подтверждён. "
                    f"Ответ Abbott: {body_text}"
                )
            raise RuntimeError(
                f"LibreLinkUp {r.status_code} on {path}: {body_text}"
            )
        return r.json()
    raise RuntimeError(f"LibreLinkUp GET {path} failed after retry")


async def _get_patient_id(client: httpx.AsyncClient) -> tuple[str, dict | None]:
    """
    Returns (patient_id, current_glucose_dict or None) from /llu/connections.
    Picks the first connection (your own self-share). If you have multiple
    shared patients, this would need to be parameterised.
    """
    payload = await _api_get(client, "/llu/connections")
    conns = payload.get("data") or []
    if not conns:
        raise RuntimeError(
            "LibreLinkUp returned no connections. Have you enabled Share to "
            "this account inside the LibreLink iOS app?"
        )
    c = conns[0]
    return c.get("patientId"), c.get("glucoseMeasurement")


# ─────────────────────── Parsing ──────────────────────────────────


_TS_FMT = "%m/%d/%Y %I:%M:%S %p"


def _parse_factory_ts(ts_str: str) -> datetime:
    """FactoryTimestamp is the sensor reading time in UTC."""
    return datetime.strptime(ts_str, _TS_FMT).replace(tzinfo=timezone.utc)


def _parse_local_ts(ts_str: str) -> datetime:
    """Timestamp is the same instant as FactoryTimestamp, but expressed in the
    user's local wall-clock time. Used only as a fallback when FactoryTimestamp
    is missing from the payload."""
    return datetime.strptime(ts_str, _TS_FMT).replace(tzinfo=LOCAL_TZ)


def _measurement_to_reading(m: dict) -> dict:
    """Convert one LLU measurement dict to our flat record."""
    mg_dl = m.get("ValueInMgPerDl")
    if mg_dl is None:
        return {}
    factory = m.get("FactoryTimestamp")
    ts = _parse_factory_ts(factory) if factory else _parse_local_ts(m["Timestamp"])
    return {
        "ts": ts.isoformat(),
        "mg_dl": float(mg_dl),
        "mmol_l": round(float(mg_dl) / 18.0182, 2),
        "trend": _TREND.get(m.get("TrendArrow")),
        "source": "libre3",
    }


# ─────────────────────── Sync ─────────────────────────────────────


async def sync_recent() -> dict:
    """
    Fetch the latest CGM data and upsert into Supabase.
    Returns a summary: number of readings written, latest reading, time range.
    """
    async with httpx.AsyncClient(timeout=20) as client:
        patient_id, current_meas = await _get_patient_id(client)
        graph_payload = await _api_get(client, f"/llu/connections/{patient_id}/graph")

    data = graph_payload.get("data") or {}
    points = data.get("graphData") or []

    # The current reading is more recent than the last graph point — include it.
    current = data.get("connection", {}).get("glucoseMeasurement") or current_meas

    readings = [r for r in (_measurement_to_reading(p) for p in points) if r]
    if current:
        cur_reading = _measurement_to_reading(current)
        if cur_reading:
            readings.append(cur_reading)

    written = db.upsert_glucose(readings)

    if not readings:
        return {"written": 0, "latest": None, "range": None}

    latest = max(readings, key=lambda r: r["ts"])
    earliest = min(readings, key=lambda r: r["ts"])
    return {
        "written": written,
        "latest": latest,
        "range": {"from": earliest["ts"], "to": latest["ts"], "count": len(readings)},
    }


# ─────────────────────── Window queries ───────────────────────────


def _to_utc(local_dt: datetime) -> datetime:
    if local_dt.tzinfo is None:
        local_dt = local_dt.replace(tzinfo=LOCAL_TZ)
    return local_dt.astimezone(timezone.utc)


def get_window(start_local: datetime, end_local: datetime) -> list[dict]:
    """Return glucose readings between two local-time datetimes."""
    s = _to_utc(start_local).isoformat()
    e = _to_utc(end_local).isoformat()
    return db.get_glucose_window(s, e)


def window_stats(readings: list[dict]) -> dict:
    if not readings:
        return {}
    vals = [float(r["mmol_l"]) for r in readings]
    in_range = sum(1 for v in vals if 3.9 <= v <= 7.8)
    return {
        "count": len(vals),
        "mean": round(sum(vals) / len(vals), 2),
        "min": round(min(vals), 2),
        "max": round(max(vals), 2),
        "time_in_range_pct": round(100 * in_range / len(vals)),
    }


def analyse_meal_response(meal_ts_local: datetime) -> dict:
    """
    Compute postprandial glucose response around a meal eaten at meal_ts_local.
    Baseline = mean of readings in [-30, 0] min before the meal.
    Looks at +0..+120 min for peak.
    """
    baseline_window = get_window(
        meal_ts_local - timedelta(minutes=30), meal_ts_local
    )
    post_window = get_window(
        meal_ts_local, meal_ts_local + timedelta(minutes=120)
    )

    if not post_window:
        return {"available": False, "reason": "no_postprandial_data"}

    if baseline_window:
        baseline = round(
            sum(float(r["mmol_l"]) for r in baseline_window) / len(baseline_window), 2
        )
    else:
        # Fall back to the first postprandial reading as baseline.
        baseline = float(post_window[0]["mmol_l"])

    peak_reading = max(post_window, key=lambda r: float(r["mmol_l"]))
    peak = float(peak_reading["mmol_l"])
    peak_ts = datetime.fromisoformat(peak_reading["ts"]).astimezone(LOCAL_TZ)
    time_to_peak_min = round((peak_ts - meal_ts_local).total_seconds() / 60)

    # Time back to baseline (within +0.5 mmol of baseline) after the peak.
    return_to_baseline_min = None
    seen_peak = False
    for r in post_window:
        v = float(r["mmol_l"])
        if v >= peak - 0.01:
            seen_peak = True
            continue
        if seen_peak and v <= baseline + 0.5:
            t = datetime.fromisoformat(r["ts"]).astimezone(LOCAL_TZ)
            return_to_baseline_min = round((t - meal_ts_local).total_seconds() / 60)
            break

    return {
        "available": True,
        "baseline_mmol": baseline,
        "peak_mmol": round(peak, 2),
        "delta_mmol": round(peak - baseline, 2),
        "time_to_peak_min": time_to_peak_min,
        "return_to_baseline_min": return_to_baseline_min,
        "readings_in_window": len(post_window),
    }


# ─────────────────────── Weekly aggregation ───────────────────────


def _meal_response_row(d: date, meal: dict) -> dict | None:
    """
    Build one row {meal context + postprandial response} or None if
    the meal has no usable timestamp.
    """
    t = meal.get("time")
    if not t:
        return None
    try:
        hh, mm = (int(x) for x in t.split(":")[:2])
    except Exception:
        return None
    meal_ts = datetime(d.year, d.month, d.day, hh, mm, tzinfo=LOCAL_TZ)

    row = {
        "date": d.isoformat(),
        "time": t,
        "hour": hh,
        "description": meal.get("description"),
        "calories": meal.get("calories"),
        "protein_g": meal.get("protein"),
        "fat_g": meal.get("fat"),
        "carbs_g": meal.get("carbs"),
        "fiber_g": meal.get("fiber"),
        "glycemic_index": meal.get("glycemic_index"),
        "response": analyse_meal_response(meal_ts),
    }
    return row


def build_weekly_data(end_date: date | None = None) -> dict:
    """
    Collect 7 days of meals × postprandial responses + day context.
    Returns a structured dict ready to feed into an LLM.
    """
    end = end_date or _today_local()
    start = end - timedelta(days=6)

    # Pull all glucose readings for the whole week up front, for week stats.
    week_start_local = datetime(start.year, start.month, start.day, 0, 0, tzinfo=LOCAL_TZ)
    week_end_local = datetime(end.year, end.month, end.day, 23, 59, tzinfo=LOCAL_TZ)
    week_readings = get_window(week_start_local, week_end_local)
    week_stats = window_stats(week_readings)

    if week_readings:
        vals = [float(r["mmol_l"]) for r in week_readings]
        mean = week_stats["mean"]
        # Coefficient of variation: lower = more stable
        if mean > 0:
            sd = (sum((v - mean) ** 2 for v in vals) / len(vals)) ** 0.5
            week_stats["cv_pct"] = round(100 * sd / mean, 1)
        week_stats["episodes_above_10"] = sum(1 for v in vals if v >= 10.0)
        week_stats["episodes_below_3_9"] = sum(1 for v in vals if v < 3.9)

    # Per-day meals + context
    days = []
    meal_rows = []
    cur = start
    while cur <= end:
        log_row = db.get_log(cur.isoformat())
        meals = log_row.get("meals") or []
        training = log_row.get("training") or []
        sleep = log_row.get("sleep") or {}

        day_meal_rows = []
        for m in meals:
            row = _meal_response_row(cur, m)
            if row:
                day_meal_rows.append(row)
                meal_rows.append(row)

        days.append({
            "date": cur.isoformat(),
            "weekday": cur.strftime("%A"),
            "meal_count": len(day_meal_rows),
            "workout_done": bool(training),
            "workout_types": [t.get("type") for t in training] if training else [],
            "sleep_hours": sleep.get("hours"),
            "sleep_quality": sleep.get("quality"),
            "weight_morning": log_row.get("weight_morning"),
        })
        cur += timedelta(days=1)

    # Coverage stats — how many meals actually had postprandial readings
    total_meals = len(meal_rows)
    meals_with_data = sum(1 for r in meal_rows if r["response"].get("available"))

    return {
        "period": {"start": start.isoformat(), "end": end.isoformat(), "days": 7},
        "week_stats": week_stats,
        "data_coverage": {
            "total_meals_logged": total_meals,
            "meals_with_glucose_response": meals_with_data,
            "coverage_pct": (
                round(100 * meals_with_data / total_meals) if total_meals else 0
            ),
        },
        "days": days,
        "meals": meal_rows,
    }


# ─────────────────────── Chart ────────────────────────────────────


def _meals_for_date(d: date) -> list[dict]:
    """
    Return list of {time_local: datetime, label: str} for meals from daily_logs
    on date d. Times are interpreted as Europe/Amsterdam local time.
    """
    log_row = db.get_log(d.isoformat())
    out = []
    for m in log_row.get("meals") or []:
        t = m.get("time")
        desc = m.get("description") or ""
        if not t:
            continue
        try:
            hh, mm = (int(x) for x in t.split(":")[:2])
        except Exception:
            continue
        local = datetime(d.year, d.month, d.day, hh, mm, tzinfo=LOCAL_TZ)
        out.append({"time_local": local, "label": desc})
    return out


def make_chart(
    start_local: datetime,
    end_local: datetime,
    meals: list[dict] | None = None,
    title: str | None = None,
) -> bytes:
    """
    Render a glucose curve as PNG bytes.
    meals: list of {"time_local": datetime, "label": str} — drawn as vertical markers.
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates

    readings = get_window(start_local, end_local)
    if not readings:
        # Empty placeholder chart
        fig, ax = plt.subplots(figsize=(9, 4))
        ax.text(0.5, 0.5, "Нет данных глюкозы в этом окне",
                ha="center", va="center", transform=ax.transAxes, fontsize=12)
        ax.axis("off")
        buf = io.BytesIO()
        fig.savefig(buf, format="png", bbox_inches="tight", dpi=120)
        plt.close(fig)
        return buf.getvalue()

    xs = [datetime.fromisoformat(r["ts"]).astimezone(LOCAL_TZ) for r in readings]
    ys = [float(r["mmol_l"]) for r in readings]

    fig, ax = plt.subplots(figsize=(10, 4.2))

    # Target band 3.9–7.8 mmol/L (Attia / ADA non-diabetic)
    ax.axhspan(3.9, 7.8, color="#d4edda", alpha=0.5, label="Целевой диапазон")
    ax.axhspan(7.8, 10.0, color="#fff3cd", alpha=0.4)
    ax.axhspan(10.0, max(12, max(ys) + 1), color="#f8d7da", alpha=0.3)
    ax.axhspan(0, 3.9, color="#f8d7da", alpha=0.3)

    ax.plot(xs, ys, color="#2c3e50", linewidth=2)
    ax.scatter(xs, ys, color="#2c3e50", s=8, zorder=3)

    # Meal markers
    y_top = max(ys) + 0.8
    for m in (meals or []):
        t = m["time_local"]
        if not (start_local <= t <= end_local):
            continue
        ax.axvline(t, color="#e67e22", linewidth=1.2, alpha=0.7)
        ax.annotate(
            m["label"][:30],
            xy=(t, y_top),
            xytext=(2, -2),
            textcoords="offset points",
            fontsize=8,
            color="#d35400",
            rotation=90,
            va="top",
            ha="left",
        )

    ax.set_ylabel("Глюкоза, ммоль/л")
    ax.set_ylim(bottom=max(2.5, min(ys) - 0.5), top=max(11, max(ys) + 1.5))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M", tz=LOCAL_TZ))
    ax.xaxis.set_major_locator(mdates.HourLocator(interval=2))
    ax.grid(True, alpha=0.2)

    if title:
        ax.set_title(title, fontsize=11)

    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight", dpi=120)
    plt.close(fig)
    return buf.getvalue()


# ─────────────────────── Tool handler ─────────────────────────────


def _today_local() -> date:
    return datetime.now(LOCAL_TZ).date()


def _format_trend(t: str | None) -> str:
    return {
        "falling_fast": "↓↓",
        "falling": "↓",
        "stable": "→",
        "rising": "↑",
        "rising_fast": "↑↑",
    }.get(t or "", "")


async def handle_tool(name: str, args: dict) -> dict:
    """
    Async tool dispatcher for Libre tools. Called from bot.py when Claude
    invokes one of the glucose tools.
    """
    if name == "sync_glucose":
        return await sync_recent()

    if name == "get_glucose_now":
        # Make sure we have fresh data first
        try:
            await sync_recent()
        except Exception as e:
            log.warning("sync before get_glucose_now failed: %s", e)
        latest = db.get_latest_glucose()
        if not latest:
            return {"available": False, "reason": "no_readings"}
        ts_local = datetime.fromisoformat(latest["ts"]).astimezone(LOCAL_TZ)
        age_min = round((datetime.now(LOCAL_TZ) - ts_local).total_seconds() / 60)
        return {
            "available": True,
            "mmol_l": float(latest["mmol_l"]),
            "mg_dl": float(latest["mg_dl"]),
            "trend": latest.get("trend"),
            "trend_arrow": _format_trend(latest.get("trend")),
            "time_local": ts_local.strftime("%H:%M"),
            "age_minutes": age_min,
        }

    if name == "glucose_around_meal":
        meal_index = args.get("meal_index", -1)
        log_row = db.get_log()
        meals = log_row.get("meals") or []
        if not meals:
            return {"available": False, "reason": "no_meals_today"}
        try:
            meal = meals[meal_index]
        except IndexError:
            return {"available": False, "reason": f"meal_index_out_of_range (have {len(meals)} meals)"}

        t_str = meal.get("time")
        if not t_str:
            return {"available": False, "reason": "meal_has_no_time"}

        d = _today_local()
        hh, mm = (int(x) for x in t_str.split(":")[:2])
        meal_ts = datetime(d.year, d.month, d.day, hh, mm, tzinfo=LOCAL_TZ)

        # Ensure we've fetched the latest data first
        try:
            await sync_recent()
        except Exception as e:
            log.warning("sync before glucose_around_meal failed: %s", e)

        analysis = analyse_meal_response(meal_ts)
        return {
            "meal": {
                "index": meal_index,
                "time": t_str,
                "description": meal.get("description"),
            },
            **analysis,
        }

    if name == "glucose_chart":
        d = _today_local()
        hours = int(args.get("hours", 12))
        end_local = datetime.now(LOCAL_TZ)
        start_local = end_local - timedelta(hours=hours)

        # Refresh first so chart shows latest data
        try:
            await sync_recent()
        except Exception as e:
            log.warning("sync before glucose_chart failed: %s", e)

        readings = get_window(start_local, end_local)
        meals = _meals_for_date(d)
        # Also include meals from yesterday if window spans midnight
        if start_local.date() < d:
            meals = _meals_for_date(start_local.date()) + meals

        png = make_chart(
            start_local,
            end_local,
            meals=meals,
            title=f"Глюкоза за последние {hours}ч",
        )
        stats = window_stats(readings)

        return {
            "type": "chart",
            "png_b64": base64.standard_b64encode(png).decode(),
            "caption": (
                f"Глюкоза за {hours}ч · "
                f"среднее {stats.get('mean', '—')} ммоль/л · "
                f"в диапазоне {stats.get('time_in_range_pct', '—')}% · "
                f"мин {stats.get('min', '—')} / макс {stats.get('max', '—')}"
                if stats else f"Глюкоза за {hours}ч — данных нет"
            ),
            "stats": stats,
            "meals_overlaid": len(meals),
        }

    return {"error": f"Unknown libre tool: {name}"}
