"""
Tool definitions for OpenAI function-calling API and their implementations.
Recipes and daily logs are read/written via Supabase (db.py).
Lab files (PDFs, images) are still read from local filesystem.
"""
from pathlib import Path
from datetime import date, datetime
import base64
import yaml

import db

BASE = Path(__file__).parent.parent

LAB_DIRS = {
    "blood_tests": BASE / "blood tests",
    "glucose":     BASE / "glucose level",
    "other":       BASE / "other tests",
}
LAB_EXTENSIONS = {".pdf", ".jpeg", ".jpg", ".png"}


def _get_targets() -> dict:
    try:
        profile_path = BASE / "data/tactical/user_profile.yaml"
        with open(profile_path, encoding="utf-8") as f:
            profile = yaml.safe_load(f) or {}
        calc = profile.get("calculated", {})
        return {
            "calories": calc.get("target_calories", 1650),
            "protein": calc.get("protein_g", 143),
        }
    except Exception:
        return {"calories": 1650, "protein": 143}


# ─────────────────────── Tool Schemas ───────────────────────────

def _fn(name: str, description: str, properties: dict, required: list) -> dict:
    """Helper to build OpenAI function-calling tool schema."""
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        },
    }


TOOLS = [
    _fn(
        "log_food",
        "Записать приём пищи в сегодняшний лог. Вызывай когда пользователь описывает что съел/выпил.",
        {
            "description": {"type": "string", "description": "Описание блюда/напитка"},
            "calories": {"type": "integer", "description": "Примерные калории (ккал)"},
            "protein": {"type": "number", "description": "Белок в граммах"},
            "time": {"type": "string", "description": "Время приёма пищи HH:MM. Если не указано — текущее."},
        },
        ["description", "calories", "protein"],
    ),
    _fn(
        "log_workout",
        "Записать тренировку в сегодняшний лог.",
        {
            "type": {"type": "string", "enum": ["strength", "zone2", "pilates", "flexibility", "other"], "description": "Тип тренировки"},
            "name": {"type": "string", "description": "Название тренировки"},
            "duration_min": {"type": "integer", "description": "Длительность в минутах"},
            "rpe": {"type": "integer", "description": "RPE от 1 до 10"},
            "notes": {"type": "string", "description": "Дополнительные заметки"},
        },
        ["type", "name", "duration_min"],
    ),
    _fn(
        "log_sleep",
        "Записать данные о сне.",
        {
            "hours": {"type": "number", "description": "Часов сна"},
            "quality": {"type": "string", "enum": ["good", "ok", "poor"], "description": "Качество сна"},
            "bed_time": {"type": "string", "description": "Время отхода ко сну HH:MM"},
            "wake_time": {"type": "string", "description": "Время пробуждения HH:MM"},
        },
        ["hours"],
    ),
    _fn(
        "log_weight",
        "Записать утренний вес (натощак).",
        {"weight_kg": {"type": "number", "description": "Вес в кг"}},
        ["weight_kg"],
    ),
    _fn(
        "get_today_summary",
        "Получить сводку дня: калории, белок, остаток бюджета, тренировки, сон.",
        {},
        [],
    ),
    _fn(
        "list_lab_files",
        "Показать список всех файлов с результатами анализов из папок Health OS. "
        "Вызывай когда пользователь хочет посмотреть доступные анализы или выбрать файл для чтения.",
        {},
        [],
    ),
    _fn(
        "read_lab_file",
        "Прочитать конкретный файл с анализами (PDF или изображение). "
        "Вызывай после list_lab_files когда пользователь выбрал файл. "
        "Для изображений и сканов PDF — передаёт визуальное содержимое на анализ.",
        {
            "filename": {"type": "string", "description": "Имя файла, например 'Биохимический анализ крови.pdf'"},
            "category": {
                "type": "string",
                "enum": ["blood_tests", "glucose", "other"],
                "description": "Папка: blood_tests, glucose или other",
            },
        },
        ["filename", "category"],
    ),
    _fn(
        "lookup_recipe",
        (
            "Найти сохранённый рецепт по названию или описанию блюда. "
            "ВСЕГДА вызывай перед log_food, когда пользователь описывает еду текстом или присылает фото. "
            "Если рецепт найден — используй сохранённые калории, белок, жиры, углеводы и ГИ по умолчанию "
            "(не пересчитывай, если пользователь не указал другую граммовку). "
            "Если не найден — оцени сам и затем сохрани через save_recipe."
        ),
        {
            "query": {
                "type": "string",
                "description": "Название или описание блюда, например 'овсянка с бананом' или 'Greek yogurt with berries'",
            }
        },
        ["query"],
    ),
    _fn(
        "update_recipe",
        (
            "Обновить существующий рецепт в базе. Вызывай когда пользователь говорит что данные "
            "неправильные или просит исправить рецепт. Обновляет только переданные поля, "
            "остальные оставляет без изменений."
        ),
        {
            "name": {"type": "string", "description": "Название рецепта для поиска (должен совпасть с существующим)"},
            "serving_g": {"type": "number", "description": "Новая стандартная порция в граммах"},
            "calories": {"type": "integer", "description": "Новые калории"},
            "protein": {"type": "number", "description": "Новый белок в граммах"},
            "fat": {"type": "number", "description": "Новые жиры в граммах"},
            "carbs": {"type": "number", "description": "Новые углеводы в граммах"},
            "glycemic_index": {"type": "integer", "description": "Новый гликемический индекс"},
            "ingredients": {"type": "string", "description": "Новый состав"},
            "aliases": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Новые алиасы (полностью заменяют старые)",
            },
        },
        ["name"],
    ),
    _fn(
        "save_recipe",
        (
            "Сохранить новый рецепт в базу. Вызывай после log_food, "
            "если lookup_recipe не нашёл этот рецепт. "
            "Не сохраняй если рецепт уже есть (lookup вернул результат)."
        ),
        {
            "name": {"type": "string", "description": "Название блюда (кратко, по-русски)"},
            "aliases": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Альтернативные названия или способы упоминания этого блюда",
            },
            "serving_g": {"type": "number", "description": "Стандартная порция в граммах"},
            "calories": {"type": "integer", "description": "Калории на стандартную порцию"},
            "protein": {"type": "number", "description": "Белок в граммах на стандартную порцию"},
            "fat": {"type": "number", "description": "Жиры в граммах на стандартную порцию"},
            "carbs": {"type": "number", "description": "Углеводы в граммах на стандартную порцию"},
            "glycemic_index": {
                "type": "integer",
                "description": "Гликемический индекс (0-100). null если не применимо (мясо, рыба, яйца).",
            },
            "ingredients": {
                "type": "string",
                "description": "Состав порции кратко, например: '80г овсянки, 120г банана, 150мл молока'",
            },
        },
        ["name", "serving_g", "calories", "protein"],
    ),
    _fn(
        "edit_food_log",
        (
            "Исправить запись о приёме пищи в сегодняшнем логе. "
            "Вызывай когда пользователь хочет поменять калории, белок, описание или другие данные "
            "уже залогированного блюда. По умолчанию исправляет последнюю запись (meal_index=-1). "
            "Обновляет только переданные поля, остальные оставляет без изменений."
        ),
        {
            "meal_index": {
                "type": "integer",
                "description": "Индекс приёма пищи: -1 = последний (по умолчанию), 0 = первый, 1 = второй и т.д.",
            },
            "description": {"type": "string", "description": "Новое описание блюда"},
            "calories": {"type": "integer", "description": "Новые калории (ккал)"},
            "protein": {"type": "number", "description": "Новый белок (г)"},
            "fat": {"type": "number", "description": "Новые жиры (г)"},
            "carbs": {"type": "number", "description": "Новые углеводы (г)"},
        },
        [],
    ),
    _fn(
        "sync_oura",
        (
            "Загрузить данные из Oura Ring: сон (часы, HRV, ЧСС, фазы), "
            "готовность (readiness score), активность (шаги, Zone 2 минуты, калории), "
            "тренировки, стресс, SpO2. "
            "Вызывай при вопросах о сне, восстановлении, активности или когда нужно "
            "синхронизировать данные с кольцом."
        ),
        {
            "date": {
                "type": "string",
                "description": "Дата YYYY-MM-DD. По умолчанию — сегодня.",
            },
            "write_to_log": {
                "type": "boolean",
                "description": "Записать сон автоматически в дневной лог. По умолчанию true.",
            },
        },
        [],
    ),
]


