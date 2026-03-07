-- Health OS — Supabase Schema
-- Run this in the Supabase SQL Editor (Dashboard → SQL Editor → New query)

-- ─── Recipes ──────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS recipes (
    id          BIGSERIAL PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    aliases     JSONB DEFAULT '[]'::jsonb,
    serving_g   NUMERIC,
    calories    INTEGER,
    protein     NUMERIC,
    fat         NUMERIC,
    carbs       NUMERIC,
    fiber       NUMERIC,
    glycemic_index INTEGER,
    ingredients TEXT,
    category    TEXT,
    estimated   BOOLEAN DEFAULT false,
    yield_pcs   INTEGER,
    added       DATE DEFAULT CURRENT_DATE,
    updated     DATE
);

-- ─── Daily Logs ───────────────────────────────────────────────────────────────
-- One row per day. meals and training are JSON arrays.

CREATE TABLE IF NOT EXISTS daily_logs (
    id             BIGSERIAL PRIMARY KEY,
    date           DATE NOT NULL UNIQUE,
    weight_morning NUMERIC,
    meals          JSONB DEFAULT '[]'::jsonb,
    training       JSONB DEFAULT '[]'::jsonb,
    sleep          JSONB,
    notes          TEXT DEFAULT '',
    updated_at     TIMESTAMPTZ DEFAULT NOW()
);

-- Auto-update updated_at on every write
CREATE OR REPLACE FUNCTION update_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER daily_logs_updated_at
    BEFORE UPDATE ON daily_logs
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();

-- ─── Oura Data ────────────────────────────────────────────────────────────────
-- Raw Oura API response per day (sleep, readiness, activity, stress, spo2).

CREATE TABLE IF NOT EXISTS oura_data (
    id         BIGSERIAL PRIMARY KEY,
    date       DATE NOT NULL UNIQUE,
    data       JSONB NOT NULL,
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TRIGGER oura_data_updated_at
    BEFORE UPDATE ON oura_data
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();
