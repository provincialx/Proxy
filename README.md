# CacheProxy

Прокси-сервис для кеширования контекста чатов из Zed в PostgreSQL с семантическим поиском.

Хранит историю диалогов и позволяет находить релевантный контекст из предыдущих общений.

---

## Стек

| Компонент | Технология |
|-----------|-----------|
| API | FastAPI |
| База данных | PostgreSQL 18 |
| ORM | SQLAlchemy 2.0 |
| Миграции | Alembic |
| Эмбеддинги | fastembed + ONNX (опционально) |
| Семантический поиск | Cosine similarity (numpy) + keyword ILIKE |
| Синхронизация с Zed | sync_agent.py — чтение SQLite БД Zed |

---

## Архитектура

Всё работает **локально на одном ПК** (Windows).

```
┌──────────────────────────────────────────────────────┐
│  Локальный ПК (Windows)                               │
│  ┌──────────────┐    ┌────────────────────┐           │
│  │  PostgreSQL   │    │  CacheProxy API    │           │
│  │  (порт 5432)  │◄───│  uvicorn :8200     │           │
│  │  БД: proxy    │    │  FastAPI + SQLAlch.│           │
│  └──────────────┘    └────────┬───────────┘           │
│                               │                       │
│                    ┌──────────▼───────────┐           │
│                    │  Web UI (index.html) │           │
│                    └──────────────────────┘           │
│         ▲                          ▲                  │
│         │ HTTP API                  │ Браузер         │
│         │ (sync_agent, MCP)         │                 │
│  ┌──────┴───────────────────────────┴──────┐          │
│  │  Zed (SQLite БД: threads.db + db.sqlite) │          │
│  │  sync_agent.py — чтение и синхронизация  │          │
│  └─────────────────────────────────────────┘          │
└──────────────────────────────────────────────────────┘
```

### API

```
POST   /sessions                          — создать сессию чата
GET    /sessions?project=...&status=...    — список сессий
GET    /sessions/{id}                      — детали сессии
POST   /sessions/{id}/archive              — архивировать сессию
DELETE /sessions/{id}                      — удалить сессию

POST   /messages                           — добавить сообщение
GET    /messages/{session_id}?limit=10000   — сообщения сессии

POST   /context                            — сохранить контекст вручную
GET    /context/search?query=...           — семантический поиск
DELETE /context/{id}                       — удалить контекст

### Admin (префикс /admin)
GET    /admin/projects                     — список проектов
POST   /admin/sync?projects=...            — запустить синхронизацию
POST   /admin/daemon/start?interval=60     — запустить демон
POST   /admin/daemon/stop                  — остановить демон
GET    /admin/daemon/status                — статус демона
POST   /admin/db-reset                     — очистить БД и кеш
GET    /admin/zed-threads                  — сырые треды из Zed
GET    /admin/zed-threads/{id}/messages    — сырые сообщения треда
POST   /admin/zed-threads/{id}/clean       — очистить тред от мусора
POST   /admin/sanitize                     — удалить tool-сообщения
POST   /admin/resummarize                  — ре-суммаризация
```

---

## Установка и запуск

### Требования
- Windows 10+
- PostgreSQL 18 (локально)
- Python 3.11+

### 1. Установить PostgreSQL

