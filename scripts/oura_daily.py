#!/usr/bin/env python3
"""
Oura Daily Metrics — Health OS
Забирает ВСЕ доступные показатели из Oura API v2.

Метрики:
  • Сон     — часы, стадии, HRV, ЧСС, SpO2
  • Готовность — readiness score + contributors
  • Стресс  — уровень за день, время в стрессе/восстановлении
  • Resilience — уровень устойчивости
  • Активность — шаги, калории, расстояние (за вчера)
  • Сосудистый возраст

Usage:
  python3 scripts/oura_daily.py              # данные на сегодня (утренний чекин)
  python3 scripts/oura_daily.py 2026-02-26  # конкретная дата
"""

import json
import sys
import os
from datetime import date, datetime, timedelta
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError

OURA_API = "https://api.ouraring.com/v2/usercollection"


# ─── Авторизация ────────────────────────────────────────────────────────────

def get_token():
    token = os.environ.get("OURA_TOKEN")
    if token:
        return token.strip()
    config_path = os.path.join(os.path.dirname(__file__), "..", "config", "oura_token.txt")
    if os.path.exists(config_path):
        with open(config_path) as f:
            t = f.read().strip()
        if t and not t.startswith("#"):
            return t
    print("ERROR: токен Oura не найден.")
    print("  config/oura_token.txt  или  export OURA_TOKEN=...")
    print("  Токен: https://cloud.ouraring.com/personal-access-tokens")
    sys.exit(1)


def api_get(endpoint, start, end, token):
    url = f"{OURA_API}/{endpoint}?start_date={start}&end_date={end}"
    req = Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urlopen(req, timeout=10) as r:
            return json.loads(r.read()).get("data", [])
    except HTTPError as e:
        if e.code == 401:
            print("ERROR: неверный токен (401)")
            sys.exit(1)
        if e.code == 404:
            return []   # endpoint недоступен для этого аккаунта
        print(f"ERROR: API {endpoint} → {e.code}")
        return []
    except URLError as e:
        print(f"ERROR: нет доступа к Oura API — {e.reason}")
        sys.exit(1)


# ─── Вспомогательные функции ─────────────────────────────────────────────────

def fmt_local(iso_str):
    """ISO 8601 → HH:MM в местном времени"""
    if not iso_str:
        return "?"
    try:
        return datetime.fromisoformat(iso_str).astimezone().strftime("%H:%M")
    except Exception:
        return iso_str[11:16]


def score_quality(score, thresholds=(85, 70)):
    if score is None:
        return "unknown"
    return "good" if score >= thresholds[0] else "ok" if score >= thresholds[1] else "poor"


