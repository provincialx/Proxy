# CacheProxy

Прокси-сервис для кеширования контекста чатов в PostgreSQL с семантическим поиском.

Хранит историю диалогов и позволяет находить релевантный контекст из предыдущих общений — чтобы новые чаты в Zed (или любом другом интерфейсе) видели всю нужную историю.

---

## Стек

| Компонент | Технология |
|-----------|-----------|
| API | FastAPI (async-ready) |
| База данных | PostgreSQL 18 |
| ORM | SQLAlchemy 2.0 |
| Миграции | Alembic |
| Эмбеддинги | fastembed + ONNX |
| Семантический поиск | Cosine similarity (numpy) + keyword ILIKE |
| Синхронизация с Zed | sync_agent.py — чтение SQLite БД Zed |

---

## Архитектура

```
POST /sessions                              — создать сессию чата
GET  /sessions?project=...&status=...        — список сессий (фильтр по проекту и статусу)
GET  /sessions/{id}                          — детали сессии
POST /sessions/{id}/archive                  — архивировать сессию (+ консолидация в контекст)
DELETE /sessions/{id}                        — удалить сессию

POST /messages                               — добавить сообщение (авто-сохранение контекста)
GET  /messages/{session_id}                  — сообщения сессии

POST /context                                — сохранить контекст вручную (авто-эмбеддинг)
GET  /context/search?query=...&project=...   — семантический поиск (с фильтром по проекту)
DELETE /context/{id}                          — удалить контекст

### Admin
```
GET  /admin/projects                         — список проектов из архивированных тредов Zed
POST /admin/sync?projects=...                — запустить синхронизацию (с фильтром по проектам)
POST /admin/daemon/start?interval=60         — запустить фоновый демон синхронизации
POST /admin/daemon/stop                      — остановить демон
GET  /admin/daemon/status                    — статус демона
POST /admin/db-reset                         — очистить БД, кеш модели и sent-маркеры
```

---

## Модели данных

### sessions
| Поле | Тип | Описание |
|------|-----|----------|
| id | UUID | Первичный ключ |
| title | VARCHAR(255) | Название сессии |
| project | VARCHAR(255) | Идентификатор проекта (репозиторий/путь) |
| status | VARCHAR(16) | `active` / `archived` |
| archived_at | TIMESTAMPTZ | Дата архивации |
| created_at | TIMESTAMPTZ | Дата создания |
| updated_at | TIMESTAMPTZ | Дата обновления |
| metadata | TEXT | JSON-метаданные |

### messages
| Поле | Тип | Описание |
|------|-----|----------|
| id | UUID | Первичный ключ |
| session_id | UUID | FK → sessions.id |
| role | VARCHAR(32) | user / assistant / system |
| content | TEXT | Текст сообщения |
| tokens_used | INTEGER | Примерное число токенов |
| created_at | TIMESTAMPTZ | Дата создания |

### contexts
| Поле | Тип | Описание |
|------|-----|----------|
| id | UUID | Первичный ключ |
| session_id | UUID | FK → sessions.id |
| summary | VARCHAR(500) | Краткое описание |
| content | TEXT | Текст контекста |
| keywords | VARCHAR(500) | Ключевые слова |
| embedding | DOUBLE PRECISION[] | 384-мерный вектор эмбеддинга |
| token_count | INTEGER | Число токенов |
| created_at | TIMESTAMPTZ | Дата создания |

---

## Семантический поиск

### Как работает
1. **Короткие запросы (1-3 слова)** — keyword search через ILIKE с транслитерацией.
   Термины расширяются через словарь: `сетап → setup, настройка, настройки` и т.д.
   Релевантность считается по плотности совпадений (точное слово > частичное).
2. **Длинные запросы (4+ слов)** — semantic search (fastembed + cosine similarity)
   с keyword-бустом для точных совпадений.
3. Результаты сортируются по убыванию score.

### Модель
- **`paraphrase-multilingual-MiniLM-L12-v2`** (384d)
- Мультиязычная (~50 языков, включая русский)
- ONNX Runtime (CPU), ~252MB на диске
- Загрузка модели ~12с, эмбеддинг ~0.01с на текст

### Словарь терминов (iRacing)
| Запрос | Расширение |
|--------|-----------|
| сетап | setup, настройка, настройки |
| подвеска | suspension, подвески |
| шин, резин | tire, tyre |
| двигател, мотор | engine |
| аэродинамик, прижим | aero, downforce, крыло |

---

## Архивация сессий

При архивации (ручной или авто):
1. Сессия помечается `status=archived`, проставляется `archived_at`
2. Все сообщения консолидируются в один `Context` с эмбеддингом
3. Архивная сессия находится через семантический поиск

**Ручная** — `POST /sessions/{id}/archive` или кнопка в UI

**Автоматическая** — фоновый поток каждые 60с проверяет сессии без обновлений >7 дней и архивирует их

---

## Sync Agent — синхронизация с Zed

`sync_agent.py` читает локальные SQLite базы Zed и отправляет архивированные треды в CacheProxy.

### Откуда читает

| База | Путь | Содержимое |
|------|------|-----------|
| sidebar | `%LOCALAPPDATA%\Zed\db\0-stable\db.sqlite` | Метаданные тредов, флаг `archived` |
| threads | `%LOCALAPPDATA%\Zed\threads\threads.db` | Контент тредов (zstd-сжатый JSON) |

### Как работает
1. Находит треды с `archived=1` в sidebar DB
2. Достаёт контент из threads DB (распаковывает zstd)
3. Парсит JSON — извлекает каждое сообщение с ролью (user/assistant)
4. Создаёт сессию в CacheProxy
5. Отправляет сообщения одно за другим
6. Архивирует сессию в CacheProxy

### Использование

```powershell
# Показать схему БД Zed
python sync_agent.py discover

