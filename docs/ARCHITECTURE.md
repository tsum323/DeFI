# Архитектура DeFi Liquidity Lab

Исследовательская система. Только чтение блокчейна. Никаких кошельков,
подписей и транзакций (см. §6).

## 1. Модули и поток данных

```
            ┌──────────────┐  raw_logs, blocks   ┌──────────────────┐
 RPC/BQ ──▶ │ data_engine  │ ──────────────────▶ │  database (SQL)  │
 (read)     └──────────────┘                     └───────┬──────────┘
                                                         │ events, snapshots
                     ┌───────────────────────────────────▼───────────┐
                     │ backtesting: uniswap_math → pool_replay →     │
                     │ position_sim (hypothetical LP, costs, rebal)  │
                     └───────▲───────────────────────────┬───────────┘
          StrategySpec       │                           │ PositionResult
   ┌────────────┐   ┌────────┴─────┐   ┌──────────────┐  │
   │ strategies │──▶│  optimizer   │──▶│  validation  │◀─┘
   └────────────┘   └──────▲───────┘   └──────┬───────┘
                           │ hypotheses       │ robustness score
                    ┌──────┴────────┐  ┌──────▼──────┐  ┌───────────────────┐
                    │ ai_researcher │  │  analytics  │  │  signal_engine    │
                    │ (Claude via   │  └─────────────┘  │  (live regime →   │
                    │  adapter)     │                   │   validated strat)│
                    └───────────────┘                   └────────┬──────────┘
                                                                 ▼
                                api (FastAPI)  ◀──  virtual_portfolio
                                     ▲
                                frontend (React + TS + Plotly)
```

| Модуль | Ответственность | Ключевые интерфейсы |
|---|---|---|
| `core` | конфиг, адреса, read-only guard, логирование, общие типы | `load_config()`, `assert_read_only()` |
| `data_engine` | RPC-клиент (только allowlist), загрузка логов окнами, декодирование, sync/gaps, проверки целостности | `sync_pool(pool, to_block)` |
| `database` | SQLAlchemy-модели, миграции; SQLite → PostgreSQL без изменения кода | репозитории по сущностям |
| `backtesting` | `uniswap_math` (целочисленные формулы), `pool_replay` (состояние пула), `position_sim` (позиция, комиссии A/B, газ, перестановки) | `simulate(spec, dataset) -> PositionResult` |
| `strategies` | декларативные `StrategySpec`: правило диапазона, правило перестановки, фильтр режима; генератор пространства параметров | `StrategySpec`, `generate(space)` |
| `optimizer` | grid / random / Optuna (TPE = bayesian), GA опционально; дедупликация по хэшу (spec + dataset + code_version); возобновляемые study | `run_study(space, objective)` |
| `validation` | IS/OOS сплиты, walk-forward, режимные срезы, чувствительность (газ, объём ×0.5/×1.5, ±шаг параметров), бенчмарки HODL / пассивный full-range LP | `validate(spec) -> RobustnessReport` |
| `analytics` | метрики (ROI, MDD, Sharpe, Sortino, Calmar, time-in-range, …), классификатор рыночных режимов | `metrics(result)`, `classify_regime(window)` |
| `ai_researcher` | адаптер LLM (Claude по умолчанию), построение контекста из БД, генерация гипотез в строгой JSON-схеме → эксперименты; LLM **не считает** и не трогает ядро | `ResearchCycle.run()` |
| `signal_engine` | актуальное состояние пула → режим → выбор валидированной стратегии → диапазон/соотношение → сценарная оценка → фильтры → статус | `evaluate_market() -> Signal \| None` |
| `virtual_portfolio` | виртуальные позиции из OPEN CANDIDATE, ежечасная переоценка на реальных событиях; параметры неизменяемы | `open_from_signal()`, `mark_all()` |
| `api` | FastAPI: чтение результатов, управление задачами (start/stop/resume) | REST `/experiments`, `/signals`, … |
| `scheduler` (в `core`) | цикл исследования, журнал задач, лимит тяжёлых задач (`max_parallel_heavy_jobs`) | `job_runs` |

