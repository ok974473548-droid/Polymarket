-- Схема WhaleFollow. Переносимая: одинаково работает в SQLite (разработка, тесты)
-- и PostgreSQL (прод). Время — целые секунды UTC (epoch), JSON — в TEXT.
-- Для прода: партиционировать orderbook_snapshots и price_history по времени,
-- горячие данные 90 дней (раздел 4, правило 7 ТЗ).

CREATE TABLE IF NOT EXISTS events (
  event_id TEXT PRIMARY KEY, title TEXT, category TEXT, start_ts INTEGER, end_ts INTEGER
);
CREATE TABLE IF NOT EXISTS markets (
  market_id TEXT PRIMARY KEY,          -- condition_id
  event_id TEXT, question TEXT, category TEXT, end_ts INTEGER,
  status TEXT,                         -- open | closed | resolved | disputed
  resolved_ts INTEGER, liquidity_usd REAL, volume_usd REAL,
  neg_risk INTEGER, fee_bps REAL, rules TEXT, updated_ts INTEGER
);
CREATE TABLE IF NOT EXISTS tokens (
  token_id TEXT PRIMARY KEY, market_id TEXT, outcome_label TEXT,
  payout REAL                          -- 1 / 0 после разрешения, иначе NULL
);
CREATE TABLE IF NOT EXISTS trades (
  trade_id TEXT PRIMARY KEY,
  ts INTEGER NOT NULL, ts_fetched INTEGER NOT NULL,
  wallet TEXT NOT NULL, token_id TEXT NOT NULL, market_id TEXT,
  side TEXT NOT NULL,                  -- BUY | SELL
  price REAL NOT NULL, size_shares REAL NOT NULL, size_usd REAL NOT NULL,
  is_maker INTEGER, tx_hash TEXT
);
CREATE INDEX IF NOT EXISTS ix_trades_wallet_ts ON trades (wallet, ts);
CREATE INDEX IF NOT EXISTS ix_trades_token_ts ON trades (token_id, ts);

CREATE TABLE IF NOT EXISTS wallets (
  wallet TEXT PRIMARY KEY, owner_eoa TEXT, is_proxy INTEGER,
  first_seen_ts INTEGER, first_funded_ts INTEGER, funder_wallet TEXT
);
CREATE TABLE IF NOT EXISTS transfers (           -- переводы USDC (из блокчейна) для антисибила
  transfer_id TEXT PRIMARY KEY, ts INTEGER, from_wallet TEXT, to_wallet TEXT, amount REAL
);
CREATE TABLE IF NOT EXISTS wallet_sync (            -- чья полная история уже загружена (поиск кандидатов)
  wallet TEXT PRIMARY KEY, full_loaded_ts INTEGER, feed_volume_usd REAL
);
CREATE TABLE IF NOT EXISTS chain_sync (             -- для каких кошельков загружены переводы из блокчейна
  wallet TEXT PRIMARY KEY, fetched_ts INTEGER, error TEXT
);
CREATE TABLE IF NOT EXISTS entity_wallets (
  wallet TEXT PRIMARY KEY, entity_id TEXT NOT NULL, link_reason TEXT, confidence REAL
);
CREATE TABLE IF NOT EXISTS wallet_links (
  wallet_a TEXT, wallet_b TEXT, link_type TEXT, strength REAL, evidence TEXT, computed_at INTEGER,
  PRIMARY KEY (wallet_a, wallet_b, link_type)
);
CREATE TABLE IF NOT EXISTS positions (
  position_id TEXT PRIMARY KEY,
  wallet TEXT, entity_id TEXT, token_id TEXT, market_id TEXT, event_id TEXT, category TEXT,
  opened_ts INTEGER, closed_ts INTEGER,
  shares_bought REAL, cost_usd REAL, avg_entry REAL,
  shares_sold REAL, proceeds_usd REAL, remaining_shares REAL,
  exit_value_per_share REAL,           -- R: 1/0 при разрешении или VWAP продажи
  pnl_usd REAL, held_to_resolution INTEGER, n_trades INTEGER,
  status TEXT                          -- open | closed | resolved
);
CREATE INDEX IF NOT EXISTS ix_positions_entity ON positions (entity_id);

CREATE TABLE IF NOT EXISTS orderbook_snapshots (
  token_id TEXT NOT NULL, ts INTEGER NOT NULL, bids TEXT, asks TEXT, trigger TEXT,
  PRIMARY KEY (token_id, ts)
);
CREATE TABLE IF NOT EXISTS price_history (
  token_id TEXT NOT NULL, ts INTEGER NOT NULL, price REAL NOT NULL,
  PRIMARY KEY (token_id, ts)
);

