# Техническое задание: yadirect-agent

> Документ для Claude Code. Написан как набор задач с приёмочными
> критериями. Задачи можно брать в любом порядке, но **M0 → M1 → M2 → M3**
> — это естественная последовательность для первого рабочего релиза.
> M4–M14 — путь от «работает в песочнице» до «реально заменяет
> медиабайера в продакшне».

## Северный полюс продукта

Цель — не «удобный интерфейс к API Директа», а **AI-агент, способный
взять на себя 80%+ рутинных решений медиабайера на платформах
Яндекса** (Директ + Метрика + Wordstat + Аудитории), оставив человеку
стратегию, бизнес-цели и подтверждение нетривиальных изменений.

Что под этим понимается операционно:

1. **Семантика и минусовка** — собирает ядро, кластеризует, фильтрует
   нерелевантное, регулярно чистит мусорные запросы (M4).
2. **Креативы** — генерирует тексты объявлений, ведёт пайплайн модерации,
   следит за разнообразием креативов в группе (M8).
3. **Тестирование** — запускает A/B на объявлениях и креативных
   гипотезах, оценивает по правильному стат-тесту, авто-завершает
   проигравших (M5, расширение в M8).
4. **Управление ставками и стратегиями** — ведёт ставки и smart-стратегии
   Директа, реагирует на сдвиг CPA / ROAS, защищает Quality Score (M2,
   расширение в M11).
5. **Аудитории и таргетинг** — управляет сегментами Метрики, ретаргетом,
   look-alike, гео/девайс/часовыми корректировками (M9).
6. **Бюджеты и пейсинг** — распределяет месячный бюджет по кампаниям,
   следит за пейсингом, перебрасывает бюджет между кампаниями
   в безопасных коридорах (M10).
7. **Аналитика и отчётность** — связывает Директ ↔ Метрика, считает
   реальные CPA/ROAS, поднимает алёрты, готовит человекочитаемые
   отчёты для стейкхолдера (M6, M12).
8. **Здоровье аккаунта** — мониторит модерацию, отклонённые объявления,
   потерю показов, дрейф запросов; автоматически чинит то, что можно,
   зовёт человека на остальное (M13).

Что **остаётся за пределами** даже в полной версии:
- Стратегические бизнес-решения (что именно продаём, кому, за сколько).
- Креативный direction визуальной части бренда.
- Переговоры с клиентом / отчётность в форме встречи.
- Интеграция с CRM (заявки → продажи) — отдельный продуктовый домен.
- Анти-бот / детекция кликфрода — это инфраструктурная задача Директа.

Каждое расширение функциональности обязано лечь в существующую safety-
архитектуру: новая мутация ⇒ новый `@requires_plan`-шлюз ⇒ хотя бы один
kill-switch стережёт её ⇒ audit-событие до и после. **Никаких новых
поверхностей записи без safety-шлюза, даже временно.**

## Путь пользователя — продуктовый якорь

Полный нарратив «как пользователь живёт с продуктом» — в
[`docs/OPERATING.md`](./OPERATING.md) → "User journey". Здесь — ровно
то, что нужно разработчику, чтобы оценить, **зачем мы делаем каждую
фичу**.

Главная персона — **Анна**, владелец небольшого e-shop, не
разработчик. От установки до тишины-как-успеха путь делится на
четыре фазы:

| Фаза | Длительность | Что в ней происходит | Какие милстоуны её обслуживают |
|------|--------------|----------------------|--------------------------------|
| **0. Discovery** | ≤ 10 мин | Установка, привязка аккаунта, **первый отчёт без оплаты Anthropic** | M0–M3 (фундамент), **M15** (onboarding) |
| **1. Shadow** | 7 дней | Агент только наблюдает, ежедневное «что бы я сделал», калибровка | M6 (Метрика-репорт), **M20** (rationale) |
| **2. Assist** | 14–21 день | Агент сам делает обратимое; всё остальное — через approval-кнопки | **M18** (notifications/approvals), M5, M11 |
| **3. Autonomy** | постоянно | Тишина-как-успех, weekly-digest, alerts только под аномалию | M8, M9, M10, M12, M13, **M16** (сезонность) |

Cross-cutting (нужно на всех фазах): **M19** rollback, **M20**
rationale, **M17** competitive intel, cost tracking агента,
[`agent_policy.yml`](../agent_policy.example.yml).

### Контракт продукта (производный от journey)

Вытекает из journey и обязателен для каждой фичи:

1. **Time to first value ≤ 10 минут.** От установки до «вот что у
   тебя в кабинете» — без YAML, без терминала после установки, без
   оплаты сторонним сервисам.
2. **Никаких surprise-мутаций.** Тратящие или необратимые операции
   спрашивают, и спрашивают **с обоснованием**.
3. **One-click rollback.** Любое агентское действие восстановимо до
   состояния «как было».
4. **Тишина = успех.** Полностью автономный агент пишет weekly,
   monthly и под аномалии — а не каждое утро.
5. **Stop, don't escape.** Под аномалией агент **тормозит и зовёт
   человека**, не пытается «выкрутиться».

### Принцип, по которому отбираем фичи