Скачать с [postgresql.org](https://www.postgresql.org/download/windows/).
При установке указать пароль для суперпользователя.

Создать БД и пользователя (через pgAdmin или psql):
```sql
CREATE USER proxyuser WITH PASSWORD '1';
CREATE DATABASE proxy OWNER proxyuser;
GRANT ALL PRIVILEGES ON DATABASE proxy TO proxyuser;
```

### 2. Клонировать и настроить

```powershell
cd D:\Projects
git clone <url> CacheProxy
cd CacheProxy

# Виртуальное окружение
python -m venv .venv
.\.venv\Scripts\pip.exe install -r requirements.txt

# Настроить .env
@"
db_host=localhost
db_port=5432
db_name=proxy
db_user=proxyuser
db_password=1
app_host=0.0.0.0
app_port=8200
debug=false
llm_enabled=false
"@ | Out-File -Encoding UTF8 .env

# Миграции
.\.venv\Scripts\alembic.exe upgrade head
```

### 3. Запустить

```powershell
cd D:\Projects\CacheProxy
$env:PYTHONPATH = "D:\Projects\CacheProxy"
.\.venv\Scripts\python.exe app\main.py
```

Сервер запустится на `http://127.0.0.1:8200/`, браузер откроется автоматически.

### 4. Синхронизация чатов из Zed

После запуска нажми **🔄 Sync выбранные** на вкладке **Сессии**.

Синхронизация:
- Читает треды из `%LOCALAPPDATA%/Zed/threads/threads.db`
- Сравнивает количество сообщений с уже существующей сессией в PostgreSQL
- Досылает **только новые сообщения** (инкрементально), без удаления и пересоздания
- Если сессии ещё нет — создаёт и отправляет все сообщения
- Архивирует сессию после синхронизации

---

## Веб-интерфейс

После запуска браузер открывается на `http://127.0.0.1:8200/`.

| Вкладка | Что делает |
|---------|-----------|
| 📁 **Сессии** | Список диалогов из PostgreSQL. Фильтр по проекту/статусу. Архивация, удаление. Панель управления: 🔄 Sync, 🧹 Clean, ▶️ Демон, 📝 Ресаммари, 🗑️ DB Reset |
| 🗑️ **Junk** | Сырые треды из threads.db без фильтрации (Thinking, ToolUse, Image и т.д.). Кнопка **🧹 Очистить от мусора** — удаляет Thinking/Image/Mention блоки, чистит tool_results |
| 🧵 **Threads** | Таблица sidebar_threads из db.sqlite. Сортировка, фильтр, удаление записей |
| 🔍 **Поиск** | Семантический поиск по контекстам |

### Сообщения

- В обеих вкладках (Сессии и Junk) сообщения отображаются **от новых к старым**
- Лимит сообщений — **10000** (полный чат)
- В Сессиях — только чистый текст (Thinking/ToolUse отфильтрованы)
- В Junk — полные данные, все блоки сохранены

### Кнопки управления

- **🔄 Sync выбранные** — инкрементальная синхронизация: досылает только новые сообщения в PostgreSQL
- **🧹 Очистить выбранные** — удаляет мусор (Thinking, Mention, Image) из threads.db для выбранных проектов
- **▶️ Запустить демона** — фоновый демон, синхронизирует каждые 60с
- **📝 Ресаммари** — пересоздаёт контексты для **активных** сессий (склейка всего диалога в один Context для поиска)
- **🗑️ DB Reset** — полная очистка PostgreSQL (sessions, messages, contexts) и model cache. Список проектов не пропадает (читается из Zed)

---

## Фильтрация контента

### При синхронизации (sync_agent.py)
При парсинге треда в `_parse_messages()`:
- **Text** — сохраняется
- **ToolUse** — сохраняется (нужен для API-контракта DeepSeek)
- **ToolResult** — пропускается (может ломать API DeepSeek без tool_calls)
- **Thinking** — пропускается (шум для поиска)
- **Mention** — пропускается (шум)
- **Image** — пропускается (шум)

### При ручной очистке в Junk
Кнопка **🧹 Очистить от мусора** полностью удаляет блоки Thinking, Mention, Image, image_url из threads.db. ToolUse сохраняется (необходим для API). tool_results обнуляются если контент пустой.

Пустые сообщения (без контента и tool_results) пропускаются — не засоряют БД.

---

## MCP-сервер (Model Context Protocol)

MCP-сервер предоставляет AI-ассистенту Zed прямой доступ к контексту из PostgreSQL.
Запускается как отдельный процесс, общается через stdio по протоколу MCP.

### Инструменты

| Инструмент | Описание | Параметры |
|-----------|----------|----------|
| `search_context` | Гибридный поиск (ILIKE + cosine similarity) | `query` (req), `project`, `limit` |
| `get_context_detail` | Полный контент контекста по ID | `context_id` (req) |
| `get_session` | Детали сессии + сниппеты сообщений | `session_id` (req) |
| `list_sessions` | Список сессий (без контента) | `project`, `status`, `limit` |
| `get_recent_contexts` | Последние контексты без поиска | `project`, `limit` |

### Настройка в Zed

В `~/.config/zed/settings.json`:

```json
{
  "mcp": {
    "cacheproxy": {
      "command": "D:\\Projects\\CacheProxy\\.venv\\Scripts\\python.exe",
      "args": ["D:\\Projects\\CacheProxy\\mcp_server.py"]
    }
  }
}
```

### Требования
- Установлен пакет `mcp` (`pip install mcp`)
- В `.env` должен быть указан `DB_HOST` (localhost для локальной БД или IP сервера)
- CacheProxy не обязательно должен быть запущен — MCP-сервер читает БД напрямую

---

## Разработка

### Структура проекта

```
CacheProxy/
├── app/
│   ├── main.py              # FastAPI + lifespan (миграции, авто-архиватор)
│   ├── config.py            # pydantic-settings (из .env)
│   ├── database.py          # engine + session
│   ├── schemas.py           # Pydantic модели
│   ├── models/
│   │   ├── session.py       # SQLAlchemy: sessions
│   │   ├── message.py       # SQLAlchemy: messages
│   │   └── context.py       # SQLAlchemy: contexts
│   ├── routes/
│   │   ├── sessions.py      # CRUD + archive
│   │   ├── messages.py      # CRUD сообщений (лимит 10000)
│   │   ├── context.py       # CRUD + гибридный поиск
│   │   └── admin.py         # Sync, daemon, db-reset, Junk, очистка
│   ├── services/
│   │   ├── __init__.py      # EmbeddingService
│   │   ├── archiver.py      # Фоновый авто-архиватор
│   │   └── summarizer.py    # LLM-суммаризация
│   └── static/
│       └── index.html       # Веб-интерфейс
├── sync_agent.py            # Синхронизация тредов из Zed
├── mcp_server.py            # MCP-сервер для Zed
├── alembic/                 # Миграции
├── .env                     # Конфигурация
└── requirements.txt
```

### Sync Agent

`sync_agent.py` работает на клиенте (там где Zed).

**Логика:**
1. Читает треды из `sidebar_threads` (db.sqlite) и контент из `threads` (threads.db)
2. Парсит JSON, извлекает сообщения, фильтрует мусор
3. Проверяет, существует ли уже сессия в PostgreSQL (по title+project)
4. Если сессия есть — сравнивает количество сообщений. Если в threads.db больше — досылает только новые
5. Если сессии нет — создаёт и отправляет все сообщения
6. Архивирует сессию

**Важно:** Sync **не пишет** в threads.db — только читает. Для очистки от мусора есть отдельная кнопка **🧹 Очистить выбранные**.

**Вызов:**
```python
from sync_agent import run_sync
result = run_sync()  # {synced: int, total: int, errors: list}
```

**Демон:**
```bash
python sync_agent.py daemon --interval 60
```

---

## .env

```
db_host=localhost
db_port=5432
db_name=proxy
db_user=proxyuser
db_password=1
app_host=0.0.0.0
app_port=8200
debug=false

# LLM-суммаризация (опционально)
llm_api_key=
llm_base_url=https://api.openai.com/v1
llm_model=gpt-4o-mini
llm_enabled=false
```

---

## Известные ограничения

- **fastembed** не установлен — семантический поиск (`search_context`) не работает. Установка: `.\.venv\Scripts\pip.exe install fastembed`
- **LLM-суммаризация** требует API-ключ (OpenAI-совместимый). Без ключа архивация работает без суммаризации
- После восстановления threads.db из бэкапа может потребоваться полная пересинхронизация
