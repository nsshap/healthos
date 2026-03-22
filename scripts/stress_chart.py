#!/usr/bin/env python3
"""
Stress Analysis Chart — Health OS
Вытаскивает данные стресса за последние 14 дней из Supabase
и строит график + анализ.
"""
import sys
import os
import json
from pathlib import Path
from datetime import date, timedelta
from dotenv import load_dotenv

BASE = Path(__file__).parent.parent
load_dotenv(BASE / ".env")

sys.path.insert(0, str(BASE / "bot"))
import db


def fetch_oura_range(days: int = 14) -> list[dict]:
    start = (date.today() - timedelta(days=days - 1)).isoformat()
    end = date.today().isoformat()
    r = db.get_client().table("oura_data") \
        .select("date, data") \
        .gte("date", start) \
        .lte("date", end) \
        .order("date") \
        .execute()
    return r.data or []


def extract_stress(rows: list[dict]) -> list[dict]:
    result = []
    for row in rows:
        d = row["date"]
        data = row.get("data") or {}
        sr = data.get("stress_recovery") or {}
        sleep = data.get("sleep") or {}
        readiness = data.get("readiness") or {}
        bio = sleep.get("biometrics") or {}

        result.append({
            "date": d,
            "stress_summary": sr.get("summary"),
            "stress_high_min": sr.get("stress_high_min"),
            "recovery_high_min": sr.get("recovery_high_min"),
            "resilience": sr.get("resilience"),
            "resilience_sleep": (sr.get("resilience_contributors") or {}).get("sleep_recovery"),
            "resilience_daytime": (sr.get("resilience_contributors") or {}).get("daytime_recovery"),
            "resilience_stress_load": (sr.get("resilience_contributors") or {}).get("stress_load"),
            "readiness_score": readiness.get("score"),
            "sleep_hours": sleep.get("hours"),
            "hrv": bio.get("hrv_avg"),
            "resting_hr": bio.get("hr_lowest"),
        })
    return result


STRESS_ORDER = {
    "restored": 1,
    "normal": 2,
    "stressful": 3,
    "very_stressful": 4,
}

RESILIENCE_ORDER = {
    "exceptional": 5,
    "strong": 4,
    "solid": 3,
    "adequate": 2,
    "inadequate": 1,
}


def print_table(records: list[dict]):
    print("\n" + "=" * 90)
    print("ДАННЫЕ OURA — СТРЕСС И ВОССТАНОВЛЕНИЕ (14 дней)")
    print("=" * 90)
    header = f"{'Дата':<12} {'Стресс':<16} {'Стресс мин':>10} {'Восст мин':>10} {'Readiness':>10} {'HRV':>6} {'Сон':>5} {'Resilience':<14}"
    print(header)
    print("-" * 90)
    for r in records:
        stress = r["stress_summary"] or "—"
        sh = str(r["stress_high_min"]) if r["stress_high_min"] is not None else "—"
        rh = str(r["recovery_high_min"]) if r["recovery_high_min"] is not None else "—"
        read = str(r["readiness_score"]) if r["readiness_score"] is not None else "—"
        hrv = str(r["hrv"]) if r["hrv"] is not None else "—"
        slp = str(r["sleep_hours"]) if r["sleep_hours"] is not None else "—"
        res = r["resilience"] or "—"
        print(f"{r['date']:<12} {stress:<16} {sh:>10} {rh:>10} {read:>10} {hrv:>6} {slp:>5} {res:<14}")
    print("=" * 90)