Каждая предлагаемая фича обязана ответить на два вопроса:

- В какую фазу journey она попадает?
- Какое из 5 правил контракта без неё становится невыполнимым?

**Если ни на один из вопросов нет чёткого ответа — фича не идёт
в продукт.** Это касается и существующих M4–M14, и любого нового
предложения. Ниже M-секции каждая указывает свою фазу и часть
контракта в начале.

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

`docs/OPERATING.md` с готовым JSON-блоком:

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

## M8 — Креативы: генерация, оценка, лайфцикл

PPC-агент без управления креативами — это калькулятор без рук. Этот
milestone превращает агента из «тех, кто меняет ставки» в «тех, кто
ведёт творческую часть аккаунта».

### M8.1 Генерация текстовых объявлений

- `services/creatives/generator.py`:
  - Вход: `adgroup_id`, бизнес-контекст (из `BusinessProfile`, см. M8.5),
    список ключей, набор УТП.
  - Выход: набор `AdDraft` (заголовок 1, заголовок 2, текст, отображаемая
    ссылка, быстрые ссылки, уточнения).
  - Под капотом: Anthropic с инструкцией-шаблоном, ограничениями Директа
    (символы), валидацией каждой строки против правил Директа
    (запрещённые символы, заглавные буквы, рекламные слова).
- Несколько **независимых hook-стратегий**: «выгода», «социальное
  доказательство», «срочность», «альтернатива». Каждый драфт
  тегируется hook-ом, чтобы M8.4 умела сравнивать их между собой.

### M8.2 Модерация и lifecycle

- `services/creatives/moderation.py`:
  - `submit(drafts) → list[AdId]` — отправка через `ads.add` + перевод
    в `ads.moderate` если нужно.
  - `poll_status(ad_ids)` — daily-job: тянет статус модерации через
    `ads.get`, пишет в `audit`.
  - `auto_repair(rejected)` — для типовых причин отказа (текст с
    нерекомендуемыми символами, превышение длины, клейм без
    оснований) генерирует исправленную версию через `M8.1` и
    переотправляет через `@requires_plan`.
- Сложные / спорные отказы выпадают как алёрт — M13.

### M8.3 Diversity guard в группе

- Внутри одной adgroup поддерживать 3–5 активных вариантов с
  разными hook-стратегиями. Если живых < 2 — алёрт + предложение
  сгенерить новые. Если > 5 — предложение остановить худшие.

### M8.4 Креативный A/B (расширение M5)

- Тот же `AbTest`-движок, но субъект — не «два объявления»,
  а **hook-стратегия**. Метрика — CTR + CR в синтетику CTR×CR.
- Стат-тест: bootstrap CI (как в M5.3), с поправкой на множественные
  сравнения (Bonferroni / BH) если вариантов > 2.
- `conclude` останавливает проигравшие hook'и и предлагает следующую
  партию через M8.1.

### M8.5 BusinessProfile

- `models/business.py` — `BusinessProfile`: чем занимается клиент,
  ICP (idealкустomer profile), тон коммуникации, запрещённые
  утверждения (по 38-ФЗ + регуляторным ограничениям его ниши),
  гарантии бренда.
- Грузится из YAML (`business_profile.yml`) рядом с `agent_policy.yml`.
- Передаётся в каждый промпт генерации креатива и в audit как
  `business_profile_version`. Изменения профиля версионируются.

### M8.6 Гарантии безопасности

- Новый kill-switch **#8 — creative compliance**: каждый сгенерированный
  драфт проходит через `compliance_check(draft, business_profile)`
  до отправки в Директ. Запрещённые формулировки — отказ. Список
  ведётся отдельно от `agent_policy.yml`, чтобы не путать
  PPC-настройки с маркетинговыми.
- `auto_approve_creative_submit` по умолчанию **False** — каждое
  новое объявление требует apply-plan, пока человек не накопит
  доверия к выходу M8.1 (после N успешных модераций — можно
  включить через policy).

**Приёмка**: на тестовом аккаунте — собрать adgroup, агент по запросу
«нужны 3 новых креатива под зимнюю акцию» сгенерил, прошёл
compliance, прошёл модерацию (или вернул понятный отказ), записал
весь путь в audit.

---

## M9 — Аудитории и таргетинг

Управление сегментами и корректировками. Без этого «оптимизация
кампании» — всегда оптимизация только ставок и текстов, что в Директе
часто проигрывает работе с аудиториями.

### M9.1 Audience API client

- `clients/audience.py` — клиент к Yandex Audience API
  (`api-audience.yandex.ru`).
- Методы: `list_segments`, `get_segment`, `create_lookalike(seed_id)`,
  `delete_segment`. Все через свой OAuth-scope.

### M9.2 Сегменты Метрики

- `services/audiences/metrika_segments.py` — обёртка над сегментами
  Метрики (counters → segments). Чтение существующих, создание новых
  на базе пользовательских условий (URL-паттерн, событие, временное
  окно).

### M9.3 Look-alike и ретаргетинг

