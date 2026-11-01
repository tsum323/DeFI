# DeFi Liquidity Lab

Автономная исследовательская лаборатория стратегий ликвидности Uniswap v3
(основной объект: WBTC/USDC, Ethereum).

> **Только исследования.** Проект читает публичные данные блокчейна и считает
> виртуальные позиции. Он не подключает кошельки, не хранит ключей, не
> подписывает и не отправляет транзакции — это обеспечено архитектурно
> (`backend/core/safety.py`, `tests/test_safety.py`).

## Статус
Все 11 этапов ТЗ выполнены; итоговая проверка — [docs/FINAL_REPORT.md](docs/FINAL_REPORT.md),
ход работ и ограничения — [ROADMAP.md](ROADMAP.md). Итог исследования (модельные оценки):
устойчивого преимущества активных стратегий над пассивной широкой позицией после всех расходов
не найдено; система выдаёт WAIT, кандидаты проходят прямое тестирование на новых данных.

## Документация
- [docs/SPEC.md](docs/SPEC.md) — полное ТЗ заказчика
- [CLAUDE.md](CLAUDE.md) — правила работы для каждой сессии
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — модули, поток данных, схема БД, безопасность
- [docs/MATH_CORE.md](docs/MATH_CORE.md) — формулы v3 и воспроизведение комиссий
- [docs/DATA_SOURCES.md](docs/DATA_SOURCES.md) — источники данных, адреса, доступность
- [docs/RISKS.md](docs/RISKS.md) — риски и ограничения модели

## Быстрый старт
```bash
pip install -r requirements.txt
python -m scripts.data_snapshot restore            # данные пула из архива (~2 мин)
python -m scripts.data_snapshot restore-research   # эксперименты, сигналы, портфель, Optuna
python -m scripts.evaluate_market --sync           # свежие блоки → анализ рынка → сигнал
python -m scripts.forward_test                     # прямые тесты кандидатов
python -m scripts.final_check                      # финальная проверка → docs/FINAL_REPORT.md
(cd frontend && npm ci && npm run build) && python -m scripts.dashboard   # панель :8000
python -m scripts.scheduler run                    # автоматический режим (SPEC §16)
```
Источники данных: бесплатный dRPC по умолчанию для загрузки; `ETH_RPC_URL` (Alchemy) — опционально.
Ключ `ANTHROPIC_API_KEY` нужен только для автономного AI-исследователя.

## Структура
```
backend/   core, data_engine, backtesting, strategies, optimizer, validation, analytics,
           ai_researcher, signal_engine, virtual_portfolio, automation, database, api
frontend/  React + TypeScript + Plotly (панель)
config/    networks.yaml, pools.yaml, lab.yaml
docs/  tests/  scripts/  data/  experiments/
```
