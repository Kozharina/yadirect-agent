# Техническое задание: yadirect-agent

> Документ для Claude Code. Написан как набор задач с приёмочными
> критериями. Задачи можно брать в любом порядке, но **M0 → M1 → M2 → M3**
> — это естественная последовательность для первого рабочего релиза.

## Общие правила

1. **Никаких заглушек в рабочем коде**. Если метод не готов — он
   вызывает `NotImplementedError` с понятным сообщением и `TODO(milestone)`
   в docstring.
2. **Каждая задача завершается тестом**. Минимум — unit на моках. Для
   HTTP-вызовов — `respx` (httpx mocking) или `pytest-vcr`.
3. **Ruff + mypy strict** должны проходить после каждой задачи.
4. **Никакой бизнес-логики в клиентах**. Клиент только делает HTTP и
   маппит типы. Логика — в `services/`.
5. **Все async**. Никаких блокирующих вызовов в main path.
6. **Сообщения ошибок и логи — на английском**. Комментарии и docstring
   — можно на русском или английском, единообразно внутри файла.
7. **Секреты**: только через `SecretStr`, никогда не в логи, никогда не
   в репорт-файлы.

---

## M0 — Инфраструктура репозитория

Нужно доделать, чтобы репо выглядел профессионально.

### M0.1 README.md

Разделы:
- Project tagline (1 строка) + бейджи (Python version, license, CI status).
- Quickstart (установка, настройка `.env`, первый запуск).
- Architecture diagram (можно в mermaid) — слои clients/services/agent.
- Safety model (коротко про plan → confirm → execute, sandbox, audit log).
- Roadmap (ссылка на этот файл).
- License.

**Приёмка**: `README.md` отрендерился на GitHub, ссылки рабочие.

### M0.2 LICENSE

MIT. Имя владельца берём из `git config user.name` или оставляем
`"yadirect-agent contributors"`.

### M0.3 Makefile

Команды:
- `make install` — `pip install -e ".[dev]"`
- `make test` — `pytest`
- `make lint` — `ruff check . && ruff format --check .`
- `make fix` — `ruff check --fix . && ruff format .`
- `make type` — `mypy src/`
- `make check` — `lint + type + test`
- `make run-cli ARGS="..."` — `yadirect-agent ${ARGS}`
- `make run-mcp` — `yadirect-mcp`

### M0.4 pre-commit

`.pre-commit-config.yaml` с хуками: ruff, ruff-format, mypy (на staged
файлах через mypy через pre-commit-mirrors).

**Приёмка**: `pre-commit run --all-files` проходит.

### M0.5 GitHub Actions CI

`.github/workflows/ci.yml`:
- Python 3.11 и 3.12 матрица.
- Steps: checkout → setup-python → `pip install -e ".[dev]"` → `make lint`
  → `make type` → `make test`.
- Кэш pip.

**Приёмка**: на пустом коммите зелёная галочка.

### M0.6 Issue / PR templates

`.github/ISSUE_TEMPLATE/bug.yml`, `feature.yml`, `.github/PULL_REQUEST_TEMPLATE.md`.

### M0.7 Первый коммит

`git init`, первый коммит — `chore: initial scaffold`.

---

## M1 — Agent loop (CLI)

Сердце проекта. Агент, который принимает задачу на естественном языке,
умеет вызывать инструменты, и ведёт многошаговый диалог с моделью.

### M1.1 Tool registry

`src/yadirect_agent/agent/tools.py`:

- Класс `Tool` с полями: `name`, `description`, `input_schema` (Pydantic
  модель), `handler` (async callable).
- Функция-декоратор `@tool` для регистрации.
- Набор базовых инструментов (обёртки над `services/`):
  - `list_campaigns(states: list[str] | None)` → summaries
  - `pause_campaigns(ids: list[int])` → audit event
  - `resume_campaigns(ids: list[int])`
  - `set_campaign_budget(campaign_id: int, budget_rub: int)`
  - `get_keywords(adgroup_ids: list[int])`
  - `set_keyword_bids(updates: list[BidUpdate])`
  - `validate_phrases(phrases: list[str])`

