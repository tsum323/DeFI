# ROADMAP

Статусы: ✅ готово · 🟡 в работе · ⏳ запланировано · ⛔ заблокировано

## Этап 1 — Исследование и архитектура ✅ (2026-11-01)
- ✅ Изучена механика v3 по исходникам контрактов → `docs/MATH_CORE.md`
- ✅ Способ воспроизведения комиссий: событийный replay + ценово-целевые свопы + инварианты + границы A/B для гипотетической позиции
- ✅ Источники данных и проверка доступности → `docs/DATA_SOURCES.md`
- ✅ Адреса Factory/NPM/токенов/пулов WBTC-USDC; пулы проверены офлайн через CREATE2 (тест)
- ✅ Архитектура, схема БД, защита от look-ahead, оценка устойчивости → `docs/ARCHITECTURE.md`
- ✅ Риски и ограничения → `docs/RISKS.md`
- ✅ Каркас проекта, конфигурация (`config/*.yaml`) с валидацией
- ✅ Read-only guard + тесты безопасности
- ✅ Скрипт проверки источников `scripts/check_data_sources.py`
- ⛔ Реальная проверка RPC: все публичные endpoints заблокированы политикой сети окружения

**Известные ограничения этапа:** существование пулов on-chain и блок создания
Factory не подтверждены через RPC (только CREATE2-вывод).

## Этап 2 — Data Engine ✅
- ✅ RPC-клиент (`backend/data_engine/rpc.py`): allowlist на каждом вызове, ретраи/backoff,
  rate-limit, batch, классификация «слишком большое окно»; URL с ключом нигде не выводится;
  распознаётся незаполненный шаблон ключа
- ✅ ABI-декодер всех 9 событий пула + `PoolCreated` (`abi.py`), топики сверены с эталоном
- ✅ Схема БД (`backend/database/schema.py`): `pools, blocks, raw_logs, ev_*, sync_state,
  sync_gaps`; большие целые хранятся точно (TEXT)
- ✅ Синхронизация (`sync.py`): адаптивные окна, 64 подтверждения, проверка `blockHash`,
  непрерывное покрытие, `sync_gaps`, идемпотентность и возобновление; тесты на фейковой цепи
- ✅ Скрипты: `verify_pools`, `compare_fee_tiers`, `sync_pool`
- ✅ Подтверждение пулов (`verify_pools`, Alchemy): 0.30% создан в блоке 12376048, 0.05% — 12561607;
  адреса совпали с CREATE2, `factory_deploy_block 12369621` подтверждён
- ✅ Выбор основного fee tier (`compare_fee_tiers`, 30 дней): **wbtc_usdc_3000** — комиссии LP
  $302 866 против $2 635 у 0.05%; 5 702 swap (≥ 1000)
- ✅ Загрузка истории wbtc_usdc_3000: блоки 12376048..26320225 (май 2021 — 2026-11-01),
  **без пропусков** (`open_gaps = 0`): 717 865 логов — 584 395 Swap, 34 525 Mint, 50 983 Burn,
  46 424 Collect, 1 517 CollectProtocol, 15 Flash; 576 551 блок (время, base fee).
  Блоки до 26033547 — Alchemy (PAYG), остаток — бесплатный dRPC (окна по 100 блоков)
- ✅ Снимок данных в репозитории: `data_snapshots/wbtc_usdc_3000/` (66 МБ, 10 частей xz ≤ 8.5 МБ,
  sha256 в `manifest.json`). Восстановление `python -m scripts.data_snapshot restore` (~2 мин)
  проверено: все таблицы совпали с исходной базой
- ✅ Калибровка газа (`calibrate_gas`): 1 440 реальных receipts 2021–2026 (стратификация по годам),
  медианы gasUsed → `config/lab.yaml costs.gas_units` (MODELED): mint 436k (было 500k),
  increase 200k, decrease+collect 200k (было 250k), простой swap 127k (было 180k);
  p90 → `gas_units_p90` для тестов чувствительности. По годам медианы стабильны (±10%)
- ✅ Фикстуры реальных логов (`export_fixtures` → `tests/fixtures/real_pool_logs.json`, 42 лога
  всех 9 типов); тест проверяет tick ↔ sqrtPriceX96, кратность тиков 60, знаки сумм

**Известные ограничения:**
- Лимит Alchemy на текущий месяц исчерпан; дальнейшая работа — через бесплатный dRPC
  (`python -m scripts.sync_pool --endpoint drpc`). Загрузчик больше не запрашивает заголовок
  каждого блока: время — из `blockTimestamp` логов, base fee — `eth_feeHistory` (1024 блока/запрос),
  один контрольный заголовок на окно.
- База `data/lab.sqlite` (~1 ГБ) живёт только в контейнере; после новой сессии —
  `data_snapshot restore`, затем `sync_pool --endpoint drpc` догружает свежие блоки.
  Снимок обновлять командой `python -m scripts.data_snapshot export` (перезаписывает части).
- PublicNode бесплатно — не архив (только ~2–3 дня), `sync_pool` его не использует.
- Газ: средние величины; реальный газ mint выше, если тики позиции ещё не инициализированы
  (p90 ≈ 488k). Цена газа — base fee блока + приоритет `priority_fee_gwei` (предположение).
- Распределение ликвидности, tick crossings и fee growth не хранятся отдельно — они строятся
  replay'ем событий в этапах 3–4 (DATA_SOURCES §1).

## Этап 3 — Математическое ядро ⏳ (не зависит от сети — можно начинать параллельно)
- TickMath, FullMath, SqrtPriceMath, SwapMath, LiquidityAmounts — целочисленно
- Fee growth (inside/outside), Position.update
- Тест-векторы v3-core, property-based тесты

## Этап 4 — Backtesting Engine ⏳
- `pool_replay` с инвариантами по каждому Swap; snapshots
- `position_sim`: комиссии A/B, газ, перестановки, ребаланс-своп через пул
- Финансовые компоненты раздельно (IL, эффект цены, комиссии, расходы)

## Этап 5 — Strategy Generator ⏳
## Этап 6 — Optimization Engine + Validation ⏳
## Этап 7 — AI Researcher ⏳
## Этап 8 — Signal Engine ⏳
## Этап 9 — Virtual Portfolio ⏳
## Этап 10 — Dashboard ⏳
## Этап 11 — Финальное тестирование ⏳

(Детальное содержание этапов 5–11 — в постановке задачи и `docs/ARCHITECTURE.md`;
уточняется по мере продвижения.)
