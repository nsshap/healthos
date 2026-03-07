"""
Loads Health OS data and builds system prompts for each role.
Recipes and daily logs come from Supabase (db.py).
Static files (profile, directives, strategy, program, biomarkers, goals) are read from disk.
"""
from pathlib import Path
from datetime import date
import yaml

import db

BASE = Path(__file__).parent.parent  # Health OS root


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8") if path.exists() else ""
    except Exception:
        return ""


def _yaml(path: Path) -> dict:
    try:
        if not path.exists():
            return {}
        with open(path, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


def _dump(data) -> str:
    try:
        return yaml.dump(data, allow_unicode=True, default_flow_style=False)
    except Exception:
        return str(data)


def _week_logs_text() -> str:
    parts = []
    for log in db.get_week_logs():
        d = log.get("date", "")
        parts.append(f"### {d}\n```yaml\n{_dump(log)}```")
    return "\n".join(parts) if parts else "Логов за прошлую неделю нет."


def build_system_prompt(role: str = "coach") -> str:
    # ── Static files (change rarely, managed via Claude Code) ──
    profile = _dump(_yaml(BASE / "data/tactical/user_profile.yaml"))
    directives = _dump(_yaml(BASE / "data/strategic/directives.yaml"))
    strategy = _read(BASE / "data/tactical/strategy.md")
    program = _dump(_yaml(BASE / "data/tactical/training/program.yaml"))
    biomarkers = _dump(_yaml(BASE / "data/strategic/biomarkers.yaml"))
    goals = _read(BASE / "data/strategic/goals.md")

    # ── Dynamic data from Supabase ──
    today = date.today().isoformat()
    today_log = _dump(db.get_log(today))
    recipes = _dump({"recipes": db.get_all_recipes()})

    base = f"""# Health OS — Active Session
Today: {today}

## User Profile
```yaml
{profile}
```

## Active Directives
```yaml
{directives}
```

## Current Strategy
{strategy}

## Training Program
```yaml
{program}
```

## Today's Log ({today})
```yaml
{today_log}
```

## Saved Recipes
```yaml
{recipes}
```
"""

    if role == "strategist":
        base += f"""
## Week's Logs
{_week_logs_text()}

## Biomarkers
```yaml
{biomarkers}
```
"""

    if role in ("cmo", "analyst"):
        base += f"""
## Biomarkers
```yaml
{biomarkers}
```

## Long-term Goals
{goals}
"""

    role_instructions = {
        "coach": (
            "Ты сейчас в роли **Coach**. Помогаешь с ежедневным питанием и тренировками. "
            "Коротко, по делу, без лекций. Используй инструменты для записи в лог.\n\n"
            "**Логирование еды — обязательный порядок:**\n"
            "1. Сначала вызови lookup_recipe с названием/описанием блюда.\n"
            "2. Если рецепт найден — используй сохранённые калории, белок, жиры, углеводы и ГИ без пересчёта. "
            "Скорректируй пропорционально только если пользователь явно указал другую граммовку.\n"
            "3. Если не найден — оцени КБЖУ самостоятельно, залогируй через log_food, "
            "затем сохрани через save_recipe чтобы в следующий раз данные уже были.\n"
            "4. При сохранении рецепта добавляй гликемический индекс (GI) — это важно для стратегии.\n\n"
            "**Редактирование уже залогированной еды:**\n"
            "Если пользователь говорит что данные неправильные после логирования (фото или текст) — "
            "вызывай edit_food_log. По умолчанию исправляет последнюю запись (meal_index=-1).\n"
            "Примеры триггеров: 'калорий было 350', 'это был салат, не суп', 'белка там 30г', "
            "'исправь калории', 'измени описание', 'там другое блюдо'.\n"
            "После исправления покажи: что изменилось и новый остаток бюджета.\n\n"
            "**Когда показываешь данные Oura — всегда интерпретируй, не просто перечисляй цифры:**\n"
            "- Readiness score: скажи что это значит для тренировки сегодня (≥85 = полный объём, "
            "70-84 = умеренно, 60-69 = 50% объёма, <60 = только прогулка)\n"
            "- HRV balance: если ниже обычного — тело под нагрузкой, тренировку стоит смягчить\n"
            "- Deep sleep < 60 мин — предупреди, что восстановление неполное\n"
            "- REM < 90 мин — сигнал хронического недосыпа или стресса\n"
            "- Efficiency < 75% — много времени в кровати без сна, обсуди гигиену сна\n"
            "- Zone 2 за день: сравни с целью 90 мин/неделю из директив\n"
            "- Resting HR выше обычного на 5+ уд/мин — сигнал перегрузки или болезни\n"
            "- SpO2 < 95% или breathing disturbance > 0 — стоит проверить апноэ\n"
            "Всегда заканчивай конкретной рекомендацией на день: что делать с тренировкой, "
            "сколько спать, что поесть с учётом реального восстановления."
        ),
        "strategist": (
            "Ты сейчас в роли **Strategist**. Анализируешь прошедшую неделю, compliance, "
            "обновляешь стратегию. Считай метрики, давай конкретные рекомендации."
        ),
        "cmo": (
            "Ты сейчас в роли **CMO**. Оцениваешь риски на горизонте 10-30 лет по методологии "
            "Peter Attia (Four Horsemen). Используй данные анализов."
        ),
        "analyst": (
            "Ты сейчас в роли **Analyst**. Работаешь с медицинскими данными. "
            "Используй оптимальные значения Attia, не лабораторную «норму»."
        ),
        "behaviorist": (
            "Ты сейчас в роли **Behaviorist**. ZERO JUDGMENT. "
            "Никогда не осуждаешь. Поддержка при срывах, тяге, тревоге."
        ),
    }

    base += f"\n---\n\n## Active Role\n{role_instructions.get(role, role_instructions['coach'])}\n"
    return base
