-- Research Scout: таблица для хранения статей из RSS фидов
-- Запустить в Supabase: Dashboard → SQL Editor → New Query

CREATE TABLE IF NOT EXISTS research_items (
    id          TEXT PRIMARY KEY,          -- MD5-хэш URL
    source      TEXT NOT NULL,             -- Peter Attia Drive, Huberman Lab, etc.
    type        TEXT NOT NULL,             -- podcast | journal | blog
    title       TEXT NOT NULL,
    url         TEXT NOT NULL,
    summary     TEXT,
    published   TEXT,
    fetched_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    score       SMALLINT                   -- 0-10, NULL пока не оценено
);

-- Индекс для быстрой выборки статей за неделю
CREATE INDEX IF NOT EXISTS research_items_fetched_at_idx
    ON research_items (fetched_at DESC);

-- Индекс для фильтрации по score
CREATE INDEX IF NOT EXISTS research_items_score_idx
    ON research_items (score DESC)
    WHERE score IS NOT NULL;
