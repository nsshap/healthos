"""
Health OS Telegram Bot
─────────────────────
A personal health assistant bot backed by the Health OS system.
Each message is handled by Claude with full Health OS context.

Commands:
  /start    — welcome & help
  /daily    — morning check-in (Coach)
  /status   — today's summary
  /review   — weekly strategy review (Strategist)
  /strategy — strategic CMO review
  /labs     — upload lab results (Analyst)
  /crisis   — crisis support (Behaviorist)
  /role     — switch active role
  /clear    — reset conversation history
"""

import asyncio
import base64
import logging
import os
from collections import defaultdict
from datetime import time as dt_time
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from openai import AsyncOpenAI, RateLimitError
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)
import json

load_dotenv()

from context import build_system_prompt
from tools import TOOLS, handle_tool, resolve_food_items, log_food_items_bulk
import oura as oura_module
import research_scout

# ─────────────────────────── Config ───────────────────────────

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
OPENAI_KEY = os.getenv("OPENAI_API_KEY", "")
_raw_ids = os.getenv("ALLOWED_USER_IDS", "")
ALLOWED_IDS: set[int] = {int(x) for x in _raw_ids.split(",") if x.strip().isdigit()}

# Daily Oura auto-sync time (Amsterdam time), default 10:00
_sync_time_str = os.getenv("OURA_SYNC_TIME", "10:00")
_sync_h, _sync_m = (int(x) for x in _sync_time_str.split(":"))
OURA_SYNC_TIME = dt_time(_sync_h, _sync_m, tzinfo=ZoneInfo("Europe/Amsterdam"))

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger(__name__)

client = AsyncOpenAI(api_key=OPENAI_KEY)

# Per-user conversation history (in-memory, resets on restart)
_history: dict[int, list] = defaultdict(list)
# Per-user active role
_role: dict[int, str] = defaultdict(lambda: "coach")
# Per-user pending food items waiting for confirmation after photo
_pending_food: dict[int, list] = {}

# Words that confirm pending food log
_CONFIRM_WORDS = frozenset({
    "да", "ок", "ok", "oke", "верно", "всё верно", "всё так", "хорошо",
    "залогируй", "логируй", "+", "✅", "👍", "yes", "yep",
})

MAX_HISTORY = 30  # messages kept per user

ROLES = {"coach", "strategist", "cmo", "analyst", "behaviorist"}

HELP_TEXT = """\
*Health OS Bot*

Просто пиши что угодно — Coach отвечает по умолчанию.

*Команды:*
/daily — утренний чек-ин
/status — сводка дня (калории, белок, тренировки)
/review — недельная ревизия (Strategist)
/strategy — стратегический review (CMO)
/labs <данные> — загрузить анализы (Analyst)
/oura [дата] — синхронизировать данные с кольцом
/recipes — список всех сохранённых рецептов
/crisis <ситуация> — кризисная поддержка (Behaviorist)
/digest — научный дайджест недели
/research — статистика Research Scout
/research run — запустить скаутинг вручную
/role <роль> — сменить роль
/clear — очистить историю диалога

*Роли:* coach · strategist · cmo · analyst · behaviorist
"""


# ─────────────────────────── Helpers ──────────────────────────

def _allowed(update: Update) -> bool:
    if not ALLOWED_IDS:
        return True
    return update.effective_user.id in ALLOWED_IDS


async def _send(update: Update, text: str):
    """Send text, splitting at 4000 chars to respect Telegram limits."""
    for i in range(0, max(len(text), 1), 4000):
        chunk = text[i : i + 4000]
        await update.message.reply_text(chunk, parse_mode="Markdown")


