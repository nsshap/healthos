"""
Research Scout — ежедневный мониторинг научных источников.

Каждый день:
  - Парсит 8 RSS/Atom фидов (подкасты, журналы, блоги)
  - Скорит релевантность 0-10 через gpt-4o-mini
  - Сохраняет в Supabase (таблица research_items)

По воскресеньям:
  - Генерирует дайджест топ-находок недели через gpt-4o
  - Отправляет в Telegram
"""

import asyncio
import hashlib
import json
import logging
from datetime import date, datetime, timedelta


import feedparser
import httpx
from openai import AsyncOpenAI

import db  # Supabase storage

log = logging.getLogger(__name__)

# ─────────────────── RSS фиды ───────────────────

FEEDS = [
    {
        "name": "Peter Attia Drive",
        "url": "https://peterattiamd.com/feed/",
        "type": "podcast",
    },
    {
        "name": "Huberman Lab",
        "url": "https://feeds.megaphone.fm/hubermanlab",
        "type": "podcast",
    },
    {
        "name": "FoundMyFitness (Rhonda Patrick)",
        "url": "https://rss.libsyn.com/shows/51714/destinations/184296.xml",
        "type": "podcast",
    },
    {
        "name": "Matt Walker Podcast",
        "url": "https://rss.buzzsprout.com/1821163.rss",
        "type": "podcast",
    },
    {
        "name": "Nature Aging",
        "url": "https://www.nature.com/nataging.rss",
        "type": "journal",
    },
    {
        "name": "Aging Cell",
        "url": "https://onlinelibrary.wiley.com/feed/14749726/most-recent",
        "type": "journal",
    },
    {
        "name": "Fight Aging!",
        "url": "https://www.fightaging.org/feed/",
        "type": "blog",
    },
    {
        "name": "InsideTracker Blog",
        "url": "https://blog.insidetracker.com/rss.xml",
        "type": "blog",
    },
]

# ─────────────────── Конфиг ─────────────────────

MAX_ITEMS_PER_FEED = 10      # сколько свежих статей брать с каждого фида
DIGEST_MIN_SCORE = 6         # минимальный score для попадания в дайджест
DIGEST_TOP_N = 7             # сколько статей показывать в дайджесте
SCORE_BATCH_SIZE = 12        # статей за один вызов gpt-4o-mini при скоринге

# Профиль для скоринга релевантности — можно адаптировать
HEALTH_PROFILE = """
Пользователь оптимизирует здоровье и долголетие по подходу Medicine 3.0 (Peter Attia).

Приоритетные темы (score 8-10):
- Сон: качество, архитектура, влияние на восстановление и когнитивные функции
- Метаболическое здоровье: инсулинорезистентность, глюкоза, HbA1c, митохондрии
- Кардиоваскулярное здоровье: Zone 2, VO2max, ApoB, LDL, сердечно-сосудистые риски
- Силовые тренировки: гипертрофия, остеопения, прогрессивная перегрузка
- Longevity-добавки: омега-3, витамин D, магний, NMN/НАД+, сенолитики
- Биомаркеры: анализы крови, оптимальные диапазоны, тренды

Релевантные темы (score 5-7):
- Питание: белок, временное ограничение питания, микронутриенты
- Стресс и восстановление: HRV, кортизол, адаптогены
- Когнитивные функции и нейропротекция
- Гормоны и старение: тестостерон, эстроген, тиреоид
- Кишечный микробиом и иммунитет

Низкий приоритет (score 1-4):
- Общие новости о здоровье без конкретики
- Фитнес для похудения без связи с longevity
- Маркетинговый контент
"""


def _item_id(url: str) -> str:
    return hashlib.md5(url.encode()).hexdigest()[:12]


# ────────────────── Фетчинг фидов ───────────────

async def _fetch_feed(client: httpx.AsyncClient, feed: dict) -> list[dict]:
    """Скачать один RSS/Atom фид и вернуть список статей."""
    name = feed["name"]
    url = feed["url"]
    try:
        resp = await client.get(url, timeout=15)
        resp.raise_for_status()
        parsed = feedparser.parse(resp.text)
        items = []
        for entry in parsed.entries[:MAX_ITEMS_PER_FEED]:
            title = entry.get("title", "").strip()
            link = (entry.get("link") or "").strip()
            # Fallback: enclosure URL (e.g. Buzzsprout audio) or entry id
            if not link:
                enclosures = entry.get("enclosures") or entry.get("links") or []
                for enc in enclosures:
                    if enc.get("href"):
                        link = enc["href"]
                        break
            if not link:
                link = entry.get("id", "")
            summary = entry.get("summary", entry.get("description", "")).strip()
            # Обрезать summary до 300 символов
            if len(summary) > 300:
                summary = summary[:297] + "..."
            published = entry.get("published", entry.get("updated", ""))
            if title and link:
                items.append({
                    "id": _item_id(link),
                    "source": name,
                    "type": feed["type"],
                    "title": title,
                    "url": link,
                    "summary": summary,
                    "published": published,
                    "fetched_at": datetime.utcnow().isoformat(),
                    "score": None,
                })
        log.info("Фид %s: %d статей", name, len(items))
        return items
    except Exception as e:
        log.warning("Ошибка фида %s: %s", name, e)
        return []


async def _fetch_all_feeds() -> list[dict]:
    """Параллельно скачать все фиды."""
    async with httpx.AsyncClient(
        headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"},
        follow_redirects=True,
    ) as client:
        tasks = [_fetch_feed(client, feed) for feed in FEEDS]
        results = await asyncio.gather(*tasks)
    all_items = [item for feed_items in results for item in feed_items]
    log.info("Всего статей из фидов: %d", len(all_items))
    return all_items


