"""
Weekly CGM review.

Pulls 7 days of meals × postprandial glucose responses from Supabase,
feeds the structured data into an LLM, and returns a clinical-style
narrative: what triggers spikes, what doesn't, and — when the signal is
ambiguous — concrete experiments to run next week.

Triggered both by the Sunday 09:00 (Amsterdam) scheduled job and by the
/glucose_weekly Telegram command.
"""
from __future__ import annotations

import json
import logging
from datetime import date

import libre

log = logging.getLogger(__name__)

_MODEL = "gpt-4o"


_SYSTEM_PROMPT = """\
Ты — нутрициолог-аналитик, работающий с непрерывным мониторингом глюкозы (CGM, Libre 3)
и подходом Peter Attia / Medicine 3.0. Пользователь — взрослый человек без диабета,
оптимизирует метаболическое здоровье.

Твоя задача — посмотреть на неделю данных «приём пищи → реакция глюкозы» и сделать
**клинический вывод**. Не отчёт со списками, не топ-N, а связный анализ, который
помогает сразу принимать решения.

КАК РАССУЖДАТЬ:
- Дельта пика (peak − baseline) — главный сигнал реакции.
  <2 ммоль/л = реакции нет, всё ок.
  2-3 ммоль/л = нормальный физиологический подъём.
  3-4 ммоль/л = заметный спайк, стоит обратить внимание.
  >4 ммоль/л = большой спайк, нужно разбираться.
- Целевой Time-in-Range (3.9-7.8) у здорового человека ≥70%, в идеале ≥85%.
- Коэффициент вариации (CV) <36% = метаболически стабильно. >36% = нестабильно.
- Контекст важен: один и тот же продукт может давать разную реакцию утром vs вечером,
  на тренировочный день vs день отдыха, после плохого сна vs хорошего.

СТРУКТУРА ОТВЕТА (без жёстких заголовков, плавный текст):

1. **Картина за неделю** — 1-2 предложения с конкретными цифрами (TIR, средняя, CV,
   эпизоды выше 10). Сразу скажи: всё стабильно / есть проблемы / нужно копать.

2. **Что точно вызывает спайки** — называй конкретные продукты или комбинации с
   цифрами. «Овсянка с мёдом утром даёт +3.8 ммоль/л стабильно (3 случая), пик
   около часа». Уверенно — только если есть ≥2 случая.

3. **Что точно НЕ вызывает скачков** — на что можно опираться без опаски, тоже с
   цифрами. Это важнее списка спайков: даёт безопасные дефолты.

4. **Гипотезы и эксперименты на следующую неделю** — если данных недостаточно,
   картина смазана или один продукт дал противоречивые реакции — предложи
   **конкретный эксперимент**. Не «попробуй разное», а «ешь Х в одно и то же
   время 3 раза подряд», «добавь 20г белка к Y и сравни», «походи 15 мин после
   обеда — посмотрим, снижает ли пик», «съешь крахмал на ужин и на завтрак в
   разные дни, сравним».

ТОН:
- Как доверенный врач, который видит лог и сразу комментирует, что важно.
- На «ты». Короткие предложения. Без воды. Без эмодзи (можно одно-два).
- Markdown: **bold** для ключевых выводов и продуктов. Без таблиц, без списков
  более 3 пунктов подряд.

ОГРАНИЧЕНИЯ:
- Если приёмов пищи с данными <5 — скажи «данных пока мало для уверенных выводов»
  и сосредоточься на гипотезах + эксперименте.
- Не повторяй сырые цифры, которые пользователь и так увидит в /glucose. Делай
  выводы.
- Не пытайся группировать блюда сам — пользователь специально просил этого не
  делать. Опирайся на текст description как есть.
- Не используй медицинские термины, требующие объяснения.

Возвращай только готовый текст ответа, без преамбулы и без «вот мой анализ:».\
"""


def _build_user_payload(data: dict) -> str:
    """Compact JSON snapshot of the week, kept readable for the LLM."""
    return json.dumps(data, ensure_ascii=False, indent=2, default=str)


async def generate_weekly_review(openai_client, end_date: date | None = None) -> str:
    """
    Build week data, send to LLM, return narrative text ready to post to Telegram.
    """
    try:
        await libre.sync_recent()
    except Exception as e:
        log.warning("sync before weekly review failed: %s", e)

    data = libre.build_weekly_data(end_date)

    total = data["data_coverage"]["total_meals_logged"]
    with_data = data["data_coverage"]["meals_with_glucose_response"]

    if with_data == 0:
        return (
            "За эту неделю данных «еда → реакция глюкозы» пока нет — "
            "либо CGM подключился недавно, либо приёмы пищи не логировались. "
            "Залогируй пару дней через бота и попробуй `/glucose_weekly` снова "
            "или подожди следующего воскресенья."
        )

    payload = _build_user_payload(data)

    header = (
        f"Период: {data['period']['start']} → {data['period']['end']} "
        f"({data['period']['days']} дней). "
        f"Логировано приёмов пищи: {total}, с реакцией CGM: {with_data} "
        f"(покрытие {data['data_coverage']['coverage_pct']}%).\n\n"
        f"ДАННЫЕ:\n{payload}"
    )

    response = await openai_client.chat.completions.create(
        model=_MODEL,
        max_tokens=1500,
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": header},
        ],
    )

    text = response.choices[0].message.content or ""
    return text.strip() or "Не удалось сгенерировать отчёт."