async def _claude(user_id: int, content: "str | list", role: str) -> str:
    """
    Call OpenAI with function-calling loop. Returns the final text response.
    content can be a plain string or a multimodal list (for images).
    Maintains per-user conversation history.
    """
    history = _history[user_id]
    history.append({"role": "user", "content": content})

    if len(history) > MAX_HISTORY:
        history = history[-MAX_HISTORY:]
        _history[user_id] = history

    system = build_system_prompt(role)

    # Agentic function-calling loop
    while True:
        for attempt in range(3):
            try:
                response = await client.chat.completions.create(
                    model="gpt-4o",
                    max_tokens=2048,
                    tools=TOOLS,
                    tool_choice="auto",
                    messages=[{"role": "system", "content": system}] + history,
                )
                break
            except RateLimitError as e:
                if attempt == 2:
                    raise
                wait = 5 * (attempt + 1)
                log.warning("Rate limit hit, retrying in %ss (attempt %d/3)", wait, attempt + 1)
                await asyncio.sleep(wait)

        message = response.choices[0].message

        if message.tool_calls:
            # Append assistant message (with tool_calls) to history
            history.append(message.model_dump(exclude_unset=False))

            # Execute each tool and append results
            pending_images: list[str] = []  # base64 images from lab files

            for tc in message.tool_calls:
                args = json.loads(tc.function.arguments)
                log.info("Tool call: %s %s", tc.function.name, args)
                if tc.function.name == "sync_oura":
                    result = await oura_module.handle_tool(args)
                else:
                    result = handle_tool(tc.function.name, args)

                # Check if tool returned image data (lab files)
                if isinstance(result, dict) and result.get("type") == "image":
                    pending_images.append(result["data"])
                    history.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": f"Файл загружен: {result.get('filename')}. Изображение передано в чат для анализа.",
                    })
                elif isinstance(result, dict) and result.get("type") == "images":
                    pending_images.extend(result["data"])
                    history.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": f"Файл загружен: {result.get('filename')} ({len(result['data'])} стр.). Изображения переданы в чат для анализа.",
                    })
                else:
                    history.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": str(result),
                        }
                    )

            # Inject lab images into conversation so the model can see them
            if pending_images:
                img_content: list = [{"type": "text", "text": "Изображения файла с анализами для визуального распознавания:"}]
                for b64 in pending_images:
                    img_content.append({
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
                    })
                history.append({"role": "user", "content": img_content})
        else:
            text = message.content or "..."
            history.append({"role": "assistant", "content": text})
            return text


# ─────────────────────────── Handlers ─────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _allowed(update):
        return
    await _send(update, HELP_TEXT)