# Один проход — отправить все архивированные треды
python sync_agent.py sync

# Фоновый режим — проверяет новые архивы каждые 60с
python sync_agent.py daemon
```

### Программный вызов

```python
from sync_agent import run_sync, get_available_projects

# Список проектов
projects = get_available_projects()

# Синхронизация всех тредов
result = run_sync()

# Синхронизация только выбранных проектов
result = run_sync(projects=["CacheProxy", "iRacing-Analyzer"])
# result = {synced: int, total: int, errors: list}
```

### Фильтрация контента

При парсинге тредов автоматически отфильтровываются:
- `<thinking>...</thinking>` — размышления модели
- `{"Thinking": {...}}` — JSON-structured thinking
- `[Tool: name] {...}` — вызовы инструментов
- `{"Mention": {...}}` — упоминания файлов
- `{"Image": {...}}` — блоки изображений

Фильтрация применяется как в sync_agent, так и на уровне API (`app/utils.py`).

---

## Установка и запуск

### Требования
- Python 3.12+
- PostgreSQL 17+
- Зависимости из `requirements.txt`

### Установка

```powershell
# Клонировать
cd D:\Projects\CacheProxy

# Создать виртуальное окружение
python -m venv .venv
.venv\Scripts\activate

# Установить зависимости
pip install -r requirements.txt

# Настроить .env
# DB_HOST=localhost
# DB_PORT=5432
# DB_NAME=Proxy
# DB_USER=ProxyUser
# DB_PASSWORD=1
# APP_PORT=8100
```

### Запуск

```powershell
uvicorn app.main:app --host 127.0.0.1 --port 8100
```

При первом запуске:
1. Автоматически создаются таблицы (lifespan)
2. Добавляются колонки `status` и `archived_at` в существующую таблицу sessions
3. Скачивается и кэшируется модель эмбеддингов (~252MB) — потребуется ~10-15с
4. Стартует фоновый авто-архиватор
5. API готов к работе

### Миграции

```powershell
# Создать новую миграцию
alembic revision --autogenerate -m "описание"

# Применить миграции
alembic upgrade head

# Откатить
alembic downgrade -1
```

---

## Веб-интерфейс

После запуска браузер открывается автоматически. Две вкладки:

| Вкладка | Что делает |
|---------|-----------|
| 🔍 **Поиск** | Семантический поиск по всем контекстам, фильтр по проекту, удаление лишнего ✕ |
| 📁 **Сессии** (дефолтная) | Список диалогов с фильтром (проект + статус), архивация, удаление. Панель **Управление**: синхронизация с Zed, демон, DB Reset |

### API документация

- **Swagger UI**: http://127.0.0.1:8100/docs
- **ReDoc**: http://127.0.0.1:8100/redoc

### Health check

```powershell
curl http://127.0.0.1:8100/health
# → {"status":"ok","service":"CacheProxy","version":"0.1.0"}
```

### Пример: отправить сообщение — контекст сохранится автоматом

```python
import httpx

base = "http://127.0.0.1:8100"

# 1. Создать сессию с проектом
sid = httpx.post(f"{base}/sessions", json={
    "title": "Настройки подвески",
    "project": "iRacing-Analyzer"
}).json()["id"]

# 2. Отправить сообщение → контекст + эмбеддинг сохранятся автоматически
httpx.post(f"{base}/messages", json={
    "session_id": sid,
    "role": "user",
    "content": "Для трассы Спа нужны мягкие передние амортизаторы"
})

