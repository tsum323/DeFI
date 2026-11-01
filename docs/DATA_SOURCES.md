# Источники данных

## 1. Что нужно и откуда

| Данные | Первичный источник | Метод |
|---|---|---|
| Swap / Mint / Burn / Flash / Collect / Initialize / SetFeeProtocol | логи пула | `eth_getLogs` по адресу пула, окнами блоков |
| Время блока, `baseFeePerGas` | заголовки блоков | `eth_getBlockByNumber(false)` |
| Контрольные точки состояния (feeGrowthGlobal, ticks, liquidity, slot0) | контракт пула | `eth_call` на историческом блоке (**нужен archive-узел**) |
| Калибровка газа mint/burn/collect | реальные транзакции NonfungiblePositionManager | `eth_getTransactionReceipt.gasUsed` |
| Распределение ликвидности по тикам | реконструкция из Mint/Burn | собственный replay (см. MATH_CORE §5) |
| Объёмы, tick crossings | производные от replay | — |
| Перекрёстная проверка | Uniswap v3 subgraph (The Graph) | GraphQL; **не источник истины** |

OHLCV-свечи и агрегаты субграфа (`poolDayData.feesUSD`) **не используются** для
расчёта комиссий позиции — только для сверки.

### Топики событий (keccak256 сигнатур, проверено)
- `Swap(address,address,int256,int256,uint160,uint128,int24)` →
  `0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67`
- Остальные топики вычисляются кодом из сигнатур `IUniswapV3PoolEvents.sol` и
  покрываются тестом на этапе 2.

## 2. Проверка доступности

### 2026-11-01, этап 2 (после открытия сети и замены ключа)
| Endpoint | Результат `check_data_sources` |
|---|---|
| `alchemy` (`ETH_RPC_URL`) | ключ рабочий (`chainId=1`), но **Free tier: `eth_getLogs` не больше 10 блоков** за запрос (HTTP 400) → `LIMITED`. Историю за месяцы так загружать непрактично — нужен тариф PAYG |
| `publicnode` | `OK`; `eth_getLogs` до ~5000 блоков работает, но **не архивный**: запросы старше ~2–3 дней → HTTP 403 «Archive requests require a personal token» |
| `drpc` | `LIMITED`: free plan пропускает `eth_getLogs` только ~100 блоков (сообщение ошибки говорит «10000», фактически отказ уже на 150) |

Найдено и исправлено в `rpc.py`:
- публичные RPC за Cloudflare отвечали 403 (error code 1010) на стандартный `User-Agent: Python-urllib` → клиент шлёт свой `User-Agent`;
- JSON-ошибки в HTTP 4xx (Alchemy, dRPC) раньше превращались в «endpoint недоступен»; теперь тело разбирается, и «слишком большое окно» становится `RangeTooLarge`, так что синхронизация сама уменьшает окно.

### 2026-11-01, этап 2 (до открытия сети)
| Endpoint | Результат |
|---|---|
| `alchemy` (`ETH_RPC_URL`, хост eth-mainnet.g.alchemy.com) | **хост доступен**, но значение переменной — незаполненный шаблон (в месте ключа кириллица). `RpcClient` распознаёт это и сообщает `MISCONFIGURED`, URL не выводится |
| публичный demo-ключ Alchemy (разовая проверка, в конфиг не включён) | отвечает эпизодически, в основном HTTP 429 |
| publicnode, drpc | по-прежнему 403 — политика сети окружения |

Что успели подтвердить через demo-ключ: `head = 26319563`;
`Factory.getPool(WBTC, USDC, 500) = 0x9a772018FbD77fcD2d25657e5C547BAfF3Fd7D16` — совпадает с CREATE2.
Остальное (0.30%, блоки создания, активность) ждёт рабочего ключа.

### 2026-11-01, этап 1
Все публичные RPC и индексаторы были заблокированы (403); BigQuery доступен без учётных данных GCP.

Проверка повторяется командой `python -m scripts.check_data_sources`.

## 3. Проверенные адреса (Ethereum mainnet)