CREATE TABLE IF NOT EXISTS wallet_scores (
  entity_id TEXT, computed_at INTEGER, window_days INTEGER,
  n_clusters INTEGER, total_cost REAL, history_days REAL, last_trade_ts INTEGER,
  edge_raw REAL, edge_shrunk REAL, edge_ci_lo REAL, edge_ci_hi REAL,
  clv_1h REAL, clv_24h REAL, clv_ci_lo REAL,
  pvalue REAL, qvalue REAL, edge_half1 REAL, edge_half2 REAL, decay_ratio REAL,
  hold_share REAL, median_hold_h REAL, return_90d REAL,
  class TEXT, status TEXT, score REAL, metrics TEXT,
  PRIMARY KEY (entity_id, computed_at)
);
CREATE TABLE IF NOT EXISTS wallet_category_scores (
  entity_id TEXT, category TEXT, computed_at INTEGER,
  n_clusters INTEGER, edge_shrunk REAL, edge_ci_lo REAL, status TEXT,
  PRIMARY KEY (entity_id, category, computed_at)
);
CREATE TABLE IF NOT EXISTS wallet_flags (
  entity_id TEXT, flag TEXT, value REAL, computed_at INTEGER,
  PRIMARY KEY (entity_id, flag, computed_at)
);
CREATE TABLE IF NOT EXISTS watchlist (            -- киты, добавленные системой или пользователем
  entity_id TEXT PRIMARY KEY, mode TEXT NOT NULL,  -- copy | watch | off
  added_by TEXT, added_ts INTEGER, categories_off TEXT
);

CREATE TABLE IF NOT EXISTS news_items (
  item_id TEXT PRIMARY KEY, market_id TEXT, ts_pub INTEGER, ts_fetched INTEGER,
  source TEXT, source_type TEXT, source_weight REAL, url TEXT, title TEXT, text_hash TEXT
);
CREATE TABLE IF NOT EXISTS forecasts (              -- вероятности с других рынков прогнозов (Manifold и др.)
  forecast_id TEXT PRIMARY KEY, market_id TEXT, ts_fetched INTEGER, source TEXT,
  title TEXT, url TEXT, probability REAL, resolved INTEGER
);
CREATE TABLE IF NOT EXISTS source_stats (
  source TEXT PRIMARY KEY, source_type TEXT, n INTEGER, hits INTEGER, weight REAL
);
CREATE TABLE IF NOT EXISTS signals (
  signal_id TEXT PRIMARY KEY, ts INTEGER, entity_id TEXT, wallet TEXT, trade_id TEXT,
  token_id TEXT, market_id TEXT, whale_price REAL, whale_size_usd REAL,
  kind TEXT,                           -- entry | whale_exit
  status TEXT,                         -- new | skipped | pending | executed | expired | rejected
  expires_ts INTEGER
);
CREATE TABLE IF NOT EXISTS llm_estimates (
  est_id TEXT PRIMARY KEY, signal_id TEXT, market_id TEXT, ts INTEGER,
  q REAL, confidence TEXT, base_rate REAL, evidence TEXT, blind INTEGER, model_version TEXT
);
CREATE TABLE IF NOT EXISTS decisions (
  signal_id TEXT PRIMARY KEY, decided_ts INTEGER,
  ask_now REAL, vwap_exec REAL, q_llm REAL, q_whale REAL, q_final REAL,
  edge_net REAL, kelly_frac REAL, size_usd REAL,
  decision TEXT,                       -- BUY | SKIP
  reasons TEXT, params_version TEXT, snapshot_ref TEXT, user_action TEXT
);
CREATE TABLE IF NOT EXISTS orders (
  order_id TEXT PRIMARY KEY, signal_id TEXT, ts INTEGER, token_id TEXT, side TEXT,
  limit_price REAL, size_usd REAL, status TEXT, is_paper INTEGER
);
CREATE TABLE IF NOT EXISTS fills (
  fill_id TEXT PRIMARY KEY, order_id TEXT, ts INTEGER, token_id TEXT, side TEXT,
  price REAL, size_shares REAL, fee_usd REAL
);
CREATE TABLE IF NOT EXISTS calibration_log (
  est_id TEXT PRIMARY KEY, market_id TEXT, q REAL, p_market REAL, y INTEGER, brier REAL, brier_market REAL
);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS risk_state (key TEXT PRIMARY KEY, value TEXT, updated_ts INTEGER);
CREATE TABLE IF NOT EXISTS notifications (
  notif_id TEXT PRIMARY KEY, ts INTEGER, kind TEXT, title TEXT, body TEXT, ref TEXT, is_read INTEGER
);
CREATE TABLE IF NOT EXISTS job_runs (
  run_id TEXT PRIMARY KEY, job TEXT, started_at INTEGER, finished_at INTEGER,
  status TEXT, n_rows INTEGER, error TEXT
);