# ────────────────── Скоринг ─────────────────────

async def _score_batch(client: AsyncOpenAI, batch: list[dict]) -> list[dict]:
    """Оценить релевантность пачки статей через Claude. Возвращает ту же пачку с полем score."""
    items_text = "\n".join(
        f"{i+1}. [{item['source']}] {item['title']}"
        + (f"\n   {item['summary']}" if item["summary"] else "")
        for i, item in enumerate(batch)
    )
    prompt = (
        f"Оцени релевантность каждой статьи для пользователя с профилем:\n{HEALTH_PROFILE}\n\n"
        f"Статьи:\n{items_text}\n\n"
        "Верни ТОЛЬКО JSON-массив с числами (score 0-10) в том же порядке.\n"
        "Пример для 3 статей: [7, 3, 9]\n"
        "Никакого текста, только массив."
    )
    try:
        response = await client.chat.completions.create(
            model="gpt-4o-mini",
            max_tokens=256,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.choices[0].message.content.strip()
        scores = json.loads(raw)
        if len(scores) == len(batch):
            for item, score in zip(batch, scores):
                item["score"] = int(score)
        else:
            log.warning("Скоринг: получено %d оценок для %d статей", len(scores), len(batch))
    except Exception as e:
        log.warning("Ошибка скоринга: %s", e)
    return batch


async def _score_articles(client: AsyncOpenAI, items: list[dict]) -> list[dict]:
    """Скорить все статьи батчами."""
    to_score = [i for i in items if i["score"] is None]
    for start in range(0, len(to_score), SCORE_BATCH_SIZE):
        batch = to_score[start : start + SCORE_BATCH_SIZE]
        await _score_batch(client, batch)
    return items


# ────────────────── Основной scout ──────────────

async def run_daily_scout(openai_client: AsyncOpenAI) -> int:
    """
    Ежедневный скаутинг:
    1. Загрузить новые статьи из всех фидов
    2. Убрать дубли (по id из Supabase)
    3. Оценить релевантность
    4. Сохранить в Supabase
    Возвращает количество новых статей.
    """
    existing_ids = db.get_research_ids()

    raw = await _fetch_all_feeds()
    new_items = [item for item in raw if item["id"] not in existing_ids]
    log.info("Новых статей (после дедупликации): %d", len(new_items))

    if new_items:
        new_items = await _score_articles(openai_client, new_items)
        db.insert_research_items(new_items)

    return len(new_items)


# ────────────────── Воскресный дайджест ─────────

async def generate_weekly_digest(openai_client: AsyncOpenAI) -> str:
    """
    Генерирует дайджест топ-находок за прошедшую неделю.
    Возвращает отформатированный текст для Telegram.
    """
    week_ago = (datetime.utcnow() - timedelta(days=7)).isoformat()
    week_items = db.get_research_items_since(week_ago, min_score=DIGEST_MIN_SCORE)

    if not week_items:
        return "📭 *Research Digest*\n\nЗа эту неделю релевантных находок не было."

    # Берём топ N (уже отсортированы по score DESC из БД)
    top = week_items[:DIGEST_TOP_N]

    # Формируем промпт для синтеза
    articles_text = "\n\n".join(
        f"[{item['source']}, score {item['score']}]\n"
        f"Заголовок: {item['title']}\n"
        f"Краткое описание: {item['summary']}"
        for item in top
    )

    prompt = (
        "Ты — эксперт по здоровью и longevity. Составь краткий воскресный дайджест научных находок.\n\n"
        f"Профиль пользователя:\n{HEALTH_PROFILE}\n\n"
        f"Топ статей за неделю:\n{articles_text}\n\n"
        "Напиши дайджест в таком формате:\n"
        "1. Одна вводная фраза (что было актуально на этой неделе)\n"
        "2. 3-5 ключевых находок — для каждой: что нашли, почему важно для пользователя, "
        "   конкретное действие (если есть)\n"
        "3. Один общий вывод или рекомендация\n\n"
        "Язык: русский. Тон: конкретный, без воды. Длина: до 400 слов."
    )

    try:
        response = await openai_client.chat.completions.create(
            model="gpt-4o",
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
        synthesis = response.choices[0].message.content.strip()
    except Exception as e:
        log.error("Ошибка генерации дайджеста: %s", e)
        synthesis = "Не удалось сгенерировать синтез."

    # Собираем итоговое сообщение
    lines = [
        f"📚 *Research Digest* — {date.today().strftime('%d.%m.%Y')}",
        f"_{len(week_items)} статей за неделю, топ {len(top)} показаны_\n",
        synthesis,
        "\n─────────────────────",
        "*Источники:*",
    ]
    for item in top:
        score_bar = "🟢" if item["score"] >= 8 else "🟡"
        lines.append(f"{score_bar} [{item['title'][:60]}]({item['url']}) — {item['source']}")

    return "\n".join(lines)


# ────────────────── Статистика ──────────────────

def get_stats() -> str:
    """Краткая статистика из Supabase для команды /research."""
    try:
        stats = db.get_research_stats()
    except Exception as e:
        return f"Ошибка получения статистики: {e}"

    if stats["total"] == 0:
        return "База пуста. Скаутинг ещё не запускался."

    lines = [
        "*Research Scout*\n",
        f"В базе: {stats['total']} статей",
        f"За неделю: {stats['week_count']} новых, {stats['week_high_score']} с score ≥ {DIGEST_MIN_SCORE}",
        "\n*По источникам (за неделю):*",
    ]
    for src, count in sorted(stats["by_source"].items(), key=lambda x: -x[1]):
        lines.append(f"• {src}: {count}")

    return "\n".join(lines)