async def cmd_daily(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _allowed(update):
        return
    uid = update.effective_user.id
    _role[uid] = "coach"
    _history[uid] = []  # fresh context for daily check-in

    await update.effective_chat.send_action("typing")
    reply = await _claude(
        uid,
        "Сделай утренний чек-ин. Сначала синхронизируй Oura (сон, готовность, активность). "
        "Потом покажи: сон из Oura, readiness score, бюджет на сегодня (калории, белок) и план тренировки.",
        "coach",
    )
    await _send(update, reply)


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _allowed(update):
        return
    uid = update.effective_user.id

    await update.effective_chat.send_action("typing")
    reply = await _claude(
        uid,
        "Покажи сводку дня: что съедено, остаток бюджета, тренировка выполнена?",
        _role[uid],
    )
    await _send(update, reply)


async def cmd_review(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _allowed(update):
        return
    uid = update.effective_user.id
    _role[uid] = "strategist"
    _history[uid] = []

    await update.effective_chat.send_action("typing")
    reply = await _claude(
        uid,
        "Сделай недельную ревизию: compliance по питанию и тренировкам, тренды веса, рекомендации.",
        "strategist",
    )
    await _send(update, reply)


async def cmd_strategy(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _allowed(update):
        return
    uid = update.effective_user.id
    _role[uid] = "cmo"
    _history[uid] = []

    await update.effective_chat.send_action("typing")
    reply = await _claude(
        uid,
        "Проведи стратегический review. Оцени риски по Four Horsemen, проверь актуальность директив.",
        "cmo",
    )
    await _send(update, reply)


async def cmd_labs(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _allowed(update):
        return
    uid = update.effective_user.id
    _role[uid] = "analyst"
    _history[uid] = []

    data = " ".join(ctx.args) if ctx.args else ""
    prompt = (
        f"Проанализируй результаты анализов и обнови biomarkers.yaml. Данные: {data}"
        if data
        else "Жду результаты анализов. Отправь данные текстом или фото."
    )

    await update.effective_chat.send_action("typing")
    reply = await _claude(uid, prompt, "analyst")
    await _send(update, reply)


async def cmd_crisis(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _allowed(update):
        return
    uid = update.effective_user.id
    _role[uid] = "behaviorist"

    situation = " ".join(ctx.args) if ctx.args else ""
    prompt = f"Нужна поддержка. {situation}" if situation else "Нужна поддержка."

    await update.effective_chat.send_action("typing")
    reply = await _claude(uid, prompt, "behaviorist")
    await _send(update, reply)


async def cmd_role(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _allowed(update):
        return
    uid = update.effective_user.id

    if not ctx.args or ctx.args[0] not in ROLES:
        current = _role[uid]
        await update.message.reply_text(
            f"Текущая роль: *{current}*\n\nДоступные роли: {', '.join(sorted(ROLES))}",
            parse_mode="Markdown",
        )
        return

    _role[uid] = ctx.args[0]
    await update.message.reply_text(f"Роль переключена: *{ctx.args[0]}*", parse_mode="Markdown")


async def cmd_clear(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _allowed(update):
        return
    uid = update.effective_user.id
    _history[uid] = []
    await update.message.reply_text("История диалога очищена.")


def _parse_json_array(text: str) -> list:
    """Parse a JSON array from Claude response, trying multiple strategies."""
    import re as _re
    text = text.strip()

    if "```" in text:
        for block in text.split("```")[1::2]:
            cleaned = block.strip().lstrip("json").strip()
            try:
                return json.loads(cleaned)
            except json.JSONDecodeError:
                continue

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    match = _re.search(r'\[[\s\S]*?\]', text)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass

    raise ValueError(f"Не удалось распарсить JSON: {text[:300]}")


async def _analyze_food_with_gpt(b64: str, caption: str) -> list:
    """
    Use GPT-4o to analyze a food photo.
    Returns a list of food items with estimated grams and full nutrition.
    """
    extra = f"\nUser note: {caption}" if caption else ""
    prompt = (
        "Analyze this food photo. Return ONLY a valid JSON array — no markdown, no explanation.\n\n"
        "For each visible food item return an object with ALL these fields:\n"
        "{\n"
        '  "name": "название на русском — КРАТКО: овсянка / куриная грудка / яйцо / банан",\n'
        '  "quantity": "2 шт",\n'
        '  "estimated_g": 120,\n'
        '  "portion_note": "краткое описание порции",\n'
        '  "calories": 156,\n'
        '  "protein": 13.0,\n'
        '  "fat": 11.0,\n'
        '  "carbs": 1.0,\n'
        '  "fiber": 0.0,\n'
        '  "glycemic_index": null,\n'
        '  "insulin_index": 31\n'
        "}\n\n"
        "Rules:\n"
        "- One entry per distinct food item; mixed dish (soup, stew, salad) = one entry\n"
        "- name: BASE ingredient only. Good: 'овсянка', 'куриная грудка'. Bad: 'жареная куриная грудка на гриле'\n"
        "- quantity: human-readable count/volume ('2 шт', '1 кусок', '200 мл', '3 ст.л.')\n"
        "- estimated_g: total weight of this item on the plate\n"
        "- Scale ALL nutrition to the estimated portion (NOT per 100g)\n"
        "- glycemic_index: integer 0-100, or null for meat/fish/eggs/cheese/pure fat\n"
        "- insulin_index: integer 0-160 (reference: white bread=100). "
        "  Eggs≈31, beef≈51, fish≈59, white rice≈79, banana≈84, yogurt≈115. "
        "  null only if genuinely impossible to estimate\n"
        "- fiber: 0.0 if none, null only if truly unknown\n"
        "- Be conservative with gram estimates; use plate/hand/utensil as reference"
        + extra
    )

    response = await client.chat.completions.create(
        model="gpt-4o",
        max_tokens=1024,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                {"type": "text", "text": prompt},
            ],
        }],
    )

    return _parse_json_array(response.choices[0].message.content or "[]")


async def _apply_food_corrections(pending: list, correction: str) -> list:
    """
    Apply user correction text to pending food items.
    Returns updated items list (same structure, recalculated nutrition if grams changed).
    """
    pending_json = json.dumps(pending, ensure_ascii=False, indent=2)
    prompt = (
        f"Current food items (JSON):\n{pending_json}\n\n"
        f"User correction: «{correction}»\n\n"
        "Apply the correction. You may change grams, replace/add/remove items, "
        "recalculate macros proportionally. "
        "Keep all fields: name, quantity, estimated_g, calories, protein, fat, carbs, "
        "fiber, glycemic_index, insulin_index, portion_note, source.\n"
        "Return ONLY the updated JSON array. No markdown, no explanation."
    )

    response = await client.chat.completions.create(
        model="gpt-4o",
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )

    return _parse_json_array(response.choices[0].message.content or "[]")


def _build_confirmation_card(items: list) -> str:
    lines = ["*Распознано на фото:*\n"]
    total_cal = total_prot = total_fat = total_carbs = total_fiber = 0.0

    for i, item in enumerate(items, 1):
        name = item.get("name", "?")
        qty = item.get("quantity", "")
        g = item.get("estimated_g", 0)
        cal = item.get("calories") or 0
        prot = item.get("protein") or 0
        fat = item.get("fat") or 0
        carbs = item.get("carbs") or 0
        fiber = item.get("fiber")
        gi = item.get("glycemic_index")
        ii = item.get("insulin_index")
        src = item.get("source", "estimated")
        note = item.get("portion_note", "")

        mark = "📚" if src == "recipe" else "~"

        # Header: name, quantity, grams
        qty_str = f" ({qty})" if qty else ""
        line = f"{i}. *{name}*{qty_str} {mark} — {g}г\n"

        # Macros line
        fiber_str = f" Кл:{round(fiber, 1)}г" if fiber is not None else ""
        line += f"   🔥 {round(cal)} ккал | Б:{round(prot,1)}г Ж:{round(fat,1)}г У:{round(carbs,1)}г{fiber_str}\n"

        # Indexes line
        gi_str = str(gi) if gi is not None else "—"
        ii_str = str(ii) if ii is not None else "—"
        line += f"   ГИ: {gi_str} | ИИ: {ii_str}"

        if note:
            line += f"\n   _{note}_"

        lines.append(line)
        total_cal += cal
        total_prot += prot
        total_fat += fat
        total_carbs += carbs
        if fiber:
            total_fiber += fiber

    lines.append("")
    lines.append("─" * 20)
    total_line = (
        f"*Итого:* {round(total_cal)} ккал | "
        f"Б:{round(total_prot,1)}г Ж:{round(total_fat,1)}г У:{round(total_carbs,1)}г"
    )
    if total_fiber:
        total_line += f" Кл:{round(total_fiber,1)}г"
    lines.append(total_line)
    lines.append("_📚 = из базы  ~ = оценка  ГИ = гликемический  ИИ = инсулиновый_")
    lines.append("\nНапиши *да* — залогирую.")
    lines.append("Или поправь: _«яиц было 3»_, _«порция овсянки 200г»_, _«убери банан»_")
    return "\n".join(lines)


async def cmd_recipes(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Show all saved recipes from Supabase."""
    if not _allowed(update):
        return
    from collections import defaultdict
    import db as db_module

    recipes = db_module.get_all_recipes()
    if not recipes:
        await update.message.reply_text("Рецептов пока нет.")
        return

    by_cat: dict = defaultdict(list)
    for r in recipes:
        cat = r.get("category") or "Другое"
        by_cat[cat].append(r)

    lines = [f"*Рецепты* — {len(recipes)} шт.\n"]
    for cat, items in sorted(by_cat.items()):
        lines.append(f"*{cat}*")
        for r in items:
            est = " _(~)_" if r.get("estimated") else ""
            gi = f", ГИ {r['glycemic_index']}" if r.get("glycemic_index") is not None else ""
            lines.append(
                f"• {r['name']}{est} — {r.get('calories', '?')} ккал, "
                f"{r.get('protein', '?')}г белка{gi} / {r.get('serving_g', '?')}г"
            )
        lines.append("")

    lines.append("_(~) = оценочные данные_")
    await _send(update, "\n".join(lines))


async def cmd_oura(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Manually sync Oura data and show summary."""
    if not _allowed(update):
        return
    uid = update.effective_user.id

    d = ctx.args[0] if ctx.args else None
    await update.effective_chat.send_action("typing")
    reply = await _claude(
        uid,
        f"Синхронизируй данные из Oura Ring{' за ' + d if d else ' за сегодня'}. "
        "Покажи сон, готовность, активность и Zone 2 минуты. Запиши сон в лог.",
        _role[uid],
    )
    await _send(update, reply)


async def handle_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handle food/lab photos sent by the user."""
    if not _allowed(update):
        return

    uid = update.effective_user.id
    role = _role[uid]
    caption = update.message.caption or ""

    await update.effective_chat.send_action("typing")

    # Download highest-resolution Telegram photo
    photo = update.message.photo[-1]
    tg_file = await ctx.bot.get_file(photo.file_id)
    photo_bytes = await tg_file.download_as_bytearray()
    b64 = base64.standard_b64encode(bytes(photo_bytes)).decode()

    # Lab results → existing GPT-4o flow
    if role == "analyst":
        prompt = (
            "Это результаты анализов. Распознай все показатели, "
            "сравни с оптимальными значениями по Attia и дай оценку."
            + (f" Комментарий: {caption}" if caption else "")
        )
        content = [
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
            {"type": "text", "text": prompt},
        ]
        try:
            reply = await _claude(uid, content, role)
        except Exception as e:
            log.exception("Photo (analyst) error: %s", e)
            reply = f"Не удалось обработать фото: {e}"
        await _send(update, reply)
        return

    # Food photo → GPT-4o analyzes, then confirmation card
    try:
        raw_items = await _analyze_food_with_gpt(b64, caption)
    except Exception as e:
        log.exception("GPT-4o food analysis failed: %s", e)
        await _send(update, f"⚠️ Не удалось распознать еду на фото ({e}). Залогируй вручную — напиши что съела и я запишу.")
        return

    # Resolve items: scale from recipe DB or keep Claude estimates
    items = resolve_food_items(raw_items)

    if not items:
        await _send(update, "Не удалось распознать еду на фото. Опиши словами что съела.")
        return

    # Store pending and show confirmation card
    _pending_food[uid] = items
    card = _build_confirmation_card(items)
    await _send(update, card)


async def _transcribe_audio(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> str:
    """Download voice/audio from Telegram and transcribe with Whisper."""
    if update.message.voice:
        file_id = update.message.voice.file_id
        filename = "voice.ogg"
    elif update.message.audio:
        file_id = update.message.audio.file_id
        filename = update.message.audio.file_name or "audio.mp3"
    else:
        return ""

    tg_file = await ctx.bot.get_file(file_id)
    audio_bytes = await tg_file.download_as_bytearray()

    transcript = await client.audio.transcriptions.create(
        model="whisper-1",
        file=(filename, bytes(audio_bytes)),
    )
    return transcript.text.strip()


async def handle_voice(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handle voice and audio messages — transcribe and route as text."""
    if not _allowed(update):
        return

    await update.effective_chat.send_action("typing")

    try:
        text = await _transcribe_audio(update, ctx)
    except Exception as e:
        log.exception("Whisper transcription failed: %s", e)
        await _send(update, f"⚠️ Не удалось распознать голосовое сообщение ({e}).")
        return

    if not text:
        await _send(update, "⚠️ Не удалось распознать голосовое сообщение.")
        return

    # Echo transcription so user sees what was recognised
    await _send(update, f"🎤 _{text}_")

    # Inject transcribed text into update and reuse handle_message logic
    update.message.text = text
    await handle_message(update, ctx)


async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _allowed(update):
        return

    uid = update.effective_user.id
    text = update.message.text or ""
    role = _role[uid]

    # ── Pending food confirmation after photo ──────────────────
    if uid in _pending_food and _pending_food[uid]:
        text_norm = text.strip().lower()

        if text_norm in {"нет", "отмена", "cancel", "не надо"}:
            del _pending_food[uid]
            await _send(update, "Отменено, ничего не залогировано.")
            return

        if text_norm in _CONFIRM_WORDS:
            pending = _pending_food.pop(uid)
            result = log_food_items_bulk(pending)
            lines = [
                f"✅ Залогировано {result['logged_count']} позиций",
                f"День: *{result['day_total']['calories']}* ккал, {result['day_total']['protein']}г белка",
                f"Осталось: *{result['remaining']['calories']}* ккал, {result['remaining']['protein']}г белка",
            ]
            await _send(update, "\n".join(lines))
            return

        # User is making corrections → apply and show updated card (don't log yet)
        pending = _pending_food[uid]
        await update.effective_chat.send_action("typing")
        try:
            updated_raw = await _apply_food_corrections(pending, text)
            updated_items = resolve_food_items(updated_raw)
            _pending_food[uid] = updated_items
            card = _build_confirmation_card(updated_items)
            await _send(update, card)
        except Exception as e:
            log.exception("Food correction error: %s", e)
            await _send(update, f"Не смог применить правку: {e}\nНапиши *да* чтобы залогировать как есть, или *отмена*.")
        return

    # ── Normal message ─────────────────────────────────────────
    await update.effective_chat.send_action("typing")

    try:
        reply = await _claude(uid, text, role)
    except Exception as e:
        log.exception("Claude error: %s", e)
        reply = f"Ошибка: {e}\n\nПопробуй ещё раз или /clear чтобы сбросить историю."

    await _send(update, reply)


# ─────────────────────── Scheduled jobs ───────────────────────

async def _daily_oura_sync(context: ContextTypes.DEFAULT_TYPE):
    """Fetch Oura data automatically each morning and notify allowed users."""
    from datetime import date
    d = date.today().isoformat()
    try:
        data = await oura_module.handle_tool({"date": d, "write_to_log": True})
        sleep = data.get("sleep", {})
        readiness = data.get("readiness", {})
        activity = data.get("activity", {})
        stress = data.get("stress", {})

        lines = [f"*Oura синхронизирован* — {d}"]
        if sleep.get("hours"):
            lines.append(
                f"Сон: {sleep['hours']}ч"
                + (f", score {sleep['score']}" if sleep.get("score") else "")
                + (f", HRV {sleep['avg_hrv']}" if sleep.get("avg_hrv") else "")
                + (f", resting HR {sleep['resting_hr']}" if sleep.get("resting_hr") else "")
            )
        if readiness.get("score") is not None:
            lines.append(f"Готовность: {readiness['score']}")
        if activity.get("steps"):
            lines.append(
                f"Шаги: {activity['steps']}"
                + (f", Zone2 {activity['zone2_min']} мин" if activity.get("zone2_min") else "")
                + (f", {activity['active_calories']} ккал" if activity.get("active_calories") else "")
            )
        if stress.get("summary"):
            lines.append(f"Стресс: {stress['summary']}")

        msg = "\n".join(lines)
        log.info("daily_oura_sync ok: %s", d)
    except Exception as e:
        log.error("daily_oura_sync error: %s", e)
        msg = f"Не удалось синхронизировать Oura: {e}"

    for uid in ALLOWED_IDS:
        try:
            await context.bot.send_message(uid, msg, parse_mode="Markdown")
        except Exception as exc:
            log.warning("Could not notify user %s: %s", uid, exc)


# ────────────────── Research Scout jobs ───────────────────────

async def _research_scout_job(context: ContextTypes.DEFAULT_TYPE):
    """Ежедневный скаутинг научных источников (05:00 UTC)."""
    log.info("Research Scout: запуск")
    try:
        count = await research_scout.run_daily_scout(_anthropic)
        log.info("Research Scout: найдено %d новых статей", count)
    except Exception as e:
        log.error("Research Scout ошибка: %s", e)


async def _research_digest_job(context: ContextTypes.DEFAULT_TYPE):
    """Воскресный дайджест научных находок (10:00 UTC)."""
    log.info("Research Digest: генерация")
    try:
        digest = await research_scout.generate_weekly_digest(_anthropic)
        for uid in ALLOWED_IDS:
            try:
                await context.bot.send_message(uid, digest, parse_mode="Markdown")
            except Exception as exc:
                log.warning("Не удалось отправить дайджест user %s: %s", uid, exc)
    except Exception as e:
        log.error("Research Digest ошибка: %s", e)


async def cmd_digest(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Запросить дайджест вручную."""
    if not _allowed(update):
        return
    await update.effective_chat.send_action("typing")
    try:
        digest = await research_scout.generate_weekly_digest(_anthropic)
    except Exception as e:
        digest = f"Ошибка генерации дайджеста: {e}"
    await _send(update, digest)


async def cmd_research(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Показать статистику Research Scout или запустить скаутинг вручную."""
    if not _allowed(update):
        return
    if ctx.args and ctx.args[0] == "run":
        await update.effective_chat.send_action("typing")
        try:
            count = await research_scout.run_daily_scout(_anthropic)
            await _send(update, f"✅ Скаутинг завершён: {count} новых статей добавлено.")
        except Exception as e:
            await _send(update, f"Ошибка: {e}")
    else:
        stats = research_scout.get_stats()
        await _send(update, stats)


# ─────────────────────────── Main ─────────────────────────────

def main():
    if not TELEGRAM_TOKEN:
        raise SystemExit("TELEGRAM_BOT_TOKEN not set in .env")
    if not OPENAI_KEY:
        raise SystemExit("OPENAI_API_KEY not set in .env")

    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("daily", cmd_daily))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("review", cmd_review))
    app.add_handler(CommandHandler("strategy", cmd_strategy))
    app.add_handler(CommandHandler("labs", cmd_labs))
    app.add_handler(CommandHandler("oura", cmd_oura))
    app.add_handler(CommandHandler("recipes", cmd_recipes))
    app.add_handler(CommandHandler("crisis", cmd_crisis))
    app.add_handler(CommandHandler("role", cmd_role))
    app.add_handler(CommandHandler("clear", cmd_clear))
    app.add_handler(CommandHandler("digest", cmd_digest))
    app.add_handler(CommandHandler("research", cmd_research))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Scheduled jobs
    app.job_queue.run_daily(_daily_oura_sync, time=OURA_SYNC_TIME)
    app.job_queue.run_daily(
        _research_scout_job,
        time=dt_time(5, 0, tzinfo=ZoneInfo("UTC")),
    )
    app.job_queue.run_daily(
        _research_digest_job,
        time=dt_time(10, 0, tzinfo=ZoneInfo("UTC")),
        days=(6,),  # воскресенье (0=пн, 6=вс)
    )
    log.info(
        "Health OS Bot started. Allowed users: %s. Oura sync at %s daily.",
        ALLOWED_IDS or "everyone",
        OURA_SYNC_TIME.strftime("%H:%M"),
    )
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