- `services/audiences/retargeting.py`:
  - Генерация look-alike на базе сегмента «купившие за 30 дней».
  - Ретаргетинг-условия: «зашли, но не купили», «бросили корзину»,
    «вернулись через N дней», «просмотрели карточку, но не категорию».
  - Создаются как `RetargetingList` в Директе (`retargetinglists.add`).

### M9.4 Корректировки ставок

- `services/audiences/adjustments.py`:
  - Geo (по регионам Яндекса), device (mobile/desktop/tablet),
    demographic (пол × возраст), audience (сегменты Метрики),
    время суток / дни недели (dayparting).
  - Применяются через `bidmodifiers.set` на уровне кампании.
- Новый kill-switch **#9 — adjustment ceiling**: ни одна
  корректировка не может выходить за коридор `[-90%, +400%]` без
  явного apply-plan. Корректировки в реальной жизни редко вне `[-50%, +200%]`,
  но Директ позволяет шире — это footgun.

### M9.5 Прозрачность

- `audiences.list` — tool возвращает все активные аудитории и
  корректировки на ровный список с `applied_to` (campaign_id).
  Чтобы агент в каждой сессии «видел», что он уже сделал.

---

## M10 — Бюджет: планирование, пейсинг, перераспределение

«Сколько потратить» — отдельный домен, который медиабайер ведёт
руками с месячного KPI вниз. Без автоматизации этого слой
«оптимизировал кампанию А, забыл кампанию Б» — гарантия.

### M10.1 Месячный план

- `services/budget/planner.py`:
  - Вход: месячный бюджет (RUB), цель (макс конверсий | таргет CPA |
    таргет ROAS), список активных кампаний с историческими CPA/ROAS.
  - Выход: распределение `{campaign_id: monthly_budget_rub}` +
    предложение дневных бюджетов с buffer на пейсинг.
  - Алгоритм: марживальная оптимизация по эластичности (если кампания
    А даёт +20% конверсий за +10% бюджета, кампания Б — +5%, то
    марживалл идёт в А до выравнивания).

### M10.2 Пейсинг daily-job

- `services/budget/pacing.py`:
  - Раз в день (cron-инструмент) считает spent-to-date vs планируемый
    pace для каждой кампании.
  - Если опережение > X% — предложение снизить дневной бюджет.
  - Если отставание > X% и кампания не упирается в low-funnel — поднять
    дневной бюджет в коридоре `max_daily_budget_change_pct`.

### M10.3 Forecast

- `services/budget/forecast.py`:
  - На базе 28-дневной истории клика/конверсии: прогноз кликов и
    конверсий на конец месяца с CI (90% bootstrap).
  - Используется planner'ом и попадает в M12-отчёты.

### M10.4 Гарантии

- KS#1 (account budget cap), KS#5 (budget balance shift) — те же.
- Новый kill-switch **#10 — pacing emergency stop**: если кампания
  потратила > 90% месячного бюджета до 70% месяца — немедленная
  пауза и алёрт. Не «снизить бюджет», именно пауза, потому что
  обычно это значит, что что-то крупное пошло не так с трафиком.

---

## M11 — Bid strategies (smart-стратегии Директа)

Сегодня агент ставит ручные ставки. В реальном кабинете 80%
кампаний — на smart-стратегиях («оптимизация конверсий», «целевая
доля показов» и т.д.). Без поддержки этих стратегий агент уступит
встроенному ML Директа.

### M11.1 Mapping стратегий

- `models/strategies.py` — типизированные представления стратегий:
  `MaxClicksWithCpcLimit`, `MaxConversions`, `TargetCpa(target_rub)`,
  `TargetRoas(target_pct)`, `MaxImpressionsShare(target_pct)`,
  `WeeklyBudgetWithCpaCap` и т.д. Полное покрытие текущей матрицы
  Директа на дату релиза.

### M11.2 Сервис

- `services/strategies/manage.py`:
  - `set_strategy(campaign_id, strategy)` — меняет стратегию через
    `campaigns.update`. Под `@requires_plan`.
  - `evaluate(campaign_id)` — рекомендует стратегию по: количеству
    конверсий за 28 дней, дисперсии CPA, цели аккаунта. Возвращает
    `StrategyRecommendation` с обоснованием.

### M11.3 Trigger-based switches

- Правила «если у кампании накопилось ≥30 конверсий за 14 дней —
  предложить переход на `TargetCpa`». Правило срабатывает в
  daily-job, формирует план через `@requires_plan`.

### M11.4 Гарантии

- Smart-стратегии часто **просят период обучения** (5–14 дней).
  Новый kill-switch **#11 — strategy churn limit**: на одну кампанию
  не больше 1 смены стратегии за 14 дней. Иначе агент будет
  истеричить и не давать никому стабилизироваться.

---

## M12 — Отчётность для стейкхолдеров

Расширение M6: M6 — это «алёрты для агента и оператора», M12 — «отчёты
для человека, который платит».

### M12.1 Еженедельный отчёт

