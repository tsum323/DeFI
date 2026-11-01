# DeFi Liquidity Lab

Автономная исследовательская лаборатория стратегий ликвидности Uniswap v3
(основной объект: WBTC/USDC, Ethereum).

> **Только исследования.** Проект читает публичные данные блокчейна и считает
> виртуальные позиции. Он не подключает кошельки, не хранит ключей, не
> подписывает и не отправляет транзакции — это обеспечено архитектурно
> (`backend/core/safety.py`, `tests/test_safety.py`).

## Статус
Этап 1 завершён. Этап 2 (Data Engine): код и тесты готовы, реальная загрузка ждёт
рабочего ключа RPC — см. [ROADMAP.md](ROADMAP.md).

## Документация
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — модули, поток данных, схема БД, безопасность
- [docs/MATH_CORE.md](docs/MATH_CORE.md) — формулы v3 и воспроизведение комиссий
- [docs/DATA_SOURCES.md](docs/DATA_SOURCES.md) — источники данных, адреса, доступность
- [docs/RISKS.md](docs/RISKS.md) — риски и ограничения модели

## Быстрый старт
```bash
pip install -r requirements.txt
python -m pytest                         # тесты конфигурации и безопасности
python -m scripts.check_data_sources     # проверка доступности RPC
python -m scripts.verify_pools           # подтверждение пулов on-chain
python -m scripts.compare_fee_tiers      # выбор основного fee tier
python -m scripts.sync_pool              # загрузка истории событий пула
```
Для реальных данных: `export ETH_RPC_URL=<archive RPC URL>`.

## Структура
```
backend/   core, data_engine, backtesting, strategies, optimizer, validation,
           analytics, ai_researcher, signal_engine, virtual_portfolio, database, api
frontend/  React + TypeScript (этап 10)
config/    networks.yaml, pools.yaml, lab.yaml
docs/  tests/  scripts/  data/  experiments/
```