def analyze(records: list[dict]) -> str:
    filled = [r for r in records if r["stress_summary"]]
    if not filled:
        return "Данных стресса нет."

    # Стресс-уровни
    stress_counts = {}
    for r in filled:
        s = r["stress_summary"]
        stress_counts[s] = stress_counts.get(s, 0) + 1

    last3 = filled[-3:]
    recent_stress = [r["stress_summary"] for r in last3]
    recent_hrv = [r["hrv"] for r in filled[-7:] if r["hrv"]]
    all_hrv = [r["hrv"] for r in filled if r["hrv"]]
    recent_readiness = [r["readiness_score"] for r in filled[-7:] if r["readiness_score"]]

    lines = []
    lines.append("\n" + "=" * 90)
    lines.append("АНАЛИЗ: НЕРВНИЧАЕШЬ ЛИ ТЫ?")
    lines.append("=" * 90)

    # Тренд стресса
    lines.append("\n📊 РАСПРЕДЕЛЕНИЕ СТРЕССА ЗА 14 ДНЕЙ:")
    for label, count in sorted(stress_counts.items(), key=lambda x: STRESS_ORDER.get(x[0], 0)):
        bar = "█" * count
        pct = round(count / len(filled) * 100)
        lines.append(f"  {label:<18} {bar:<15} {count} дней ({pct}%)")

    # Последние 3 дня
    lines.append(f"\n🗓️  ПОСЛЕДНИЕ 3 ДНЯ: {', '.join(recent_stress)}")

    # HRV тренд
    if len(all_hrv) >= 4:
        avg_hrv_week = sum(recent_hrv) / len(recent_hrv) if recent_hrv else None
        avg_hrv_all = sum(all_hrv) / len(all_hrv)
        hrv_trend = "↓ снижается" if avg_hrv_week and avg_hrv_week < avg_hrv_all * 0.95 else \
                    "↑ растёт" if avg_hrv_week and avg_hrv_week > avg_hrv_all * 1.05 else "→ стабильно"
        hrv_week_str = f"{avg_hrv_week:.0f}" if avg_hrv_week else "—"
        lines.append(f"\n💓 HRV: среднее за 14 дней = {avg_hrv_all:.0f} мс | последние 7 дней = {hrv_week_str} мс | тренд: {hrv_trend}")

    # Readiness
    if recent_readiness:
        avg_read = sum(recent_readiness) / len(recent_readiness)
        lines.append(f"⚡ Readiness (7 дней): среднее = {avg_read:.0f}")

    # Resilience последние дни
    recent_resilience = [r["resilience"] for r in filled[-5:] if r["resilience"]]
    if recent_resilience:
        lines.append(f"🛡️  Resilience (последние дни): {', '.join(recent_resilience)}")

    # Итоговый вердикт
    high_stress_days = stress_counts.get("stressful", 0) + stress_counts.get("very_stressful", 0)
    high_stress_pct = high_stress_days / len(filled) * 100
    recent_high = sum(1 for s in recent_stress if s in ("stressful", "very_stressful"))

    lines.append("\n" + "─" * 90)
    lines.append("ВЕРДИКТ:")

    if recent_high >= 2 and high_stress_pct >= 50:
        lines.append("🔴 ДА, ты явно под стрессом прямо сейчас.")
        lines.append("   Последние дни — стрессовые, и это устойчивая тенденция за 2 недели.")
        lines.append("   Рекомендация: снизить нагрузку, приоритет — сон и зона 2.")
    elif recent_high >= 2:
        lines.append("🟠 УМЕРЕННЫЙ СТРЕСС — последние дни напряжённые.")
        lines.append("   Общая картина за 2 недели лучше, но сейчас идёт волна стресса.")
        lines.append("   Следи за HRV и сном. Если тренируешься — снизь интенсивность.")
    elif recent_high == 1:
        lines.append("🟡 НЕБОЛЬШОЕ НАПРЯЖЕНИЕ — один стрессовый день в последних трёх.")
        lines.append("   В целом норма. Следи, не нарастает ли.")
    else:
        lines.append("🟢 НЕТ, ты в порядке — последние дни восстановительные или нормальные.")
        lines.append("   Продолжай в том же духе.")

    lines.append("=" * 90)
    return "\n".join(lines)


def ascii_chart(records: list[dict]):
    filled = [r for r in records if r["stress_summary"]]
    if not filled:
        return

    levels = {
        "restored":      0,
        "normal":        1,
        "stressful":     2,
        "very_stressful":3,
    }
    icons = {0: "🟢", 1: "🟡", 2: "🟠", 3: "🔴"}
    labels = {0: "restored", 1: "normal", 2: "stressful", 3: "very_stressful"}

    print("\n📈 ГРАФИК СТРЕССА (14 дней):")
    print()

    # Рисуем сверху вниз (4 уровня)
    for lvl in [3, 2, 1, 0]:
        row = f" {icons[lvl]} {labels[lvl]:<16} │"
        for r in filled:
            s = r["stress_summary"]
            cur_lvl = levels.get(s, -1)
            if cur_lvl == lvl:
                row += " ██ "
            elif cur_lvl > lvl:
                row += " ▓▓ "
            else:
                row += "    "
        print(row)

    # Ось X с датами
    dates_row = " " * 22 + "│"
    for r in filled:
        dates_row += r["date"][5:] + " "  # MM-DD
    print(dates_row)

    # HRV строка
    if any(r["hrv"] for r in filled):
        hrv_row = f" 💓 HRV                 │"
        for r in filled:
            hrv = r["hrv"]
            if hrv:
                hrv_row += f"{hrv:>3}  "
            else:
                hrv_row += "  —  "
        print(hrv_row)

    # Readiness строка
    if any(r["readiness_score"] for r in filled):
        read_row = f" ⚡ Readiness           │"
        for r in filled:
            rs = r["readiness_score"]
            if rs:
                read_row += f"{rs:>3}  "
            else:
                read_row += "  —  "
        print(read_row)

    print()


def main():
    print("Загружаю данные из Supabase...")
    rows = fetch_oura_range(14)
    print(f"Получено записей: {len(rows)}")

    if not rows:
        print("Данных нет. Проверь подключение к Supabase.")
        return

    records = extract_stress(rows)
    print_table(records)
    ascii_chart(records)
    print(analyze(records))


if __name__ == "__main__":
    main()