-- Индексы под частые запросы экранов, агента и ночного пересчёта (v0.7)
CREATE INDEX IF NOT EXISTS ix_trades_market ON trades (market_id);
CREATE INDEX IF NOT EXISTS ix_trades_fetched ON trades (ts_fetched);
CREATE INDEX IF NOT EXISTS ix_tokens_market ON tokens (market_id);
CREATE INDEX IF NOT EXISTS ix_markets_status_end ON markets (status, end_ts);
CREATE INDEX IF NOT EXISTS ix_positions_wallet ON positions (wallet);
CREATE INDEX IF NOT EXISTS ix_positions_token ON positions (token_id);
CREATE INDEX IF NOT EXISTS ix_entity_wallets_entity ON entity_wallets (entity_id);
CREATE INDEX IF NOT EXISTS ix_transfers_from ON transfers (from_wallet);
CREATE INDEX IF NOT EXISTS ix_transfers_to ON transfers (to_wallet, ts);
CREATE INDEX IF NOT EXISTS ix_signals_status ON signals (status, kind, ts);
CREATE INDEX IF NOT EXISTS ix_signals_entity ON signals (entity_id, ts);
CREATE INDEX IF NOT EXISTS ix_signals_token ON signals (token_id, kind);
CREATE INDEX IF NOT EXISTS ix_news_market ON news_items (market_id, ts_pub);
CREATE INDEX IF NOT EXISTS ix_forecasts_market ON forecasts (market_id, ts_fetched);
CREATE INDEX IF NOT EXISTS ix_llm_signal ON llm_estimates (signal_id);
CREATE INDEX IF NOT EXISTS ix_llm_market ON llm_estimates (market_id, ts);
CREATE INDEX IF NOT EXISTS ix_orders_signal ON orders (signal_id);
CREATE INDEX IF NOT EXISTS ix_fills_token ON fills (token_id, ts);
CREATE INDEX IF NOT EXISTS ix_notifications_unread ON notifications (is_read, ts);
CREATE INDEX IF NOT EXISTS ix_job_runs_job ON job_runs (job, started_at);
CREATE INDEX IF NOT EXISTS ix_wallet_scores_status ON wallet_scores (status, computed_at)
;

-- v0.8: журнал операций кошелька (split/merge/conversion), сверка с Polymarket, рынки других площадок
CREATE TABLE IF NOT EXISTS activities (
  activity_id TEXT PRIMARY KEY, wallet TEXT NOT NULL, ts INTEGER NOT NULL, type TEXT NOT NULL,
  market_id TEXT, token_id TEXT, outcome_index INTEGER, size REAL, usdc_size REAL, price REAL, tx_hash TEXT
);
CREATE INDEX IF NOT EXISTS ix_activities_wallet ON activities (wallet, ts);
CREATE TABLE IF NOT EXISTS activity_sync (wallet TEXT PRIMARY KEY, fetched_ts INTEGER, last_ts INTEGER, error TEXT);
CREATE TABLE IF NOT EXISTS wallet_reconcile (
  entity_id TEXT, computed_at INTEGER,
  n_pm INTEGER, n_ours INTEGER, n_matched INTEGER,
  pnl_pm REAL, pnl_ours REAL, diff_usd REAL, diff_pct REAL,
  verdict TEXT,                        -- ok | warn | bad | no_data
  details TEXT,
  PRIMARY KEY (entity_id, computed_at)
);
CREATE TABLE IF NOT EXISTS ext_markets (
  source TEXT, ext_id TEXT, title TEXT, subtitle TEXT, probability REAL, close_ts INTEGER, url TEXT,
  extra TEXT, updated_ts INTEGER,
  PRIMARY KEY (source, ext_id)
);
CREATE INDEX IF NOT EXISTS ix_trades_source ON trades (source);

-- v0.11: сделки из потока и из опроса сводятся по хешу транзакции (защита от двойного сигнала)
CREATE INDEX IF NOT EXISTS ix_trades_tx ON trades (tx_hash)