Описания (`description`) пишем **для LLM**: что делает инструмент, когда
его использовать, что возвращает, какие бывают ошибки. Короткие, но
конкретные.

### M1.2 Agent loop

`src/yadirect_agent/agent/loop.py`:

- Класс `Agent` с методом `run(user_message: str) -> AgentRun`.
- Под капотом — цикл `anthropic.messages.create(tools=...)` с обработкой
  `tool_use` блоков:
  1. Модель отвечает с `stop_reason == "tool_use"`.
  2. Мы выполняем все `tool_use` блоки (параллельно через
     `asyncio.gather`, если инструменты read-only — писать всегда
     последовательно).
  3. Отправляем `tool_result` блоки обратно.
  4. Повторяем до `stop_reason == "end_turn"` или до `max_iterations`.
- `AgentRun` содержит: финальный текст, список вызванных инструментов
  с их аргументами и результатами, общие токены, стоимость.
- Интеграция с structlog: каждый шаг логируется с `trace_id`.

### M1.3 System prompt

`src/yadirect_agent/agent/prompts.py`:

- Базовый system prompt объясняет: роль (PPC-специалист), safety-правила
  (никаких массовых изменений без plan-confirm, обязательно песочница
  для новых операций), формат ответа.
- Константы промптов экспортируются — чтобы их можно было A/B-тестить.

### M1.4 CLI entrypoint

`src/yadirect_agent/cli/main.py` на typer:

- `yadirect-agent run "задача текстом"` — одноразовый запуск.
- `yadirect-agent chat` — интерактивный REPL.
- `yadirect-agent list-campaigns [--state=ON]` — прямой вызов без
  модели (для отладки).
- `yadirect-agent --version`.

**Приёмка**: `yadirect-agent run "покажи все кампании в песочнице"` —
работает, выводит список, стоимость вызова внизу, пишет audit-запись.

---

## M2 — Safety layer

Без этого агента нельзя выпускать за пределы песочницы.

### M2.0 Kill-switches (обязательный минимум)

