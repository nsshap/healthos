"""
Research Scout — ежедневный мониторинг научных источников.

Каждый день:
  - Парсит 9 RSS/Atom фидов (подкасты, журналы, блоги)
  - Скорит релевантность 0-10 через Claude
  - Сохраняет в локальный JSON-кэш

По воскресеньям:
  - Генерирует дайджест топ-находок недели
  - Отправляет в Telegram
"""

import asyncio
import hashlib
import json
import logging
from datetime import date, datetime, timedelta
from pathlib import Path

import feedparser
import httpx
from openai import AsyncOpenAI

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
        "url": "https://anchor.fm/s/dd6922b4/podcast/rss",
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

CACHE_FILE = Path(__file__).parent / "research_items.json"
MAX_ITEMS_PER_FEED = 10      # сколько свежих статей брать с каждого фида
DIGEST_MIN_SCORE = 6         # минимальный score для попадания в дайджест
DIGEST_TOP_N = 7             # сколько статей показывать в дайджесте
RETENTION_DAYS = 30          # сколько дней хранить статьи в кэше
SCORE_BATCH_SIZE = 12        # статей за один вызов Claude при скоринге

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


# ────────────────── Кэш ─────────────────────────

def _load_cache() -> list[dict]:
    if not CACHE_FILE.exists():
        return []
    try:
        return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning("Не удалось загрузить кэш: %s", e)
        return []


def _save_cache(items: list[dict]) -> None:
    CACHE_FILE.write_text(
        json.dumps(items, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _item_id(url: str) -> str:
    return hashlib.md5(url.encode()).hexdigest()[:12]


def _prune_cache(items: list[dict]) -> list[dict]:
    """Удалить статьи старше RETENTION_DAYS дней."""
    cutoff = (datetime.utcnow() - timedelta(days=RETENTION_DAYS)).isoformat()
    return [i for i in items if i.get("fetched_at", "") >= cutoff]


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
            link = entry.get("link", "").strip()
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
        headers={"User-Agent": "HealthOS-ResearchScout/1.0"},
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

async def run_daily_scout(anthropic_client: AsyncOpenAI) -> int:
    """
    Ежедневный скаутинг:
    1. Загрузить новые статьи из всех фидов
    2. Убрать дубли (уже есть в кэше)
    3. Оценить релевантность
    4. Добавить в кэш
    Возвращает количество новых статей.
    """
    cache = _load_cache()
    existing_ids = {item["id"] for item in cache}

    raw = await _fetch_all_feeds()
    new_items = [item for item in raw if item["id"] not in existing_ids]
    log.info("Новых статей (после дедупликации): %d", len(new_items))

    if new_items:
        new_items = await _score_articles(anthropic_client, new_items)
        cache.extend(new_items)

    cache = _prune_cache(cache)
    _save_cache(cache)
    return len(new_items)


# ────────────────── Воскресный дайджест ─────────

async def generate_weekly_digest(anthropic_client: AsyncOpenAI) -> str:
    """
    Генерирует дайджест топ-находок за прошедшую неделю.
    Возвращает отформатированный текст для Telegram.
    """
    cache = _load_cache()
    week_ago = (datetime.utcnow() - timedelta(days=7)).isoformat()
    week_items = [
        item for item in cache
        if item.get("fetched_at", "") >= week_ago
        and (item.get("score") or 0) >= DIGEST_MIN_SCORE
    ]

    if not week_items:
        return "📭 *Research Digest*\n\nЗа эту неделю релевантных находок не было."

    # Сортируем по score, берём топ N
    top = sorted(week_items, key=lambda x: x.get("score", 0), reverse=True)[:DIGEST_TOP_N]

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
        response = await anthropic_client.chat.completions.create(
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
    """Краткая статистика кэша для команды /research."""
    cache = _load_cache()
    if not cache:
        return "Кэш пуст. Скаутинг ещё не запускался."

    week_ago = (datetime.utcnow() - timedelta(days=7)).isoformat()
    week_items = [i for i in cache if i.get("fetched_at", "") >= week_ago]
    high_score = [i for i in week_items if (i.get("score") or 0) >= DIGEST_MIN_SCORE]

    by_source: dict[str, int] = {}
    for item in week_items:
        src = item["source"]
        by_source[src] = by_source.get(src, 0) + 1

    lines = [
        f"*Research Scout*\n",
        f"В кэше: {len(cache)} статей за {RETENTION_DAYS} дней",
        f"За неделю: {len(week_items)} новых, {len(high_score)} с score ≥ {DIGEST_MIN_SCORE}",
        "\n*По источникам (за неделю):*",
    ]
    for src, count in sorted(by_source.items(), key=lambda x: -x[1]):
        lines.append(f"• {src}: {count}")

    return "\n".join(lines)