- `services/reporting/weekly.py`:
  - Генерирует Markdown-отчёт за прошедшую неделю по аккаунту:
    spent, clicks, conversions, CPA, ROAS, динамика к предыдущей
    неделе, топ-3 кампании по эффективности, топ-3 проблемы,
    что агент сделал (выжимка из audit с человекочитаемыми
    формулировками).
  - LLM-постпроцессинг: 3–5 «инсайтов» текстом (что выросло,
    что упало, почему по гипотезе агента).
- Шаблон в Jinja, чтобы можно было кастомизировать на агентство.

### M12.2 Ежемесячный отчёт

- То же, но сравнение к прошлому месяцу + к плану M10.1.
- Дополнительно: forecast на следующий месяц (M10.3) с CI.

### M12.3 Доставка

- CLI: `yadirect-agent report weekly --to=email|file|stdout`.
- MCP: tool `generate_report(period: weekly|monthly)` возвращает
  Markdown как структурированный результат.
- Опционально (M12.4): рендер в PDF через `weasyprint`. Не приоритет.

### M12.4 Multi-account ready

- Шаблон отчёта параметризуется `client_login` — заготовка под M14.
- Каждый отчёт пишет в audit `report.generated` с ts, актором,
  периодом, хэшем содержимого.

---

## M13 — Здоровье аккаунта и мониторинг модерации

Самый «инфраструктурный» milestone — то, что медиабайер делает
утром, прежде чем смотреть метрики.

### M13.1 Daily health check

- `services/health/check.py` — daily-job, идёт по checklist:
  - Отклонённые объявления (`ads.get` с фильтром `Rejected`).
  - Ключи в статусе `Rejected` / `Suspended` / `Few impressions`.
  - Кампании с потерянной долей показов из-за бюджета (через
    Метрику `report` с метрикой `BudgetLostImpressions`).
  - Кампании с потерянной долей показов из-за рейтинга (Quality
    Score).
  - Группы без активных объявлений.
  - Кампании, в которых внезапно дрейфанула CTR > 30% за неделю.
- Каждое нарушение → запись в `health_findings.jsonl` + алёрт через M6.3.

### M13.2 Auto-repair простых случаев

- Через ту же `M8.2 auto_repair` для модерационных отказов.
- Группы без активных объявлений → агент предлагает (через
  `@requires_plan`) сгенерить и отправить новые.
- Кампания исчерпала дневной бюджет к 14:00 на 3 дня подряд —
  предложение поднять дневной бюджет (внутри коридора M10).

### M13.3 Ad-health dashboard

- CLI: `yadirect-agent doctor account` — выводит сводный статус
  аккаунта в одном экране (light мониторинг между daily-job
  запусками).
- MCP-tool: `account_health()` возвращает структурированный отчёт.

---

## M14 — Agency mode (мультиаккаунт)

**Опционально и сильно позже.** Только если у проекта появляется
агентский use-case.

### M14.1 Multi-tenancy в clients

- Все клиенты принимают `client_login` явно; `Settings`
  поддерживает `clients_config` с per-client профилями
  (token, sandbox-режим, policy-файл).
- Один процесс может вести несколько кабинетов параллельно.

### M14.2 Per-client policy и audit

- `agent_policy.yml` → `agent_policy/<client_login>.yml`.
- `logs/audit.jsonl` → `logs/audit/<client_login>.jsonl`.
- Никакого cross-client leak — отдельные `Settings`, отдельные
  `Audit Sink`-и, проверки через тесты.

### M14.3 Agency dashboard

- CLI: `yadirect-agent agency status` — таблица per-client:
  spent, conversions, alerts, последний agent-run.
- M12-отчёты per-client с агрегатной шапкой.

### M14.4 Почему опционально

- Если проект решает one-account use-case (внутренний инструмент
  владельца аккаунта) — M14 не нужен и добавляет сложности
  безопасности (риск повреждения чужих данных).
- Если становится агентским SaaS — это уже отдельный продуктовый
  слой (биллинг, RBAC, web-UI), который выходит за пределы этого
  репозитория.

---

## M15 — Frictionless onboarding (точка входа)

> **Фаза journey**: Phase 0 (Discovery) и Phase 1 (Shadow setup).
> **Контракт**: time-to-first-value ≤ 10 минут без YAML и без оплаты
> Anthropic.

Сейчас pre-M15 пользователь проходит ~7 ручных шагов до первого
запуска (Python, git clone, OAuth-app, Anthropic-карта, .env, policy,
cron). Каждый — точка отвала. M15 убирает все, кроме одного клика
«Разрешить» в Yandex OAuth и одной команды установки.

### M15.1 PyPI release

- Проект публикуется в PyPI как `yadirect-agent`.
- `pip install yadirect-agent` ставит CLI и MCP-server.
- Альтернатива — `docker run yadirect-agent/yadirect onboard` для
  тех, у кого нет Python.
- CI workflow на релиз по тегу (`v*.*.*`), сборка sdist + wheel,
  publish через trusted-publisher (без хранения PyPI-токена).

### M15.2 `install-into-claude-desktop`

- Команда `yadirect-agent install-into-claude-desktop`:
  - находит config Claude Desktop по OS (macOS / Windows / Linux),
  - делает backup существующего конфига перед изменениями,
  - вставляет корректный `mcpServers` блок (или объединяет с
    существующим),
  - валидирует JSON после записи,
  - выводит готовое сообщение «перезапусти Claude Desktop, потом
    напиши: помоги настроить агента».