Источник: best-practice статья ["Agentic PPC Campaign Management"](https://www.digitalapplied.com/blog/agentic-ppc-campaign-management-autonomous-bid)
(см. `docs/PRIOR_ART.md`). Все семь должны работать до того, как агент
получит доступ к продакшн-кабинету:

1. **Budget caps** — жёсткий потолок дневных трат на уровне аккаунта и на
   уровне группы кампаний. Первая и самая важная проверка, потому что
   потраченный бюджет — единственное необратимое действие.
2. **Max CPC** — потолок стоимости клика per-campaign. Любая ставка
   выше — отклоняется.
3. **Negative keyword floor** — минимальный обязательный набор минус-слов
   (бесплатно, скачать, своими руками, отзывы, вакансии и т.п.). Если
   кампания запускается без них — запуск блокируется.
4. **Quality Score guardrail** — мониторим показатель качества по ключам.
   Если агент хочет поднять ставку на ключ с низким QS — предупреждение
   и требование подтверждения. QS защищается как **отдельная метрика**,
   а не оптимизируется — потеря QS обходится неделями восстановления.
5. **Budget balance** — пропорции бюджетов между кампаниями не меняются
   более чем на X% за сутки (конфигурируется). Защита от ситуации «агент
   слил всё в одну кампанию».
6. **Conversion integrity** — регулярная проверка: конверсии в Метрике
   приходят, их количество не упало резко, цели не сломались. Если
   трекинг подозрителен — все write-операции блокируются.
7. **Query drift detection** — раз в сутки сравниваем поисковые запросы
   с прошлой неделей. Если доля «новых» запросов > порога — алёрт и
   требование ревью (может означать, что Директ начал показывать
   рекламу не по той аудитории).

Каждый kill-switch реализуется как отдельный `Check` класс с методом
`check(plan) -> CheckResult`. Pipeline: план проходит через все проверки
последовательно; **любая** блокировка останавливает исполнение.

### M2.1 Policy схема

`src/yadirect_agent/agent/safety.py`:

- Pydantic-модель `Policy`:
  ```python
  class Policy(BaseModel):
      # --- Approval tiers ---
      auto_approve_readonly: bool = True
      auto_approve_pause: bool = True          # безопасно (всегда обратимо)
      auto_approve_resume: bool = False        # тратит деньги
      auto_approve_negative_keywords: bool = True  # всегда снижает трату

      # --- Thresholds (per single operation) ---
      max_daily_budget_change_pct: float = 0.2 # +/-20% без подтверждения
      max_bid_increase_pct: float = 0.5
      max_bid_change_per_day_pct: float = 0.25 # cumulative за сутки
      max_bulk_size: int = 50                  # >50 объектов = требует подтверждения

      # --- Hard limits (kill-switches) ---
      account_daily_budget_cap_rub: int         # обязательно
      campaign_max_cpc_rub: dict[int, float]    # per-campaign
      required_negative_keywords: list[str] = []
      min_quality_score_for_bid_increase: int = 5
      max_budget_balance_shift_pct_per_day: float = 0.3
      max_new_query_share: float = 0.4          # query drift alert

      # --- Forbidden always ---
      forbidden_operations: list[str] = [
          "delete_campaigns", "delete_ads", "archive_campaigns_bulk"
      ]
  ```
- YAML loader: `agent_policy.yml` в корне проекта.
- Дефолт: максимально строгий (auto_approve только read-only + pause).
- Policy **нельзя** переопределить из контекста модели — только через
  явный human-action (CLI-команда или прямой edit файла).

### M2.2 Plan → confirm → execute

- Класс `OperationPlan`: `action`, `resource`, `args`, `preview`,
  `requires_confirmation`, `reason`.
- Декоратор `@requires_plan` оборачивает сервис-метод:
  - Генерирует `OperationPlan`.
  - Пропускает через `Policy.check(plan)` — возвращает `Approved` или
    `NeedsConfirmation`.
  - Если `NeedsConfirmation` и режим interactive — спрашивает у человека.
  - Если batch-режим — пишет план в `pending_plans.jsonl` и выходит.
- CLI-команда `yadirect-agent apply-plan <plan_id>` — исполнить
  отложенный план.

### M2.3 Audit log

- `src/yadirect_agent/audit.py`:
  - `AuditEvent` (Pydantic): `ts`, `trace_id`, `actor` (agent|human),
    `action`, `resource`, `args`, `result`, `units_spent`.
  - Async writer — JSONL в `AUDIT_LOG_PATH`.
  - Sink-интерфейс — в будущем легко добавить Kafka/Postgres.
- Все сервисные методы пишут audit до и после (`*.requested` /
  `*.ok` / `*.failed`).

### M2.4 Daily budget guard

- Перед любой операцией, которая может поднять дневную трату (изменение
  бюджета, resume, set_bid), сервис суммирует текущие бюджеты активных
  кампаний.
- Если сумма после операции > `AGENT_MAX_DAILY_BUDGET_RUB` — операция
  отклоняется с `AgentSafetyError`.

### M2.5 Staged rollout

Агент **не получает** продакшн-доступ за один шаг. Рекомендуемая схема
развёртывания (минимум 30 дней, с success gates между этапами):

| Этап | Длительность | Что разрешено агенту | Gate для перехода |
|------|--------------|----------------------|-------------------|
| **0. Shadow** | 3–5 дней | Только read. Предлагает изменения, не делает | План агента совпадает с решениями человека в 80%+ случаев |
| **1. Assist** | 7 дней | Минус-слова, pause underperformers, bid ±10% | Нет ложных позитивов по kill-switches; CPA не ухудшился |
| **2. Autonomy light** | 14 дней | Bid ±25%, budget ±15%, создание ключей | Все метрики в целевых коридорах; Quality Score не падал |
| **3. Autonomy full** | постоянно | Всё кроме `forbidden_operations` | — |

Текущий этап хранится в `agent_policy.yml` как `rollout_stage: 1`.
CLI команда `yadirect-agent rollout promote` — переключает этап,
пишет в audit, требует явного подтверждения человеком.

### M2.6 Quality Score как защищённая метрика

**Отдельное правило, вытекающее из kill-switch #4.** Quality Score в
Директе влияет на CPC напрямую: потеря QS на 1 пункт может поднять
реальную цену клика на 10–20%. Восстановление — недели. Поэтому:

- QS по каждому ключу логируется ежедневно в отдельный ts-файл.
- Если медианный QS кампании упал на >1 пункт за 7 дней — агент
  останавливается, пишет алёрт, ждёт человека.
- Агент **никогда** не оптимизирует против QS (не использует как
  целевую метрику). QS — constraint, не objective.

---

## M3 — MCP-сервер

Адаптер ядра к MCP-протоколу — чтобы агента можно было использовать из
Claude Desktop / Claude Code.

### M3.1 Server bootstrap

`src/yadirect_agent/mcp_server/server.py`:

- Использовать официальный `mcp` Python SDK (`mcp.server`).
- Stdio transport.
- Инструменты — **те же самые** функции, что и в `agent/tools.py`
  (не дублировать!). Оборачиваем в MCP-tool-декораторы.

### M3.2 Поведение

- По умолчанию — read-only инструменты и pause (обратимые).
- Write-операции (create campaign, set budget, resume) — только если
  запущен с флагом `--allow-write` (или env `MCP_ALLOW_WRITE=true`).
- Любой инструмент возвращает структурированный результат + читаемую
  строку.

### M3.3 Конфиг для Claude Desktop

`docs/CLAUDE_DESKTOP.md` с готовым JSON-блоком:

```json
{
  "mcpServers": {
    "yadirect": {
      "command": "yadirect-mcp",
      "env": {
        "YANDEX_DIRECT_TOKEN": "...",
        "YANDEX_METRIKA_TOKEN": "...",
        "YANDEX_USE_SANDBOX": "true"
      }
    }
  }
}
```

---

## M4 — Семантика (реальный Wordstat)

### M4.1 Архитектура провайдеров

- `WordstatProvider` (Protocol) уже есть.
- Реализации:
  - `DirectKeywordsResearch` — уже есть (только has_search_volume).
  - `WordstatApiProvider` — для реального Wordstat API
    (`api.wordstat.yandex.net`). Требует отдельный токен и approved доступ
    (для 99% пользователей недоступен).
  - `KeyCollectorBridge` — чтение CSV из Key Collector (как fallback).
  - `MockWordstatProvider` — для тестов и демо.

### M4.2 Кластеризация v2

- Вместо наивного `cluster_key` — через эмбеддинги.
- Использовать multilingual sentence-transformers локально ИЛИ вызов
  Anthropic с batch-режимом для получения семантической близости.
- Порог близости в конфиге.

### M4.3 Минус-слова и чистка

- Сервис `semantics.clean`:
  - Удаление стоп-слов.
  - Определение и удаление нерелевантных хвостов (бесплатно, скачать,
    википедия, отзывы и т.п. — по настраиваемому списку).
  - Группировка по коммерческому интенту.

### M4.4 Upload в кампанию

- `semantics.upload_to_adgroup(adgroup_id, cluster)` — создаёт ключи
  в группе, соблюдая лимиты Директа (200 ключей на группу).

---

## M5 — A/B testing service

### M5.1 Модель теста

`src/yadirect_agent/services/ab_testing.py`:

- `AbTest`: `id`, `adgroup_id`, `variants: list[int]` (ad_ids), `metric`
  (CTR | CR | CPA), `min_impressions`, `confidence_level`,
  `started_at`, `status`.
- Сохраняется в JSON-файле (для MVP) или SQLite (для prod).

### M5.2 Запуск теста

- Принимает adgroup + 2–4 варианта объявлений.
- Создаёт их через `ads.add`, отправляет на модерацию.
- Регистрирует тест в стейте.
- Распределение показов — равномерное через встроенный механизм ротации
  Директа (одна группа, несколько объявлений).

### M5.3 Оценка

- `evaluate(test_id)` — тянет статистику по каждому варианту,
  считает стат. значимость. Используем **разные тесты для разных
  метрик** (см. `ericosiu/ai-marketing-skills/growth-engine` как
  референс):
  - **CTR / CR** (binomial) — chi-square test или двухпропорциональный
    z-test.
  - **CPA / ROAS** (непрерывные, скошенные) — Mann-Whitney U test (не
    t-test: распределение затрат на клиента сильно скошено влево).
  - **Доверительные интервалы** — bootstrap (1000+ resamples) вместо
    параметрических формул. Работает с любым распределением.
- Минимум наблюдений на вариант перед оценкой:
  - 1000 показов ИЛИ 100 кликов (для CTR).
  - 30 конверсий на вариант (для CR / CPA).
  - Если меньше — возвращаем `InsufficientData`, не делаем выводов.
- Значимость по умолчанию: p < 0.05.
- Возвращает: `Winner(variant_id, confidence)` / `NoSignificantDifference`
  / `InsufficientData`.

### M5.4 Завершение

- `conclude(test_id)` — ставит на паузу проигравшие варианты через
  `ads.suspend`. Пишет в audit log.

---

## M6 — Отчётность (Metrika)

### M6.1 Методы клиента

Дописать `clients/metrika.py`:
- `get_goals(counter_id)`
- `get_report(counter_id, metrics, dimensions, date1, date2)` — через
  `/stat/v1/data`.
- `get_conversion_by_source(counter_id, goal_id, date_range)`.

### M6.2 Сервис

`services/reporting.py`:
- `campaign_performance(campaign_id, date_range)` — связка Direct
  (затраты, клики) + Metrika (конверсии, CR, CPA) в один отчёт.
- `daily_summary()` — что изменилось за день: что ушло в минус, где
  CPA вырос, где упал трафик.

### M6.3 Алёрты

- `services/alerts.py` — правила «если CPA кампании вырос на 30%
  относительно 7-дневного среднего, зарегистрировать alert».
- Alerts пишутся в `alerts.jsonl` + отправляются инструментом агента
  на следующий запуск.

---

## M7 — Тесты

### M7.1 Unit

Для каждого сервиса — минимум:
- Happy path на моке клиента.
- Обработка `ValidationError`, `AuthError`, `RateLimitError`.
- Проверка audit-событий (через фикстуру с in-memory sink).

### M7.2 HTTP-слой

- Для `DirectApiClient` — `respx` моки на:
  - 200 OK с result.
  - 200 с error (разные коды → разные исключения).
  - 500 → retry.
  - Timeout → retry.
  - 429 → retry с бэкоффом.
  - Парсинг Units заголовка.

### M7.3 VCR (опционально)

- `pytest-vcr` кассеты для песочницы Директа. Хранить в
  `.vcr_cassettes/`, не коммитить если есть токены.

### M7.4 Coverage target

- 80% по `src/`. `pytest-cov` в make-команде.

---

## Что точно НЕ делаем в первой версии

- Web-UI / дашборд (это отдельный проект).
- Мультитенантность / агентский режим для нескольких клиентов
  одновременно.
- Автоматический parsing конкурентов.
- Обучение собственной модели.
- Интеграция с CRM (заявки, продажи).

---

## Checklist к релизу 0.1.0

- [ ] M0 полностью
- [ ] M1.1–M1.4 полностью (CLI-агент работает в песочнице)
- [ ] M2.0 все 7 kill-switches реализованы
- [ ] M2.1–M2.3 (safety без daily budget guard)
- [ ] M3.1–M3.3 (MCP-сервер работает с read-only + pause)
- [ ] M7.1, M7.2 (unit и http-моки)
- [ ] README, CI зелёный
- [ ] Один конец-в-конец сценарий в песочнице: «собери список кампаний,
      выбери с низким CTR, поставь на паузу» — работает через CLI.

---

## Prior art & references

Перед имплементацией каждого milestone — посмотри референсы в
`docs/PRIOR_ART.md`. Там расписано, какие репозитории стоит
изучить/заимствовать под какую задачу, с конкретными ссылками на файлы.