# 3. Семантический поиск (можно с фильтром по проекту)
httpx.get(f"{base}/context/search", params={
    "query": "бельгийская трасса подвеска",
    "project": "iRacing-Analyzer",
    "limit": 5
}).json()
```

### Удаление

```powershell
DELETE /sessions/{id}    # удалить сессию со всем содержимым
DELETE /context/{id}     # удалить конкретный контекст
```

---

## Разработка

### Структура проекта

```
CacheProxy/
├── app/
│   ├── __init__.py
│   ├── main.py              # FastAPI app + lifespan (миграции, авто-архиватор)
│   ├── config.py            # pydantic-settings
│   ├── database.py          # engine + session
│   ├── schemas.py           # Pydantic модели
│   ├── models/
│   │   ├── __init__.py
│   │   ├── session.py       # SQLAlchemy: sessions (+ status, archived_at)
│   │   ├── message.py       # SQLAlchemy: messages
│   │   └── context.py       # SQLAlchemy: contexts
│   ├── routes/
│   │   ├── __init__.py
│   │   ├── sessions.py      # CRUD + archive endpoint
│   │   ├── messages.py      # CRUD сообщений
│   │   ├── context.py       # CRUD + гибридный поиск (keyword + semantic)
│   │   └── admin.py         # Управление: sync, daemon, db-reset
│   └── services/
│       ├── __init__.py      # EmbeddingService
│       └── archiver.py      # Фоновый авто-архиватор
├── sync_agent.py            # Синхронизация архивных тредов из Zed
├── app/utils.py             # Фильтрация контента (thinking, mentions, tool calls)
├── alembic/                 # Миграции
├── alembic.ini
├── .env                     # Конфигурация
└── requirements.txt
```

---

## Roadmap

- [x] База данных (PostgreSQL + SQLAlchemy)
- [x] CRUD для сессий, сообщений, контекста
- [x] Семантический поиск (fastembed + cosine similarity)
- [x] Веб-интерфейс (чат, поиск, управление сессиями)
- [x] Авто-сохранение контекста при отправке сообщения
- [x] Фильтрация по проекту
- [x] Удаление сессий и контекстов из UI
- [x] Просмотр последних контекстов без поиска
- [x] Предзагрузка модели эмбеддингов при старте
- [x] Graceful degradation при ошибках эмбеддинга
- [x] Гибридный поиск (keyword ILIKE + semantic)
- [x] Архивация сессий (ручная + авто-архиватор)
- [x] Sync Agent — выгрузка архивных тредов из Zed
- [x] Фильтрация мусора (thinking, mentions, images, tool calls)
- [x] Admin API — sync, daemon, db-reset, выбор проектов
- [x] Авто-открытие браузера при старте
- [ ] MCP-сервер для интеграции с AI ассистентом Zed
- [ ] Авто-суммаризация контекста через LLM
- [ ] pgvector вместо brute-force
- [ ] Batch-эмбеддинг для больших объёмов

---

## Changelog

### 2026-06-17

#### Добавлено
- **Admin API** — эндпоинты `/admin/sync`, `/admin/daemon/*`, `/admin/db-reset`, `/admin/projects`
- **UI: панель управления** — выбор проектов для синхронизации, запуск/остановка демона, DB Reset с защитой от дурака
- **UI: вкладка «Сессии» по дефолту**, вкладка «Чат» удалена
- **Фильтрация контента** — `strip_thinking()` в `app/utils.py`:
  - XML `<thinking>...</thinking>`
  - JSON `{"Thinking": {...}}`, `{"Mention": {...}}`, `{"Image": {...}}`
  - Tool calls `[Tool: name] {...}`
- **`sync_agent.py`**: программный API (`run_sync()`, `get_available_projects()`), фильтр по проектам, пропуск ToolUse/Thinking/Mention/Image при парсинге
- **`/admin/db-reset`** — очищает БД + model cache + sent-маркеры
- **Авто-открытие браузера** при старте `http://127.0.0.1:8100`

#### Добавлено
- **Sync Agent** (`sync_agent.py`) — чтение архивированных тредов из SQLite БД Zed
  (sidebar `0-stable/db.sqlite` + контент `threads/threads.db` с zstd-распаковкой)
- **Архивация сессий** — `status`/`archived_at` поля, `POST /sessions/{id}/archive`,
  кнопка в UI, фильтр `?status=active|archived`
- **Фоновый авто-архиватор** — каждые 60с проверяет сессии без обновлений >7 дней
- **Гибридный поиск** — для коротких запросов (1-3 слова) keyword ILIKE,
  для длинных semantic + keyword boost. Словарь терминов iRacing.
- **Legacy-миграция** — `ALTER TABLE` для существующих БД при старте

#### Исправлено
- **Модель эмбеддингов загружалась при первом запросе** (10-15с).
  Теперь предзагружается в `lifespan` при старте сервера.
- **Модель кешировалась во временную папку** `%TEMP%\zed-agent-terminal-*\fastembed_cache`,
  которая очищается между сессиями. Теперь кеш в `app/.model_cache` — постоянный.
- **При ошибке эмбеддинга сообщение не сохранялось**.
  Теперь сообщение сохраняется в любом случае, эмбеддинг опционален.
- **Два uvicorn-процесса на один порт** — убивали друг друга.
- **`[user]`/`[assistant]` префикс в embedded контенте** — шумел в эмбеддинге.
  Теперь эмбеддится чистый текст сообщения.
- **Короткие запросы искали мусор** — семантическая модель не работает на одном слове.
  Теперь keyword search для коротких запросов.

---

## Лицензия

MIT