- `--dry-run` показывает diff без записи.
- `uninstall-from-claude-desktop` — реверс.

### M15.3 Standard OAuth flow с локальным callback

- Один раз регистрируем OAuth-приложение `yadirect-agent` в Yandex
  с правильными scopes (`direct:api`, `metrika:read`, `metrika:write`).
- В onboarding: команда поднимает локальный HTTP-server на
  `localhost:8765/callback`, открывает в браузере
  `https://oauth.yandex.ru/authorize?...` со своим `client_id`.
- Пользователь видит знакомую страницу Yandex, нажимает "Разрешить",
  Yandex редиректит на `localhost:8765/callback?code=...`,
  локальный server обменивает `code` на access token и сохраняет
  его в OS keychain через `keyring`.
- `.env` больше не содержит токены — `Settings` читает их из
  keychain (с fallback на env vars для CI/Docker).
- Команды: `yadirect-agent auth login`, `auth status`, `auth logout`.
  (`logout` чистит локальный keychain-slot; Yandex OAuth не
  предоставляет публичный revocation-endpoint, так что
  refresh-токен на стороне Yandex остаётся валидным до ручного
  отзыва на `yandex.ru/profile/access`.)

### M15.4 Conversational onboarding via MCP

- MCP-tool `start_onboarding()`:
  - проверяет статус OAuth (если нет — триггерит M15.3);
  - собирает `BusinessProfile` через серию вопросов (ниша, ICP,
    бюджет, цели, запрещённые формулировки);
  - предлагает разумные дефолты для policy на основе текущего
    состояния аккаунта (например: budget cap = 1.2× от текущего
    суммарного дневного бюджета);
  - делает baseline snapshot (M19);
  - запускает первый health-check (M15.5) и возвращает отчёт.
- Tool возвращает структурированный + читаемый результат, чтобы
  Claude Desktop мог показать его в чате как нормальный ответ.
- Шаги — re-runnable: повторный вызов с уже настроенным
  BusinessProfile спрашивает, что обновить, не начинает с нуля.

### M15.5 `--no-llm` rule-based mode

- Команда `yadirect-agent doctor account --no-llm` и MCP-tool
  `account_health()` без зависимости от Anthropic API key.
- Реализация — детерминистический pipeline на правилах M13:
  - кампании с CTR < threshold за 7+ дней,
  - кампании-сжигатели (трата без конверсий),
  - rejected-keywords и rejected-ads,
  - lost-impression-share (бюджет/рейтинг),
  - дрейф CTR > X% за неделю.
- Вывод — структурированный отчёт «вот N проблем, вот их размер
  в рублях, вот что я бы предложил».
- Это **обязательная часть MVP** — единственный способ показать
  ценность до того, как пользователь добавит Anthropic-key.
- LLM-функционал помечается декоратором `@requires_llm` на
  тулзах, где он реально нужен (M8 generation, M12 insights,
  conversational mode). Без ключа эти тулзы не регистрируются.

### M15.6 Built-in scheduler

- Команда `yadirect-agent schedule install`:
  - macOS — пишет `~/Library/LaunchAgents/...plist` + `launchctl load`,
  - Linux — `systemd --user` timer + service unit,
  - Windows — Task Scheduler через `schtasks`.
- По умолчанию — daily run в 08:00 локального времени, плюс
  hourly health-check.
- `schedule status / remove / pause` — управление без знания
  внутренностей OS.
- Лог запусков идёт в стандартное место аудита (`logs/audit.jsonl`).

### M15.7 Acceptance

- Чистая машина с Python 3.11+ или Docker.
- `pip install yadirect-agent && yadirect-agent install-into-claude-desktop`.
- Перезапуск Claude Desktop, в чате: "помоги настроить агента".
- 2 клика «Разрешить» в браузере.
- 5 ответов на вопросы wizard'а в чате.
- Первый отчёт «вот что в твоём кабинете» — **без Anthropic-key**.
- **Total elapsed: ≤ 10 минут.** Замеряется на CI smoke-test
  (mock OAuth, mock Direct/Metrika, секундомер от `pip install` до
  возврата `account_health()`).

---

## M16 — Calendar & seasonality

> **Фаза journey**: Phase 3 (Autonomy). **Контракт**: «тишина = успех»
> ломается, если агент истеричит на ожидаемых сезонных пиках.

### M16.1 Календарь событий

- `services/calendar/events.py` — модель `MarketingEvent`: `name`,
  `date_range`, `kind` (holiday | promo | external), `expected_lift`
  (multiplier на трафик/CPA), `applies_to` (campaign_ids или null).
- Встроенный список российских событий (Black Friday, Кибер-понедельник,
  8 марта, 23 февраля, 1 сентября, новогодние каникулы).
- Пользовательские события вводятся через CLI/MCP-tool
  `add_calendar_event(...)`.

### M16.2 Pre-event и post-event поведение

