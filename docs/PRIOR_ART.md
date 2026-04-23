# Prior art & references

> Документ для Claude Code и людей. Каждый раздел спека (`TECHNICAL_SPEC.md`)
> ссылается сюда за деталями. Если нужно реализовать какой-то milestone
> — сначала загляни в соответствующую секцию, изучи референс, только
> потом пиши код.

## 🎯 Напрямую применимо к Яндекс.Директу

### [SvechaPVL/yandex-mcp](https://github.com/SvechaPVL/yandex-mcp)

**Что это**: Python MCP-сервер для Yandex Direct + Metrika, 33 инструмента.
MIT, рабочий код.

**Для каких milestones**:
- **M0**: структура проекта, обработка OAuth-токенов, песочница.
- **M3**: MCP-слой (но мы делаем свой, этот — референс).
- **Клиенты**: как решается Client-Login для агентских аккаунтов.

**Что взять**: паттерн разделения `YANDEX_TOKEN` на `YANDEX_DIRECT_TOKEN`
и `YANDEX_METRIKA_TOKEN`, флаг `YANDEX_USE_SANDBOX`, структура инструментов
в `direct_*` / `metrika_*` namespace.

**Что НЕ копировать**: бизнес-логика минимальная, safety-слоя нет.

---

## 🏗️ Архитектурные референсы (Google Ads, но паттерны универсальны)

### [grantweston/google-ads-mcp-complete](https://github.com/grantweston/google-ads-mcp-complete)

**Что это**: полная реализация Google Ads API v21 через MCP, ~29 инструментов.

**Для каких milestones**:
- **M1.1** (tool registry): структура `tools_campaigns.py`, `tools_bidding.py`,
  `tools_keywords.py` — ровно тот разрез, который нам нужен.
- **Clients/base.py**: `error_handler.py` с retry-логикой под специфику
  ad-platform ошибок.

**Что взять**: способ разделения tools-по-домену, паттерн
`auth + error_handler + rate_limiter` как отдельные компоненты.

**Что адаптировать**: Google Ads использует gRPC, у нас HTTP — retry-логика
похожа, но детали другие.

### [johnoconnor0/google-ads-mcp](https://github.com/johnoconnor0/google-ads-mcp)

**Что это**: MCP-сервер с планом на 161 инструмент, многоуровневая
инфраструктура (auth / cache / config / error / response / query_optimizer).

**Для каких milestones**:
- **M1.1**: домен-менеджеры (`campaign_manager`, `ad_group_manager`,
  `bidding_strategy_manager`, `conversion_manager`) — карта того, что нужно
  для полного покрытия платформы.

**Что взять**: `cache_manager.py` (Redis/memory) — нам понадобится для
справочников. `query_optimizer.py` — идея кэшировать и батчить запросы.

### [google-marketing-solutions/google_ads_mcp](https://github.com/google-marketing-solutions/google_ads_mcp)

**Что это**: официальный Google-made MCP. Read-only (намеренно — для
безопасности).

**Для каких milestones**:
- **M3.2**: решение про read-only vs `--allow-write` флаг.

**Важный инсайт**: Google поставляет только read-only. Это осознанное
решение — write-операции через MCP считаются слишком рискованными без
дополнительных guardrails. Мы идём дальше, но с обязательными kill-switches.

### [AgriciDaniel/claude-ads](https://github.com/AgriciDaniel/claude-ads)

**Что это**: Claude Code skill на 250+ проверок кабинета (Google/Meta/
YouTube/LinkedIn/TikTok/Microsoft/Apple). Weighted scoring, industry
templates.

**Для каких milestones**:
- **M2.0** (kill-switches): готовая библиотека проверок — смотри какие
  симптомы ищут в реальных кабинетах.
- **M6** (reporting): industry-specific бенчмарки (e-comm, SaaS, local
  business) — портируй под русскоязычные ниши.
- **System prompt (M1.3)**: там хорошие промпты для «медиа-байерской
  роли».

**Что взять**: структура rubric-файлов (`scoring-rubrics/`) — как
взвешивать нарушения. Industry templates — подсмотри как разбиты
бенчмарки по вертикалям.

---

## 🤖 Агент-луп и human-in-the-loop

### [AlessandroAnnini/agent-loop](https://github.com/AlessandroAnnini/agent-loop)

**Что это**: компактный агент с флагом `--safe`, MCP-интеграцией, защитой
от зацикливаний.

**Для каких milestones**:
- **M1.2** (agent loop): готовый референс на цикл tool use с detection
  of alternating patterns — защита от ситуации когда модель в тупике
  делает одно и то же.

**Что взять**:
- Max iterations hard limit (у них 20 по умолчанию — нам подойдёт).
- Argument-aware repetition detection — блокируем `bash(cmd=X)` 5 раз
  подряд с одинаковым X, но не блокируем разные команды.
- `--safe` mode как первый флаг — мы зовём его `--interactive-confirm`.

### [shareAI-lab/learn-claude-code](https://github.com/shareAI-lab/learn-claude-code)

**Что это**: учебный репозиторий, реверс-инжинирит Claude Code по слоям
(s01–s12): agent loop, subagent isolation, context compression, task
system, permission governance.

**Для каких milestones**:
- **M1.2**: s01 "the agent loop" — минимальный корректный цикл в 30
  строк. Базовый скелет нашего `agent/loop.py`.
