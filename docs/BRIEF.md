# Бриф проекта: yadirect-agent

> Контекст для продолжения работы в Claude Code. Читается за 2 минуты.

## Что это

Автономный AI-агент для управления рекламой в Яндекс.Директе и аналитикой
в Яндекс.Метрике. Агент умеет (или будет уметь):

- Собирать семантическое ядро (Wordstat + кластеризация)
- Создавать и редактировать кампании, группы, объявления
- Управлять ставками и бюджетами
- Запускать и анализировать A/B-тесты объявлений
- Читать отчёты из Метрики и реагировать на метрики

Агент должен работать в **двух режимах**:
1. **CLI-агент** — запускается по расписанию (cron), сам принимает решения.
2. **MCP-сервер** — интерактивный доступ из Claude Desktop / Claude Code.

Общее ядро (API-клиент, модели, доменные сервисы) используется обоими.

## Ключевые архитектурные решения

- **Язык**: Python 3.11+, async/await везде.
- **Стек**: httpx, pydantic v2, pydantic-settings, tenacity, structlog,
  anthropic SDK, mcp SDK, typer, pytest, ruff, mypy strict.
- **Слои**:
  1. `clients/` — тонкие HTTP-клиенты (Direct, Metrika, Wordstat).
  2. `models/` — Pydantic-модели ресурсов API.
  3. `services/` — доменная бизнес-логика (campaigns, bidding, semantics...).
  4. `agent/` — цикл агента, tool-definitions, safety-политики.
  5. `mcp_server/` — адаптер к MCP.
  6. `cli/` — typer-команды для человека и для cron.
- **Безопасность операций** (plan → confirm → execute):
  любое действие, меняющее структуру или тратящее деньги, сначала
  формируется как план, валидируется safety-политикой, и только потом
  выполняется. Для CLI-агента — с авто-подтверждением по policy file.
- **Песочница Директа по умолчанию** (`YANDEX_USE_SANDBOX=true`).
- **Аудит-лог** в JSONL (`logs/audit.jsonl`) — все действия, которые
  агент выполнил, с trace_id.
- **Rate limit aware**: парсим заголовок `Units` у Direct, бэкаемся когда
  остаётся < 10% дневных поинтов.

## Что уже сделано (scaffold)

Я начал в чате и выложил файлы в архиве. Готово:

- `pyproject.toml` — зависимости, ruff/mypy/pytest конфиги.
- `.env.example`, `.gitignore`.
- `src/yadirect_agent/config.py` — Settings на pydantic-settings.
- `src/yadirect_agent/logging.py` — structlog.
- `src/yadirect_agent/exceptions.py` — типизированная иерархия ошибок.
- `src/yadirect_agent/clients/base.py` — базовый HTTP-клиент для Direct v5
  с retry, классификацией ошибок, парсингом Units. **Это ключевой файл.**
- `src/yadirect_agent/clients/direct.py` — высокоуровневый сервис-фасад
  (campaigns, adgroups, ads, keywords, reports).
- `src/yadirect_agent/clients/metrika.py` — заглушка (только get_counters).
- `src/yadirect_agent/clients/wordstat.py` — Protocol + заглушка
  `DirectKeywordsResearch` на `keywordsresearch.hasSearchVolume`.
- `src/yadirect_agent/models/` — Campaign, DailyBudget, Keyword, KeywordBid.
- `src/yadirect_agent/services/campaigns.py` — list/pause/resume/budget.
- `src/yadirect_agent/services/bidding.py` — apply bid updates (с TODO
  про MAX_INCREASE_PCT).
- `src/yadirect_agent/services/semantics.py` — normalize + naive cluster.

## Что НЕ сделано (TODO для Claude Code)

См. ТЗ в `TECHNICAL_SPEC.md`. Перед реализацией каждого milestone —
обязательно смотри `PRIOR_ART.md`: там расписано, какие репозитории и
статьи изучить/заимствовать под конкретную задачу. Коротко:

- Agent loop с tool use через Anthropic SDK.
- Safety layer (policy.yml, plan-confirm-execute).
- CLI на typer (команды: `run`, `mcp`, `plan`, `apply-plan`).
- MCP-сервер с нормальным набором инструментов.
- Semantics: реальный Wordstat-провайдер.
- Metrika: get_goals, get_report, conversion_by_source.
- Ads service: создание текстовых объявлений, модерация.
- A/B testing service.
- Тесты (unit + VCR для интеграционных).
- CI (GitHub Actions: ruff, mypy, pytest).
- README, LICENSE, Makefile, pre-commit.

## Как запускать

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
# заполнить .env
pytest
```

## Важное

- Пока **всё в песочнице** — `YANDEX_USE_SANDBOX=true`. Переключение на
  продакшн — только после ручной проверки первых сценариев.
- Anthropic-модель по умолчанию — `claude-opus-4-7` для планирования,
  возможно `claude-sonnet-4-6` для дешёвых вспомогательных вызовов.
- Токены **никогда** не логируются и не коммитятся — `SecretStr` + .env в
  `.gitignore`.
- API Директа возвращает HTTP 200 даже при логических ошибках — проверяем
  `"error"` в теле ответа. Это уже учтено в `clients/base.py`.