- За N дней до события (configurable per event_kind) агент:
  - предлагает поднять дневные бюджеты в обозначенный коридор
    (через M10 + `@requires_plan`);
  - проверяет, что креативы под событие активны (через M8);
  - переводит проверки аномалий в «sensitive» режим (lift на CPA
    в день события не считается алёртом, если попадает в
    `expected_lift`).
- После события — снимает «sensitive», запускает post-mortem отчёт
  через M12.

### M16.3 Гарантии

- Никаких автоматических budget-bump'ов **до события** без
  apply-plan от человека — это всегда «спрашивает».
- KS#1 (account budget cap) и KS#5 (budget balance shift) **не
  ослабляются** под событие.

---

## M17 — Competitive intelligence (через API)

> **Фаза journey**: Phase 2 (Assist) и далее. **Контракт**: rationale
> (M20) должен уметь объяснять «почему ставка не сработала» —
> без данных аукциона он этого не может.

Парсинг чужих сайтов остаётся в out-of-scope. Здесь — только то,
что Direct и Metrika **сами** отдают через API.

### M17.1 AuctionPerformance reader

- `clients/direct.py` — метод `auctionperformance.get` (если
  доступен в текущей версии Direct API; иначе — отчёт `reports`
  с метриками `AvgImpressionPosition`, `ImpressionShare`,
  `LostImpressionShareDueToRank`).
- `services/competition/auction.py`:
  - `get_position_history(campaign_id, days=28)` — динамика
    позиции в аукционе.
  - `get_competitor_pressure(campaign_id, days=7)` — сводка:
    кто отъедает наши показы, какая доля по ключевым keywords.

### M17.2 Использование в rationale

- M20 (rationale) интегрируется с M17: каждое решение по ставкам
  аннотируется текущим конкурентным контекстом.
- Пример: *«поднял ставку на ключе X на 18%, потому что доля
  показов упала с 62% до 41% за 5 дней — конкурент перебил, а CPA
  на ключе всё ещё в коридоре»*.

### M17.3 Альтернатива (если API недоступен)

- В отсутствие auctionperformance — fallback на собственные
  метрики (`avg_impression_position`, `lost_impression_share_*`)
  из `reports.get`.
- Никогда не парсим выдачу или чужие лендинги — это и юридически,
  и продуктово отдельная история.

---

## M18 — Notifications & approvals (Telegram / Slack / email)

> **Фаза journey**: обязательно для Phase 2 (Assist) и Phase 3.
> **Контракт**: «никаких surprise-мутаций» невыполним без канала,
> где пользователь живёт, а не в терминале.

### M18.1 Notification sinks

- `services/notify/sink.py` — Protocol с реализациями:
  - `TelegramSink` (Bot API, токен в keyring через M15.3).
  - `SlackSink` (Incoming Webhook).
  - `EmailSink` (SMTP, для weekly/monthly).
  - `ChatSink` (возврат в Claude Desktop через MCP-tool result —
    fallback, когда других каналов нет).
- Один `NotificationDispatcher` маршрутизирует событие по правилам
  (severity → каналы): aлёрт → Telegram + email; weekly → email;
  daily-shadow → Telegram.

### M18.2 Approval requests с inline-кнопками

- При `OperationPlan` со `requires_confirmation=True`:
  - сериализуем preview + reason + side-effects в карточку
    Telegram inline-keyboard с кнопками `Apply / Reject / Why`;
  - callback_data — `plan_id` + signature (HMAC от secret для
    защиты от подделки);
  - tap «Apply» → бот вызывает `apply-plan <plan_id>` через
    локальный socket / pipe (агент крутится у пользователя
    локально), audit пишет actor=`telegram:<user_id>`;
  - tap «Why» → бот возвращает расширенное обоснование (M20).
- Slack — слешкоманды `/yadirect-approve <plan_id>`.
- Тайм-аут approval — 24 часа; после — план переходит в
  `expired`, агент пишет в digest «истёк такой-то план».

### M18.3 Anti-injection защита

- Telegram callback_data подписывается HMAC-SHA256 на shared
  secret (хранится в keyring).
- Чужие сообщения с поддельным callback_data отклоняются с
  audit-event `notify.signature_mismatch`.
- Кнопка «Apply» работает **только для plan_id, который был
  отправлен этим же ботом и которому ещё не истёк tаймаут**.
- KS#-список (M2) re-evaluated на момент apply, **не** на момент
  отправки notification — staleness defense.

### M18.4 Setup

- `yadirect-agent notify setup telegram` — wizard:
  - помогает создать бота через `@BotFather`,
  - сохраняет токен в keyring,
  - просит пользователя нажать `/start` боту, чтобы зарегистрировать
    `chat_id`.
- Аналогично `slack` и `email` — но они опциональны.

---

## M19 — Rollback / time machine

> **Фаза journey**: cross-cutting. **Контракт**: «one-click rollback»
> невыполним без явного механизма снапшотов.

### M19.1 Снапшот перед каждым agent run

- `services/snapshot/take.py` — перед стартом любого `AgentRun`:
  - читает «опасные» поля каждой кампании (бюджет, статус,
    стратегия, ставки на ключи, корректировки);
  - пишет в `snapshots/<run_id>.json.zst` (zstd-сжатие).
