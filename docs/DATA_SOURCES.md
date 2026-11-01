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

## 2. Проверка доступности (2026-11-01, эта облачная сессия)

| Хост | Результат |
|---|---|
| ethereum-rpc.publicnode.com, eth.llamarpc.com, rpc.ankr.com, cloudflare-eth.com, eth.drpc.org, 1rpc.io | **403 — заблокировано политикой сети окружения** |
| gateway.thegraph.com, api.etherscan.io, api.dune.com, api.coingecko.com, docs.uniswap.org, Alchemy, Infura, Hypersync | недоступно (заблокировано) |
| bigquery.googleapis.com | доступен, но нет учётных данных GCP |
| api.anthropic.com | доступен (нужен `ANTHROPIC_API_KEY`) |
| github / raw.githubusercontent, PyPI, npm | доступны |

**Вывод:** в текущем окружении реальные данные блокчейна загрузить нельзя.
Код этапа 2 будет написан и покрыт тестами, но реальная загрузка требует одного из:

1. **Рекомендуется:** разрешить в настройках окружения домен archive-RPC
   (Alchemy / Infura / QuickNode / собственный узел) и задать `ETH_RPC_URL`.
   Archive нужен для контрольных точек; логи можно брать и с full-узла.
2. Разрешить публичный RPC (`ethereum-rpc.publicnode.com`, `eth.drpc.org`) —
   хватит для логов, но без archive-сверок и с жёсткими лимитами `eth_getLogs`.
3. BigQuery `bigquery-public-data.crypto_ethereum.logs` (нужен GCP-проект) —
   быстрый массовый бэкфилл, затем догрузка через RPC.

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
