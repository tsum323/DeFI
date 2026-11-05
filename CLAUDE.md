# CLAUDE.md — правила работы над UNISWAP AI LAB (бывш. DeFi Liquidity Lab)

Читать в начале КАЖДОЙ сессии, до любых действий.

## Обязательно прочитать
1. `docs/SPEC.md` — полное ТЗ заказчика (источник требований, не сокращать и не менять).
2. `ROADMAP.md` — текущий этап и статус.
3. `docs/LAB.md` (новая архитектура), `docs/AUDIT.md`, `docs/ARCHITECTURE.md`, `docs/MATH_CORE.md`,
   `docs/DATA_SOURCES.md`, `docs/RISKS.md` — принятые решения.

## Язык
Общение с заказчиком и документация — на русском. Код и комментарии — на английском.
Заказчик работает с телефона и не программист: давать пошаговые инструкции, без жаргона.

## Жёсткие правила
- Только чтение блокчейна. Любой RPC-вызов — через `backend.core.safety.assert_read_only`.
  Никаких ключей кошельков, подписей, транзакций, approvals. `tests/test_safety.py` должен проходить.
- Никаких фиктивных или синтетических данных вместо реальных. Синтетика допускается только
  в тестах и помечается `SYNTHETIC`.
- Маркировка достоверности чисел: `EXACT` / `MODELED` / `SCENARIO` / `SYNTHETIC` (ARCHITECTURE §2).
- Ядро Uniswap v3 — целочисленная арифметика с округлениями как в Solidity (MATH_CORE).
- Нельзя использовать будущие данные в решениях прошлого (ARCHITECTURE §4).
- Не объявлять стратегию успешной по одной исторической доходности — только после OOS/walk-forward.
- Зависимости закрепляются версиями в `requirements.txt`; ставить только реально используемое.

## Процесс
- Этапы — строго по порядку из `docs/SPEC.md` §17. После каждого этапа: тесты → проверка →
  документация → обновить `ROADMAP.md` (включая известные ограничения) → коммит и push.
- Не переходить к следующему этапу при критических ошибках.
- Работа ведётся в ветке `stage-1-architecture` (или новой ветке этапа от неё), `git push -u origin <ветка>`.

- Сайт публикует собранную версию из `site/`. После любого изменения `frontend/src` собирать ТОЛЬКО так:
  `cd frontend && npx tsc -b && VITE_STATIC=1 npx vite build --base=./ --outDir dist-static`, затем заменить
  `site/index.html` и `site/assets/` содержимым `frontend/dist-static/` и закоммитить вместе с кодом
  (обычный `npm run build` даёт пути `/assets/...` и режим API — на GitHub Pages страница будет пустой).

## Окружение
- Доступ к данным: Alchemy, ключ в переменной окружения `ETH_RPC_URL`. Никогда не выводить
  её значение в чат, логи, коммиты.
- Проверка доступа: `python -m scripts.check_data_sources`.
- База не хранится между сессиями. В начале сессии:
  `python -m scripts.data_snapshot restore` (данные пула) и
  `python -m scripts.data_snapshot restore-research` (эксперименты, отчёты, гипотезы, сигналы,
  виртуальные позиции, прямые тесты, Optuna), затем `python -m scripts.evaluate_market --sync`
  и `python -m scripts.forward_test` (или весь цикл: `python -m scripts.scheduler once`). В конце сессии: `python -m scripts.data_snapshot export-research`
  и коммит `data_snapshots/research_db/`. Лимит Alchemy ограничен — для загрузки использовать dRPC.
- Ключ Claude API для AI Researcher (этап 7): `ANTHROPIC_API_KEY` (может быть ещё не задан).
- Распорядок UNISWAP AI LAB: `python -m scripts.lab_cycle` (сканер, портфель, обучение; раз в день —
  исследование и отчёт). Проверка резервной копии: `python -m scripts.verify_backup`.