- Размер снапшота для среднего аккаунта (50 кампаний, 3000 ключей)
  — ~50 KB сжато; хранится 90 дней (configurable).

### M19.2 Команда `rollback`

- `yadirect-agent rollback --to=<run_id>`:
  - читает снапшот;
  - формирует **обратный** `OperationPlan` (восстановить бюджет,
    статус, стратегию для каждого изменённого объекта);
  - **прогоняет через тот же safety-pipeline** — rollback не
    бесплатный, он тоже мутация и тоже может быть опасен;
  - `apply-plan` исполняет.
- `--dry-run` показывает, что будет восстановлено, без записи.

### M19.3 Conversational rollback

- MCP-tool `rollback_last_run()` / `rollback_to(run_id)`.
- В Claude Desktop: *«отмени всё, что ты сделал вчера»* → агент
  находит run, делает dry-run, показывает «верну: бюджет 800→1100,
  paused→active на 3 кампании. Подтвердить?» → tap Apply.

### M19.4 Граничные случаи

- Если с момента run'а часть изменений уже была перезаписана
  более поздними действиями — rollback покажет эти конфликты и
  спросит, что приоритетнее.
- Rollback **не** восстанавливает удалённые объекты (мы и так не
  удаляем — `forbidden_operations`); но если кампания изменила
  статус в Direct по причине вне агента (модерация), rollback
  не будет насильно её включать.

---

## M20 — Human-readable rationale

> **Фаза journey**: cross-cutting, **обязателен с Phase 1** —
> без него shadow-калибровка слепа.
> **Контракт**: «никаких surprise-мутаций» — surprise начинается там,
> где пользователь не понимает, **почему**.

### M20.1 Rationale model (✅ shipped)