## 2. Уровни достоверности данных (сквозная маркировка)

Каждое число в БД и UI несёт метку:
- `EXACT` — восстановлено из событий и прошло инварианты/контрольные точки;
- `MODELED` — модельная оценка (комиссии гипотетической позиции, газ по ассумпциям);
- `SCENARIO` — прогноз (сигналы, сценарии цены);
- `SYNTHETIC` — синтетические данные (только в тестах; запрещены в рейтингах).

## 3. Схема БД (черновик, этап 2/6)

- **Данные:** `pools`, `blocks(number, hash, timestamp, base_fee)`,
  `raw_logs(chain_id, block, log_index, tx_hash, address, topics, data, source)`,
  `swaps`, `mints`, `burns`, `flashes`, `collects`, `fee_protocol_changes`,
  `sync_state`, `sync_gaps`, `state_checkpoints`, `pool_snapshots`.
- **Исследования:** `datasets(id, pool, from_block, to_block, data_hash)`,
  `strategies(id, name, spec_json, spec_hash)`,
  `experiments(id, strategy_id, dataset_id, params_json, code_version, status,
  rejection_reason, started_at, finished_at, dedup_key UNIQUE)`,
  `experiment_metrics`, `experiment_timeseries` (parquet-файл в `experiments/`),
  `validation_reports`, `optuna_*` (родные таблицы Optuna в той же БД).
- **AI:** `hypotheses(id, description, rationale, params_json, test_plan, status,
  result_summary, conclusion, confidence, created_by_model)`, `hypothesis_experiments`.
- **Сигналы:** `signals(id, created_at, pool, status, spec_id, lower_tick, upper_tick,
  ratio, expected_fees_lo/hi, expected_costs, il_scenarios_json, net_lo/hi,
  max_dd_hist, oos_result, confidence, reasons, invalidation_rules, valid_until)` —
  **append-only**; `signal_events` — история смены статусов.
- **Виртуальный портфель:** `virtual_positions` (неизменяемые параметры),
  `virtual_marks` (переоценки), `virtual_rebalances`.
- **Операции:** `job_runs(id, job, started, finished, status, log)`.

## 4. Защита от look-ahead

- Бэктест — событийный цикл по `(block, logIndex)`; стратегия получает только
  `MarketView`, построенный из событий `≤` текущего блока.
- Решение в блоке `b` исполняется не раньше `b+1`.
- Индикаторы (волатильность, объём, режим) — только по прошедшим окнам;
  тест «отравленного будущего»: изменение событий после `t` не меняет решений до `t`.
- Optuna видит только in-sample; OOS-окна раскрываются один раз при валидации.

## 5. Оценка устойчивости (черновик формулы)

`Robustness = w1·norm(OOS net vs HODL) + w2·(доля walk-forward окон с Net>0)
+ w3·(1 − норм. MDD) + w4·стабильность по соседним параметрам
+ w5·устойчивость к газу×2 и объёму×0.5 − штраф за малое число окон/перестановок`.
Стратегия, положительная только на одном отрезке, получает низкий балл.
Победитель выбирается по Robustness, а не по максимальному историческому ROI.

## 6. Безопасность (архитектурно)

- В проекте **нет** кода подписи, ключей, мнемоник, `eth_account`.
- Единственная точка выхода в сеть блокчейна — RPC-клиент `data_engine`,
  вызывающий `core.safety.assert_read_only` для каждого метода (закрытый allowlist).
- web3.py используется только с HTTP-провайдером без middleware аккаунтов.
- CI-тест `tests/test_safety.py` сканирует исходники на запрещённые паттерны.
- `lab.mode` может быть только `research_only` — иначе конфиг не загрузится.

## 7. Расширяемость

- Сети/пулы/токены — только конфиг (`config/*.yaml`).
- Протокол — интерфейс `PoolModel` (`apply_event`, `swap_step`, `fee_growth_inside`);
  v4 (singleton PoolManager, hooks, динамические комиссии) — отдельная реализация.
- LLM — интерфейс `LLMAdapter.complete(messages, schema)`.