- **M1.3** (prompts): s05 "on-demand skill loading" — как не грузить
  все знания в context сразу.
- **M2** (safety): s12 permission governance.

**Инсайт из README**: «The harness doesn't make Claude smart. Claude is
already smart. The harness gives Claude hands, eyes, and a workspace».
Не тратим время на переизобретение промпт-техник — фокус на инструментах
и guardrails.

### [deepeshBodh/human-in-loop](https://github.com/deepeshBodh/human-in-loop)

**Что это**: Claude Code плагин с DAG-workflow для spec-first разработки.
Несколько специализированных агентов (requirements analyst, devil's
advocate, principal architect, staff engineer, QA engineer).

**Для каких milestones**:
- **M2.2** (plan → confirm → execute): идея «devil's advocate» — второй
  агент специально ищет дыры в плане перед подачей человеку.

**Что взять на будущее**: разделение агента на персоны
(`planner` → `reviewer` → `executor`) — это даёт качественно лучшее
решение для rare/крупных операций. Сейчас overkill, но в версии 0.2
точно пригодится.

---

## 🛡️ Safety / guardrails

### [Agentic PPC Campaign Management (статья)](https://www.digitalapplied.com/blog/agentic-ppc-campaign-management-autonomous-bid)

**Что это**: статья-манифест про 7 обязательных kill-switches и staged
rollout для автономных PPC-агентов.

**Для каких milestones**:
- **M2.0**: все 7 kill-switches взяты прямо оттуда.
- **M2.5**: staged rollout — 4 этапа за 30 дней.
- **M2.6**: Quality Score as protected metric.

**Читать обязательно**. Это ТЗ описывает _что_, статья — _почему_. Без
этой рамки агент будет делать то же самое, что делают все автоматы
«настрой ставки под KPI» — через неделю потеряешь QS и CTR, а восстановление
— месяцы.

### [jshorwitz/awesome-agentic-advertising](https://github.com/jshorwitz/awesome-agentic-advertising)

**Что это**: курированный список всего в области AI-advertising: MCP-серверы,
протоколы (AdCP, A2A), фреймворки, статьи.

**Для каких milestones**: общий контекст. Пройдись по списку, когда
появляются вопросы «а как это решают другие».

**Важный пункт**: **AdCP (Ad Context Protocol)** — open standard для
advertising automation поверх MCP и A2A. Сейчас v3.0 beta. В обозримом
будущем стандартизирует весь buy/sell цикл рекламы. Стоит следить, но
пока не имплементировать — сыро.

---

## 📊 Статистика и A/B-тесты

### [ericosiu/ai-marketing-skills](https://github.com/ericosiu/ai-marketing-skills)

**Что это**: набор Claude Code skills для маркетинг-операций. Рабочие
скрипты для реальных задач, не демо.

**Для каких milestones**:
- **M5** (A/B-тесты): `growth-engine/` использует **bootstrap confidence
  intervals** и **Mann-Whitney U test** — именно те методы, которые
  нужны для метрик с несимметричным распределением (CPA, ROAS).
- **M4.3** (минус-слова): подход к ICP-learning — динамическое
  переписывание критериев на основе реальных данных. Можем применить
  к списку минус-слов: если новые запросы с определёнными хвостами
  стабильно не конвертятся — агент сам добавляет их в минуса.

**Что взять напрямую**: `experiment-engine.py` — paste & adapt под
Директовские метрики.

---

## 🚀 На будущее (версия 0.2+)

### [abandini/autonomous-marketing-agent](https://github.com/abandini/autonomous-marketing-agent)

**Что это**: end-to-end маркетинг-система с knowledge graph, RL-loop,
operator interface для human oversight.

**Когда смотреть**: когда будем проектировать версию 0.2 с multi-channel
(Директ + VK Ads + Google Ads). Revenue Knowledge Graph (связь кампания →
сегмент → канал → стратегия) — хорошая идея на потом.

### [wshobson/agents](https://github.com/wshobson/agents)

**Что это**: огромный набор Claude Code плагинов, включая `protect-mcp`
(Cedar policy enforcement + Ed25519 signed receipts).

**Когда смотреть**: когда дойдём до мультитенантности (один инстанс
агента управляет несколькими клиентскими кабинетами). Cedar policies
позволят описывать «агент X может управлять только кабинетом клиента Y
в бюджете до Z руб/день» декларативно.

### [agentscope-ai/HiClaw](https://github.com/agentscope-ai/HiClaw)

**Что это**: multi-agent OS с Matrix-rooms для human-in-the-loop
координации.

**Когда смотреть**: если будем строить агентство-версию (несколько
медиа-байеров + агенты, общая комната с задачами и аудитом).

---

## Как использовать этот документ в Claude Code

Когда ставишь задачу Claude Code на конкретный milestone:

```
Делаем M2.0 — kill-switches. Прежде чем писать код:
1. Открой docs/PRIOR_ART.md, секцию "Agentic PPC Campaign Management".
2. Прочитай статью по ссылке (fetch URL).
3. Выпиши 7 kill-switches и как их проверяет автор.
4. Только после этого — приступай к реализации.
```

Это сильно повышает качество решения vs «пиши с нуля по моему описанию».