- `models/rationale.py` — `Rationale`: `decision_id` (1:1 с
  `OperationPlan.plan_id`), `summary` (1–2 предложения для UI),
  `inputs` (какие данные использованы, с timestamps + values),
  `alternatives_considered` (что ещё рассматривалось и почему
  отвергнуто), `policy_slack` (насколько мы близки к kill-switch
  threshold'у), `confidence` (low | medium | high).
- В коде модель называется `Rationale` (без суффикса `Event`),
  чтобы не путать с audit-events; storage —
  `logs/rationale.jsonl`, индексируется по `decision_id`.

### M20.2 Эмиссия (✅ shipped)

- Каждый агент-цикл, который заканчивается мутацией, эмитит
  `Rationale` **до** соответствующего `*.requested` audit-event.
- Эмиссия — обязательная часть `@requires_plan`-decorator'а;
  без `rationale=` декоратор raise'ит `TypeError` на non-bypass
  пути ДО `pipeline.review`. Apply-plan re-entry path
  (`_applying_plan_id` set) — bypass: rationale записан в момент
  proposal, повторно не эмитится.
- Реализация на стороне tool inputs: каждый mutating-tool input
  (`pause_campaigns`, `resume_campaigns`, `set_campaign_budget`,
  `set_keyword_bids`) несёт required `reason: str`
  (`min_length=10, max_length=500`). LLM физически не может
  вызвать мутирующий tool без артикуляции причины — это и есть
  механизм, который делает M20.3 («не сочиняет на лету»)
  реализуемым.
- Хранение — sibling JSONL `logs/rationale.jsonl`, индексируется
  по `decision_id` и `resource_id`.

### M20.3 Read-back

- CLI: `yadirect-agent rationale show <decision_id>` (✅ shipped)
  или `rationale why --campaign=<id> --on=<date>` (планируется).
- MCP-tool: `explain_decision(decision_id)` (slice 3, в работе).
- В чате: *«почему ты вчера снизил ставку на ключе X?»* — агент
  достаёт `Rationale`, **не сочиняет на лету**.

### M20.4 Использование в notifications (M18)

- Approval-карточка содержит `summary` + ссылку «Why» → расширенное
  обоснование.
- Weekly digest (M12) сжимает rationale-события за неделю в
  3–5 человеческих абзацев.

---

## M21 — Cost tracking агента (LLM-расходы)

> **Фаза journey**: cross-cutting, особенно Phase 3 (Autonomy).
> **Контракт**: «тишина = успех» ломается, если LLM-кредиты
> сожжены незаметно и агент перестал работать ночью.
>
> Promote из *Ideas* — стало обязательным после понимания, что
> агент в проде крутится daily и расходы Anthropic могут
> накапливаться незаметно.

### M21.1 Per-call cost capture

- `agent/loop.py` после каждого `messages.create` пишет
  `tokens_in`, `tokens_out`, `model`, расчётную стоимость в USD/RUB.
- Агрегация в `AgentRun`: total tokens, total cost.
- Появляется в audit как `agent_run.cost`.

### M21.2 Бюджет агента

- В `agent_policy.yml` — `agent_monthly_llm_budget_rub: 3000` (default).
- Daily check: если месячный расход > 80% — мягкий алёрт; > 100% —
  агент переходит в `--no-llm` mode автоматически (M15.5
  деградирует красиво).
- Переключение пишется в audit как `llm_budget.exceeded`.

### M21.3 Surface

- `yadirect-agent cost status` — таблица: spent this month,
  forecast, top-cost runs.
- В weekly digest — строчка «потратили на агента N руб из бюджета M».

---

## SaaS-режим — explicit out-of-scope

«Hosted yadirect-agent» (наш сервер, web-UI, биллинг, multi-tenant
хранение чужих токенов) — **не** милстоун этого репозитория.

Если когда-нибудь делаем — это **форк или отдельный продукт**
поверх той же кодовой базы, с другой моделью данных
(`tenant_id` everywhere), другими compliance-требованиями (хранение
чужих OAuth-токенов = ФЗ-152, ФЗ-149, security review), другим
жизненным циклом релизов и, скорее всего, другой командой.

Здесь мы строим **local-first** инструмент, в котором:
- токены пользователя живут в его собственном keychain,
- audit и snapshots — в его собственной файловой системе,
- агент не делает исходящих вызовов никуда, кроме Yandex API,
  Anthropic API и явно настроенных notification-каналов.

Это не догма — это сознательный выбор архитектуры. Любая фича,
которая молча требует «нам нужен наш сервер», обсуждается отдельно
и, скорее всего, отклоняется в этой кодовой базе.

---

## Что точно НЕ делаем в первой версии

- Web-UI / дашборд (это отдельный проект).
- Мультитенантность / агентский режим для нескольких клиентов
  одновременно.
- Автоматический parsing конкурентов.
- Обучение собственной модели.
- Интеграция с CRM (заявки, продажи).

---

## Релизные чекпоинты

Релизы привязаны к фазам user journey (см. начало документа).
Каждый релиз обязан целиком закрыть свою фазу — half-baked фаза
ломает контракт продукта.

### 0.1.0 — «работает в песочнице» (фундамент, для разработчика)

- [x] M0 полностью
- [x] M1.1–M1.4 (CLI-агент работает в песочнице)
- [x] M2.0 все 7 kill-switches реализованы
- [x] M2.1–M2.3 (safety + audit)
- [x] M3.1–M3.3 (MCP-сервер с read-only + pause; opt-in write)
- [x] M7.1, M7.2 (unit + http-моки)
- [x] README, CI зелёный
- [ ] Один конец-в-конец сценарий в песочнице, повторяемый: «собери
      список кампаний, выбери с низким CTR, поставь на паузу» —
      через CLI.

### 0.2.0 — «Анна может попробовать» (закрывает Phase 0 + Phase 1)

Без этого продукт нельзя предлагать никому, кроме разработчиков.

- [ ] **M15** полностью (PyPI + install-into-claude-desktop +
      OAuth-flow + conversational onboarding + `--no-llm` mode +
      built-in scheduler).
- [ ] **M20** (rationale) — обязателен с Phase 1, без него
      shadow-калибровка не работает.
- [ ] **M21** (cost tracking) — без него LLM-расходы накапливаются
      незаметно.
- [ ] M6 базово (Метрика-отчёт «вот что у тебя в кабинете»).
- [ ] CI smoke-test «time-to-first-value ≤ 10 минут» (M15.7).
- [ ] Acceptance: чистая машина, `pip install`, 5 минут чата с
      Claude Desktop, первый отчёт без Anthropic-ключа — отработало.

### 0.3.0 — «делает половину работы медиабайера» (закрывает Phase 2)

Анна спокойно живёт в assist-режиме.

- [ ] **M18** (notifications + approvals в Telegram/Slack/email) —
      без этого Phase 2 нельзя.
- [ ] **M19** (rollback / time machine) — without it, нет «спать
      спокойно».
- [ ] M4 (Wordstat + минусовка + кластеризация).
- [ ] M5 (A/B на объявлениях со стат-тестами).
- [ ] M6 полностью (отчёты + алёрты).
- [ ] M11 (smart-стратегии Директа).
- [ ] M17 (competitive intel из API — нужен для M20 rationale).
- [ ] M7.3, M7.4 (VCR + coverage 80%+).

### 0.4.0 — «реально заменяет медиабайера» (закрывает Phase 3)

Анна не открывает Директ. Тишина = успех.

- [ ] M8 (генерация креативов + модерация + creative-A/B).
- [ ] M9 (аудитории + корректировки).
- [ ] M10 (бюджетный планировщик + пейсинг).
- [ ] M12 (еженедельный/ежемесячный отчёт).
- [ ] M13 (daily health check + auto-repair).
- [ ] M16 (calendar / seasonality).
- [ ] 30-дневный staged rollout (`shadow → assist → autonomy_full`)
      на одном живом аккаунте, без incident'ов выше LOW в audit.

### 1.0.0 — production-ready

- [ ] Всё из 0.4.0 + три месяца чистого audit'а.
- [ ] M14, если появился agency use-case.
- [ ] CHANGELOG, semver, security advisory процесс.

---

## Prior art & references

Перед имплементацией каждого milestone — посмотри референсы в
`docs/PRIOR_ART.md`. Там расписано, какие репозитории стоит
изучить/заимствовать под какую задачу, с конкретными ссылками на файлы.