| Объект | Адрес | Как проверено |
|---|---|---|
| UniswapV3Factory | `0x1F98431c8aD98523631AE4a59f267346ea31F984` | официальный деплой; используется в CREATE2-векторе, дающем известный пул WETH/USDC 0.05% |
| NonfungiblePositionManager | `0xC36442b4a4522E871399CD717aBDD847Ab11FE88` | официальный деплой (только чтение) |
| POOL_INIT_CODE_HASH | `0xe34f…8b54` | `v3-periphery/contracts/libraries/PoolAddress.sol` |
| WBTC (8 dec) | `0x2260FAC5E5542a773Aa44fBCfeDf7C193bc2C599` | — |
| USDC (6 dec) | `0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48` | — |
| WBTC/USDC 0.30% | `0x99ac8cA7087fA4A2A1FB6357269965A2014ABc35` | CREATE2 (тест `test_pool_addresses_match_create2`) |
| WBTC/USDC 0.05% | `0x9a772018FbD77fcD2d25657e5C547BAfF3Fd7D16` | CREATE2 |
| WETH/USDC 0.05% (расширение) | `0x88e6A0c2dDD26FEEb64F039a2c41296FcB3f5640` | CREATE2, совпадает с широко известным адресом |

CREATE2 доказывает, *каким был бы* адрес пула; факт существования, блок создания
и текущая активность подтверждаются на этапе 2 через `Factory.getPool` и
событие `PoolCreated` (статус в `config/pools.yaml` → `verified_onchain`).
Выбор основного fee tier (0.30% или 0.05%) — по фактической активности на этапе 2.

## 4. Стратегия загрузки (этап 2)

- Окна `eth_getLogs` адаптивные: старт 5000 блоков, деление пополам при ошибках
  «too many results / response size», минимум 50.
- Загружаются только блоки с `≥ 64` подтверждениями (защита от реоргов).
- Сырые логи хранятся как есть (`raw_logs`, ключ `(chain_id, block_number, log_index)` —
  естественная дедупликация), затем декодируются в типизированные таблицы.
- `sync_state` хранит последний полностью загруженный блок по каждому
  (пулу, типу события); `sync_gaps` — пропущенные/упавшие окна для повторной загрузки.
- Целостность: непрерывность покрытия окон; совпадение `blockHash` логов с
  заголовком блока; инварианты replay (MATH_CORE §5); выборочная сверка с субграфом.
- Каждая запись хранит `source` (имя endpoint) — журнал происхождения данных.

## 5. Конвейер этапа 2 (команды)

```bash
python -m scripts.data_snapshot restore  # в новой сессии: поднять базу из data_snapshots/ (~2 мин)
python -m scripts.check_data_sources     # доступность endpoint'ов
python -m scripts.verify_pools           # getPool, token0/1, fee, tickSpacing, PoolCreated → data/pool_verification.json
python -m scripts.compare_fee_tiers      # 30 дней: swaps, объём, комиссии LP, TVL, fee/TVL → data/fee_tier_comparison.json
python -m scripts.sync_pool --endpoint drpc   # история событий primary_pool в БД (возобновляемо, бесплатно)
python -m scripts.data_snapshot export   # сохранить базу в data_snapshots/ для git
python -m scripts.calibrate_gas          # газ по реальным receipts → data/gas_calibration.json
python -m scripts.export_fixtures        # реальные логи для тестов → tests/fixtures/
python -m scripts.fetch_eth_usd          # дневной ETH/USD (Chainlink) для газа → data/eth_usd_daily.json
python -m scripts.validate_math_history  # этап 3: формулы против всей истории
python -m scripts.verify_replay_checkpoints  # этап 4: состояние пула против eth_call
python -m scripts.validate_fee_meter     # этап 4: комиссии против реальных позиций
python -m scripts.backtest --from 2025-01-01 --to 2026-11-01 --width 0.10 --rebalance on_exit
python -m scripts.run_grid --from 2025-01-01 --to 2026-11-01   # этап 5: массовый тест сетки стратегий
```

**Правило выбора основного fee tier:** основным становится пул с большей выручкой LP
(объём × fee) в USDC за последние 30 дней, при условии ≥ 1000 свопов в окне.
Если разница меньше 20%, вариант помечается `close_call` и решается вручную.