# ─────────────────────── Tool Handlers ──────────────────────────

def handle_tool(name: str, args: dict) -> dict:
    if name == "log_food":
        log = db.get_log()
        if "meals" not in log:
            log["meals"] = []

        meal = {
            "time": args.get("time") or datetime.now().strftime("%H:%M"),
            "description": args["description"],
            "calories": args["calories"],
            "protein": args["protein"],
        }
        log["meals"].append(meal)
        db.upsert_log(log["date"], log)

        targets = _get_targets()
        total_cal = sum(m.get("calories", 0) for m in log["meals"])
        total_prot = sum(m.get("protein", 0) for m in log["meals"])
        return {
            "logged": meal,
            "day_total": {"calories": total_cal, "protein": round(total_prot, 1)},
            "remaining": {
                "calories": targets["calories"] - total_cal,
                "protein": round(targets["protein"] - total_prot, 1),
            },
            "targets": targets,
        }

    if name == "log_workout":
        log = db.get_log()
        if "training" not in log:
            log["training"] = []

        workout = {
            "type": args["type"],
            "name": args["name"],
            "duration_min": args["duration_min"],
        }
        if "rpe" in args:
            workout["rpe"] = args["rpe"]
        if "notes" in args:
            workout["notes"] = args["notes"]

        log["training"].append(workout)
        db.upsert_log(log["date"], log)
        return {"logged": workout, "success": True}

    if name == "log_sleep":
        log = db.get_log()
        log["sleep"] = {
            "hours": args["hours"],
            "quality": args.get("quality", "ok"),
        }
        if "bed_time" in args:
            log["sleep"]["bed_time"] = args["bed_time"]
        if "wake_time" in args:
            log["sleep"]["wake_time"] = args["wake_time"]
        db.upsert_log(log["date"], log)
        return {"logged": log["sleep"], "success": True}

    if name == "log_weight":
        log = db.get_log()
        log["weight_morning"] = args["weight_kg"]
        db.upsert_log(log["date"], log)
        return {"logged": args["weight_kg"], "success": True}

    if name == "get_today_summary":
        log = db.get_log()
        targets = _get_targets()
        meals = log.get("meals", [])
        total_cal = sum(m.get("calories", 0) for m in meals)
        total_prot = sum(m.get("protein", 0) for m in meals)
        return {
            "date": date.today().isoformat(),
            "weight_morning": log.get("weight_morning"),
            "calories": {
                "consumed": total_cal,
                "target": targets["calories"],
                "remaining": targets["calories"] - total_cal,
            },
            "protein": {
                "consumed": round(total_prot, 1),
                "target": targets["protein"],
                "remaining": round(targets["protein"] - total_prot, 1),
            },
            "meals": meals,
            "training": log.get("training", []),
            "sleep": log.get("sleep"),
        }

    if name == "list_lab_files":
        result = {}
        for cat, path in LAB_DIRS.items():
            if path.exists():
                files = sorted(
                    f.name for f in path.iterdir()
                    if f.suffix.lower() in LAB_EXTENSIONS
                )
                if files:
                    result[cat] = files
        return result

    if name == "read_lab_file":
        category = args.get("category", "blood_tests")
        filename = args["filename"]
        path = LAB_DIRS.get(category, LAB_DIRS["blood_tests"]) / filename

        if not path.exists():
            for d in LAB_DIRS.values():
                candidate = d / filename
                if candidate.exists():
                    path = candidate
                    break
            else:
                return {"error": f"Файл не найден: {filename}"}

        suffix = path.suffix.lower()

        if suffix in (".jpeg", ".jpg", ".png"):
            data = path.read_bytes()
            b64 = base64.standard_b64encode(data).decode()
            return {"type": "image", "data": b64, "filename": filename}

        if suffix == ".pdf":
            try:
                import pypdf
                reader = pypdf.PdfReader(str(path))
                text = "\n".join(
                    (page.extract_text() or "") for page in reader.pages
                )
                if len(text.strip()) > 150:
                    return {"type": "text", "content": text, "filename": filename}
            except Exception:
                pass

            try:
                import fitz
                doc = fitz.open(str(path))
                images = []
                for i, page in enumerate(doc):
                    if i >= 5:
                        break
                    pix = page.get_pixmap(dpi=150)
                    b64 = base64.standard_b64encode(pix.tobytes("jpeg")).decode()
                    images.append(b64)
                doc.close()
                return {"type": "images", "data": images, "filename": filename}
            except Exception as e:
                return {"error": f"Не удалось прочитать PDF: {e}"}

        return {"error": f"Неподдерживаемый формат: {suffix}"}

    if name == "edit_food_log":
        log = db.get_log()
        meals = log.get("meals", [])
        if not meals:
            return {"updated": False, "reason": "Нет залогированных приёмов пищи сегодня"}

        idx = args.get("meal_index", -1)
        try:
            meal = meals[idx]
        except IndexError:
            return {"updated": False, "reason": f"Приём пищи с индексом {idx} не найден (всего {len(meals)})"}

        old = dict(meal)
        for field in ("description", "calories", "protein", "fat", "carbs"):
            if field in args and args[field] is not None:
                meal[field] = args[field]

        db.upsert_log(log["date"], log)

        targets = _get_targets()
        total_cal = sum(m.get("calories", 0) for m in meals)
        total_prot = sum(m.get("protein", 0) for m in meals)
        return {
            "updated": True,
            "meal_index": idx if idx >= 0 else len(meals) + idx,
            "before": old,
            "after": meal,
            "day_total": {"calories": total_cal, "protein": round(total_prot, 1)},
            "remaining": {
                "calories": targets["calories"] - total_cal,
                "protein": round(targets["protein"] - total_prot, 1),
            },
        }

    if name == "update_recipe":
        target_name = args.get("name", "").lower().strip()
        recipe = db.lookup_recipe(target_name)
        if not recipe:
            return {"updated": False, "reason": "recipe not found", "query": args["name"]}

        updatable = ["serving_g", "calories", "protein", "fat", "carbs",
                     "glycemic_index", "ingredients", "aliases"]
        changes = {}
        for field in updatable:
            if field in args and args[field] is not None:
                changes[field] = args[field]
        changes["estimated"] = False
        changes["updated"] = date.today().isoformat()

        db.update_recipe_by_id(recipe["id"], changes)
        return {"updated": True, "name": recipe["name"], "changed_fields": changes}

    if name == "lookup_recipe":
        query = args.get("query", "")
        recipe = db.lookup_recipe(query)
        if recipe:
            return {"found": True, "recipe": recipe}
        total = len(db.get_all_recipes())
        return {"found": False, "query": query, "total_recipes": total}

    if name == "save_recipe":
        # Dedup check
        existing = db.lookup_recipe(args.get("name", ""))
        if existing and existing.get("name", "").lower() == args.get("name", "").lower():
            return {"saved": False, "reason": "already_exists", "name": args["name"]}

        entry = {
            "name": args["name"],
            "aliases": args.get("aliases", []),
            "serving_g": args.get("serving_g"),
            "calories": args.get("calories"),
            "protein": args.get("protein"),
            "fat": args.get("fat"),
            "carbs": args.get("carbs"),
            "glycemic_index": args.get("glycemic_index"),
            "ingredients": args.get("ingredients", ""),
            "added": date.today().isoformat(),
        }
        db.insert_recipe(entry)
        total = len(db.get_all_recipes())
        return {"saved": True, "name": args["name"], "total_recipes": total}

    return {"error": f"Unknown tool: {name}"}