def sec_to_hm(seconds):
    """3661 → '1ч 01м'"""
    if not seconds:
        return "0м"
    h, m = divmod(int(seconds) // 60, 60)
    return f"{h}ч {m:02d}м" if h else f"{m}м"


def stress_label(summary):
    labels = {
        "restored":           "восстановление",
        "normal":             "норма",
        "stressful":          "стрессовый",
        "exhausted_recovery": "истощение→восстановление",
        "sleep_recovery":     "восстановление через сон",
    }
    return labels.get(summary, summary or "нет данных")


def resilience_label(level):
    labels = {
        "inadequate":  "недостаточная",
        "adequate":    "достаточная",
        "solid":       "хорошая",
        "strong":      "сильная",
        "exceptional": "отличная",
    }
    return labels.get(level, level or "нет данных")


# ─── Основная логика ──────────────────────────────────────────────────────────

def main():
    today = sys.argv[1] if len(sys.argv) > 1 else date.today().isoformat()
    yesterday = (datetime.strptime(today, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
    token = get_token()

    # --- Сон (детальные сессии) ---
    sleep_sessions_raw = api_get("sleep", yesterday, today, token)
    sleep_sessions = [s for s in sleep_sessions_raw if s.get("day") == today]

    # --- Все дневные сводки (сегодня) ---
    daily_sleep   = next(iter(api_get("daily_sleep",            today, today, token)), {})
    readiness     = next(iter(api_get("daily_readiness",        today, today, token)), {})
    stress        = next(iter(api_get("daily_stress",           today, today, token)), {})
    resilience    = next(iter(api_get("daily_resilience",       today, today, token)), {})
    cardio_age    = next(iter(api_get("daily_cardiovascular_age", today, today, token)), {})
    spo2          = next(iter(api_get("daily_spo2",             today, today, token)), {})

    # Активность — за вчера (сегодняшний день ещё не закончен)
    activity_list = api_get("daily_activity", yesterday, yesterday, token)
    activity      = next(iter(activity_list), {})

    # ─── Парсим сон ───────────────────────────────────────────────────────────
    sleep_score   = daily_sleep.get("score")
    total_sec     = 0
    bed_time = wake_time = "?"
    deep_min = rem_min = light_min = 0
    avg_hrv = avg_hr = lowest_hr = None

    if sleep_sessions:
        main_s      = max(sleep_sessions, key=lambda s: s.get("total_sleep_duration", 0))
        total_sec   = sum(s.get("total_sleep_duration", 0) for s in sleep_sessions)
        bed_time    = fmt_local(main_s.get("bedtime_start"))
        wake_time   = fmt_local(main_s.get("bedtime_end"))
        deep_min    = round(sum(s.get("deep_sleep_duration",  0) for s in sleep_sessions) / 60)
        rem_min     = round(sum(s.get("rem_sleep_duration",   0) for s in sleep_sessions) / 60)
        light_min   = round(sum(s.get("light_sleep_duration", 0) for s in sleep_sessions) / 60)
        avg_hrv     = main_s.get("average_hrv")
        avg_hr      = main_s.get("average_heart_rate")
        lowest_hr   = main_s.get("lowest_heart_rate")

    total_hours   = round(total_sec / 3600, 1) if total_sec else "?"
    spo2_avg      = spo2.get("spo2_percentage", {}).get("average")
    breathing_di  = spo2.get("breathing_disturbance_index")

    # ─── Парсим стресс ────────────────────────────────────────────────────────
    stress_high_sec    = stress.get("stress_high", 0) or 0
    recovery_high_sec  = stress.get("recovery_high", 0) or 0
    day_summary        = stress.get("day_summary")

    # Вчерашний стресс для контекста
    stress_yesterday   = next(iter(api_get("daily_stress", yesterday, yesterday, token)), {})

    # ─── Парсим активность (вчера) ───────────────────────────────────────────
    steps        = activity.get("steps")
    act_calories = activity.get("active_calories")
    tot_calories = activity.get("total_calories")
    walk_km      = round(activity.get("equivalent_walking_distance", 0) / 1000, 1) if activity.get("equivalent_walking_distance") else None
    act_score    = activity.get("score")
    sedentary_h  = round(activity.get("sedentary_time", 0) / 3600, 1) if activity.get("sedentary_time") else None

    # ─── Вывод ───────────────────────────────────────────────────────────────
    print(f"# Oura Daily — {today}")
    print()

    # СОН
    print("sleep:")
    print(f"  hours: {total_hours}")
    print(f'  quality: "{score_quality(sleep_score)}"  # score: {sleep_score or "n/a"}/100')
    print(f'  bed_time: "{bed_time}"')
    print(f'  wake_time: "{wake_time}"')
    if deep_min or rem_min or light_min:
        print(f"  stages_min:")
        print(f"    deep: {deep_min}    # норма: 60-90 мин")
        print(f"    rem: {rem_min}     # норма: 90-120 мин")
        print(f"    light: {light_min}")
    if avg_hrv or avg_hr:
        print(f"  biometrics:")
        if avg_hrv:    print(f"    hrv_avg: {avg_hrv}       # выше = лучше восстановление")
        if avg_hr:     print(f"    hr_avg: {round(avg_hr, 1)}")
        if lowest_hr:  print(f"    hr_lowest: {lowest_hr}   # чем ниже, тем лучше форма")
        if spo2_avg:   print(f"    spo2: {round(spo2_avg, 1)}%    # норма >95%")
        if breathing_di is not None:
            print(f"    breathing_disturbance: {breathing_di}  # норма: 0")
    print()

    # ГОТОВНОСТЬ
    r_score = readiness.get("score")
    r_contrib = readiness.get("contributors", {})
    temp_dev = readiness.get("temperature_deviation")
    print("readiness:")
    print(f"  score: {r_score or 'n/a'}/100  # {score_quality(r_score)}")
    if r_contrib:
        print(f"  contributors:")
        for k, v in sorted(r_contrib.items(), key=lambda x: x[1]):
            bar = "░" * (v // 10)
            print(f"    {k}: {v}  {bar}")
    if temp_dev is not None:
        flag = "⚠️ повышена" if temp_dev > 0.5 else ("⚠️ снижена" if temp_dev < -0.5 else "норма")
        print(f"  temperature_deviation: {temp_dev:+.2f}°C  # {flag}")
    print()

    # СТРЕСС И RESILIENCE
    res_level = resilience.get("level")
    res_contrib = resilience.get("contributors", {})
    print("stress_recovery:")
    if day_summary:
        print(f"  today_summary: \"{stress_label(day_summary)}\"")
    elif stress_yesterday.get("day_summary"):
        print(f"  yesterday_summary: \"{stress_label(stress_yesterday.get('day_summary'))}\"  # сегодня ещё нет данных")
    if stress_high_sec or recovery_high_sec:
        print(f"  stress_high: \"{sec_to_hm(stress_high_sec)}\"")
        print(f"  recovery_high: \"{sec_to_hm(recovery_high_sec)}\"")
    print(f"  resilience: \"{resilience_label(res_level)}\"")
    if res_contrib:
        print(f"  resilience_contributors:")
        print(f"    sleep_recovery: {res_contrib.get('sleep_recovery', 'n/a')}")
        print(f"    daytime_recovery: {res_contrib.get('daytime_recovery', 'n/a')}")
        print(f"    stress_load: {res_contrib.get('stress', 'n/a')}")
    print()

    # АКТИВНОСТЬ (вчера)
    if activity:
        print(f"activity_yesterday:  # {yesterday}")
        print(f"  score: {act_score or 'n/a'}/100")
        if steps:       print(f"  steps: {steps}")
        if walk_km:     print(f"  walk_km: {walk_km}")
        if act_calories: print(f"  active_calories: {act_calories}")
        if tot_calories: print(f"  total_calories: {tot_calories}")
        if sedentary_h:  print(f"  sedentary_h: {sedentary_h}   # сидела {sedentary_h}ч")
        print()

    # СОСУДИСТЫЙ ВОЗРАСТ
    vasc_age = cardio_age.get("vascular_age")
    if vasc_age:
        print(f"cardiovascular:")
        print(f"  vascular_age: {vasc_age}  # биологический возраст сосудов")
        print()

    # ─── ПРЕДУПРЕЖДЕНИЯ ───────────────────────────────────────────────────────
    warnings = []
    if isinstance(total_hours, float) and total_hours < 6:
        warnings.append("⚠️  Сон <6ч → MAINTENANCE калории (2052 ккал), не дефицит!")
    if isinstance(total_hours, float) and 6 <= total_hours < 7:
        warnings.append("⚠️  Сон <7ч → Zone 2 по самочувствию, следи за энергией")
    if r_score and r_score < 70:
        warnings.append(f"⚠️  Readiness {r_score} < 70 → восстановление неполное, снизь интенсивность")
    if temp_dev and temp_dev > 0.5:
        warnings.append(f"⚠️  Температура +{temp_dev:.1f}°C → возможна болезнь, отдых приоритет")
    if spo2_avg and spo2_avg < 95:
        warnings.append(f"⚠️  SpO2 {spo2_avg:.1f}% < 95% → проверь дыхание/апноэ")
    if breathing_di and breathing_di > 5:
        warnings.append(f"⚠️  Нарушения дыхания ночью ({breathing_di}) → поговори с врачом")

    if warnings:
        for w in warnings:
            print(f"# {w}")
    else:
        print("# ✓  Всё в норме")


if __name__ == "__main__":
    main()
