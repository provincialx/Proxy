# CacheProxy

A tool for storing chat context from any project in PostgreSQL.
Works exclusively with the [Zed](https://zed.dev) editor.

CacheProxy operates as an **MCP server** — it provides your AI assistant with
persistent memory across sessions via the Model Context Protocol.

It also compresses and summarizes chats to reduce storage load and save tokens
when searching through conversation history.

---

## Tech Stack

| Component | Technology |
|-----------|-----------|
| API | FastAPI |
| Database | PostgreSQL 18 |
| ORM | SQLAlchemy 2.0 |
| Migrations | Alembic |
| Embeddings | fastembed + ONNX (optional) |
| Semantic Search | Cosine similarity (numpy) + keyword ILIKE |
| Zed sync | sync_agent.py — reads Zed SQLite databases |

---

## Architecture

Everything runs **locally on a single machine** (Windows).

```
┌──────────────────────────────────────────────────────┐
│  Local Machine (Windows)                              │
│  ┌──────────────┐    ┌────────────────────┐           │
│  │  PostgreSQL   │    │  CacheProxy API    │           │
│  │  (port 5432)  │◄───│  uvicorn :8200     │           │
│  │  DB: proxy    │    │  FastAPI + SQLAlch.│           │
│  └──────────────┘    └────────┬───────────┘           │
│                               │                       │
│                    ┌──────────▼───────────┐           │
│                    │  Web UI (index.html) │           │
│                    └──────────────────────┘           │
│         ▲                          ▲                  │
│         │ HTTP API                  │ Browser         │
│         │ (sync_agent, MCP)         │                 │
│  ┌──────┴───────────────────────────┴──────┐          │
│  │  Zed (SQLite: threads.db + db.sqlite)    │          │
│  │  sync_agent.py reads & syncs            │          │
│  └─────────────────────────────────────────┘          │
└──────────────────────────────────────────────────────┘
```

### API Endpoints

```
POST   /sessions                          — create a chat session
GET    /sessions?project=...&status=...    — list sessions
GET    /sessions/{id}                      — session details
POST   /sessions/{id}/archive              — archive a session
DELETE /sessions/{id}                      — delete a session

POST   /messages                           — add a message (auto-saves context)
GET    /messages/{session_id}?limit=10000   — session messages

POST   /context                            — manually save context
GET    /context/search?query=...           — semantic search
DELETE /context/{id}                       — delete context

### Admin (prefix /admin)
GET    /admin/projects                     — list projects from Zed
POST   /admin/sync?projects=...            — run sync
POST   /admin/daemon/start?interval=60     — start sync daemon
POST   /admin/daemon/stop                  — stop daemon
GET    /admin/daemon/status                — daemon status
POST   /admin/db-reset                     — clear database and cache
GET    /admin/zed-threads                  — raw threads from Zed
GET    /admin/zed-threads/{id}/messages    — raw thread messages
POST   /admin/zed-threads/{id}/clean       — clean thread garbage
POST   /admin/sanitize                     — remove tool messages
POST   /admin/resummarize                  — re-summarize sessions
```

---

## Installation & Setup

### ⚠️ Before You Start — Backup Your Zed Data

CacheProxy reads your local Zed databases directly. While it never writes to them,
it's good practice to keep backups.

Copy these folders to a safe location (e.g. `D:\Backups\Zed\`):

```powershell
# 1. Thread content (chat messages, zstd-compressed)
copy "$env:LOCALAPPDATA\Zed\threads\threads.db" "D:\Backups\Zed\threads.db"

# 2. Global database (workspace state, sessions)
copy "$env:LOCALAPPDATA\Zed\db\0-global" "D:\Backups\Zed\0-global" /E

# 3. Stable database (sidebar threads, folder paths)
copy "$env:LOCALAPPDATA\Zed\db\0-stable" "D:\Backups\Zed\0-stable" /E
```

To restore later, simply copy them back.

### Requirements
- Windows 10+
- PostgreSQL 18 (local)
- Python 3.11+

### 1. Install PostgreSQL

Download from [postgresql.org](https://www.postgresql.org/download/windows/).
During installation, set the superuser password.

Create database and user (via pgAdmin or psql):
```sql
CREATE USER proxyuser WITH PASSWORD '1';
CREATE DATABASE proxy OWNER proxyuser;
GRANT ALL PRIVILEGES ON DATABASE proxy TO proxyuser;
```

### 2. Clone & Configure

```powershell
cd D:\Projects
git clone <url> CacheProxy
cd CacheProxy

# Virtual environment
python -m venv .venv
.\.venv\Scripts\pip.exe install -r requirements.txt

# Configure .env
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
"@ | % { [System.IO.File]::WriteAllText("$pwd\.env", $_) }

# Run migrations
.\.venv\Scripts\alembic.exe upgrade head
```

### 3. Run

```powershell
cd D:\Projects\CacheProxy
$env:PYTHONPATH = "D:\Projects\CacheProxy"
.\.venv\Scripts\python.exe app\main.py
```

The server starts at `http://127.0.0.1:8200/`, browser opens automatically.

### 4. Sync Chats from Zed

Click **🔄 Sync selected** on the **Sessions** tab.

Sync process:
- Reads threads from `%LOCALAPPDATA%/Zed/threads/threads.db`
- Compares message count with existing PostgreSQL session
- Sends **only new messages** (incremental, no full re-upload)
- If no session exists — creates one and sends all messages
- Archives session after sync

---

## Web Interface

After startup, the browser opens at `http://127.0.0.1:8200/`.

The language can be switched between **EN** and **RU** using the dropdown in the top-right corner.

### Tabs

#### 🔍 Search
Semantic search across all context entries. Type a query, optionally filter by project. Results show relevance score and a snippet around the matching text. Click ✕ to delete individual context entries.

- **Query field** — enter what you're looking for
- **Project filter** — narrow results to a specific project
- **Results** — sorted by relevance, full content with scroll

#### 📁 Sessions
Main tab — lists all dialogs from PostgreSQL. Displays session title, project, message count, status, and last update date.

**Features:**
- **Filter by project** — dropdown with all available projects
- **Filter by status** — All / Active 🟢 / Archived 📦
- **Click a session** — opens all messages (newest first, limit 10000)
- **📦 Archive button** — marks session as archived, consolidates into context
- **✕ Delete button** — removes session and all its messages from PostgreSQL

**Control Panel (below session list):**

| Button | Action |
|--------|--------|
| 🔄 **Sync selected** | Reads threads from Zed's `threads.db` and sends new/changed data to PostgreSQL. **Incremental** — only new messages are uploaded. Select which projects to sync via checkboxes above. |
| 🧹 **Clean selected** | Removes garbage (Thinking blocks, Mentions, Images) from Zed's `threads.db` for selected projects. This cleans up the raw data that Zed stores. ToolUse blocks are preserved. **Important:** Close Zed before cleaning — otherwise Zed's cache will restore the garbage on next restart. 🔄 **Sync selected** works regardless of whether Zed is open. |
| ▶️ **Daemon** | Starts/stops a background thread that runs Sync automatically every 60 seconds. |
| 📝 **Resummarize** | Rebuilds context entries for **active** sessions. Concatenates all session messages into one or more searchable context chunks (max ~40000 chars each). Run this after Sync to make new data searchable. |
| 🗑️ **DB Reset** | **Danger zone.** Deletes ALL sessions, messages, and contexts from PostgreSQL. Model cache is also cleared. **Chats are NOT lost** — they will be restored from Zed on the next Sync. Located in a separate row to prevent accidental clicks. |

#### 🗄️ Zed DB
Raw thread data directly from Zed's `threads.db` — unfiltered, with all block types preserved (Thinking, ToolUse, Mentions, Images, etc.). Useful for inspecting what Zed actually stores vs what gets into PostgreSQL.

**Features:**
- **Filter by project** and **status** (Active/Archived/Broken)
- **Click a thread** — shows all raw messages with block type indicators
- **🧹 Clean from junk button** — removes Thinking/Mention/Image blocks from `threads.db` for that specific thread, keeping ToolUse intact

#### 🧵 Threads
A table view of the `sidebar_threads` table from Zed's `db.sqlite`. Shows metadata about all known threads. Useful for debugging.

**Features:**
- **Sort by any column** — click column header
- **Filter by title/folder/project** — text input

### Messages Display

- In both Sessions and Zed DB tabs, messages are shown **newest first**
- Message limit per session — **10000** (full chat visible)
- In Sessions — only clean text (Thinking/ToolUse filtered out)
- In Zed DB — full raw data with all block types

---

## Content Filtering

### During Sync (sync_agent.py)
When parsing threads in `_parse_messages()`:
- **Text** — preserved
- **ToolUse** — preserved (required for DeepSeek API contract)
- **ToolResult** — skipped (can break DeepSeek API without tool_calls parent)
- **Thinking** — skipped (noise for search)
- **Mention** — skipped (noise)
- **Image** — skipped (noise)

### Manual Clean in Zed DB
The **🧹 Clean from junk** button removes Thinking, Mention, Image, image_url blocks from `threads.db`. ToolUse blocks are preserved (required for API). `tool_results` are nullified if content is empty.

Empty messages (no content and no tool_results) are skipped.

---

## MCP Server (Model Context Protocol)

The MCP server gives Zed's AI assistant direct access to context from PostgreSQL.
Runs as a separate process, communicates via stdio using the MCP protocol.

### Tools

| Tool | Description | Parameters |
|------|-------------|-----------|
| `search_context` | Hybrid search (ILIKE + cosine similarity) | `query` (req), `project`, `limit` |
| `get_context_detail` | Full context content by ID | `context_id` (req) |
| `get_session` | Session details + message snippets | `session_id` (req) |
| `list_sessions` | List sessions (no content) | `project`, `status`, `limit` |
| `get_recent_contexts` | Latest contexts without search | `project`, `limit` |

Skill examples in folder "Skills"

### Setup in Zed

In `~/.config/zed/settings.json`:

```json
{
  "context_servers": {
    "cacheproxy": {
      "enabled": true,
      "env": {},
      "command": "D:\\Projects\\CacheProxy\\.venv\\Scripts\\python.exe",
      "args": ["D:\\Projects\\CacheProxy\\mcp_server.py"],
    }
  },
```

### Requirements
- Install `mcp` package (`pip install mcp`)
- Set `DB_HOST` in `.env` (localhost for local DB or server IP)
- CacheProxy doesn't need to be running — MCP reads the DB directly

---

## Development

### Project Structure

```
CacheProxy/
├── app/
│   ├── main.py              # FastAPI + lifespan (migrations, auto-archiver)
│   ├── config.py            # pydantic-settings (from .env)
│   ├── database.py          # engine + session
│   ├── schemas.py           # Pydantic models
│   ├── models/
│   │   ├── session.py       # SQLAlchemy: sessions
│   │   ├── message.py       # SQLAlchemy: messages
│   │   └── context.py       # SQLAlchemy: contexts
│   ├── routes/
│   │   ├── sessions.py      # CRUD + archive
│   │   ├── messages.py      # CRUD (limit 10000)
│   │   ├── context.py       # CRUD + hybrid search
│   │   └── admin.py         # Sync, daemon, db-reset, raw threads, clean
│   ├── services/
│   │   ├── __init__.py      # EmbeddingService
│   │   ├── archiver.py      # Background auto-archiver
│   │   └── summarizer.py    # LLM summarization
│   └── static/
│       └── index.html       # Web UI
├── sync_agent.py            # Thread sync from Zed
├── mcp_server.py            # MCP server for Zed
├── alembic/                 # Migrations
├── .env                     # Configuration
└── requirements.txt
```

### Sync Agent

`sync_agent.py` runs on the client machine (where Zed is installed). It reads Zed's local SQLite databases and sends data to CacheProxy via the API.

**Logic:**
1. Reads threads from `sidebar_threads` (db.sqlite) and content from `threads` (threads.db)
2. Parses JSON, extracts messages, filters garbage
3. Checks if a session already exists in PostgreSQL (by title+project)
4. If session exists — compares message count. If threads.db has more — sends only new ones
5. If no session — creates one and sends all messages
6. Archives the session

**Important:** Sync **never writes** to threads.db — read-only. Use **🧹 Clean selected** on the UI to clean garbage from threads.db.

**From code:**
```python
from sync_agent import run_sync
result = run_sync()  # {synced: int, total: int, errors: list}
```

**Daemon:**
```bash
python sync_agent.py daemon --interval 60
```

---

## Configuration (.env)

```
db_host=localhost
db_port=5432
db_name=proxy
db_user=proxyuser
db_password=1
app_host=0.0.0.0
app_port=8200
debug=false

# LLM summarization (optional)
llm_api_key=
llm_base_url=https://api.openai.com/v1
llm_model=gpt-4o-mini
llm_enabled=false
```

---

## Known Limitations

- **fastembed** not installed — semantic search (`search_context`) won't work. Install: `.\.venv\Scripts\pip.exe install fastembed`
- **LLM summarization** requires an API key (OpenAI-compatible). Without it, archiver works without summarization
- After restoring `threads.db` from a backup, a full re-sync may be needed

---

<br>
<hr>
<br>

# CacheProxy

Инструмент для хранения контекста любого проекта в базе данных PostgreSQL.
Работает только с редактором [Zed](https://zed.dev).

CacheProxy работает как **MCP-сервер** — он предоставляет вашему AI-ассистенту
постоянную память между сессиями через протокол Model Context Protocol.

Также сжимает и суммаризирует чаты, снижая нагрузку на хранилище и экономя
токены при поиске по истории диалогов.

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
│  │  Zed (SQLite: threads.db + db.sqlite)    │          │
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

POST   /messages                           — добавить сообщение (авто-контекст)
GET    /messages/{session_id}?limit=10000   — сообщения сессии

POST   /context                            — сохранить контекст вручную
GET    /context/search?query=...           — семантический поиск
DELETE /context/{id}                       — удалить контекст

### Admin (префикс /admin)
GET    /admin/projects                     — список проектов из Zed
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

### ⚠️ Перед началом — сделайте бэкап данных Zed

CacheProxy читает локальные базы Zed напрямую. Хотя он никогда в них не пишет,
рекомендуется сохранить резервную копию.

Скопируйте следующие папки в надёжное место (например `D:\Backups\Zed\`):

```powershell
# 1. Содержимое чатов (сообщения, сжатые zstd)
copy "$env:LOCALAPPDATA\Zed\threads\threads.db" "D:\Backups\Zed\threads.db"

# 2. Глобальная база (состояние рабочего пространства, сессии)
copy "$env:LOCALAPPDATA\Zed\db\0-global" "D:\Backups\Zed\0-global" /E

# 3. Стабильная база (сайдбар, пути к проектам)
copy "$env:LOCALAPPDATA\Zed\db\0-stable" "D:\Backups\Zed\0-stable" /E
```

Для восстановления просто скопируйте файлы обратно.

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
"@ | % { [System.IO.File]::WriteAllText("$pwd\.env", $_) }

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
- Досылает **только новые сообщения** (инкрементально, без перезаписи)
- Если сессии ещё нет — создаёт и отправляет все сообщения
- Архивирует сессию после синхронизации

---

## Веб-интерфейс

Язык интерфейса можно переключить между **EN** и **RU** через выпадающий список в правом верхнем углу.

### Вкладки

#### 🔍 Поиск
Семантический поиск по всем контекстам. Введите запрос, опционально выберите проект для фильтрации. Результаты показывают процент совпадения и текст вокруг найденного фрагмента. ✕ удаляет отдельную запись контекста.

- **Поле запроса** — введите что ищете
- **Фильтр по проекту** — выберите конкретный проект
- **Результаты** — отсортированы по релевантности, полный контент со скроллом

#### 📁 Сессии
Основная вкладка — список всех диалогов из PostgreSQL. Отображает название, проект, количество сообщений, статус и дату обновления.

**Возможности:**
- **Фильтр по проекту** — выпадающий список всех проектов
- **Фильтр по статусу** — Все / Активные 🟢 / Архивные 📦
- **Клик по сессии** — открывает все сообщения (новые сверху, до 10000)
- **📦 Архивация** — помечает сессию как архивную, консолидирует в контекст
- **✕ Удаление** — удаляет сессию и все её сообщения из PostgreSQL

**Панель управления (под списком сессий):**

| Кнопка | Действие |
|--------|----------|
| 🔄 **Sync выбранные** | Читает треды из `threads.db` Zed и отправляет новые/изменённые данные в PostgreSQL. **Инкрементально** — только новые сообщения. Выберите проекты для синхронизации через чекбоксы. |
| 🧹 **Очистить выбранные** | Удаляет мусор (Thinking блоки, Mentions, Images) из `threads.db` Zed для выбранных проектов. Очищает сырые данные. ToolUse блоки сохраняются. **Важно:** Закройте Zed перед очисткой — иначе кеш Zed восстановит мусор при следующем запуске. 🔄 **Sync выбранные** работает независимо от того, открыт Zed или нет. |
| ▶️ **Демон** | Запускает/останавливает фоновый поток, который автоматически выполняет Sync каждые 60 секунд. |
| 📝 **Ресаммари** | Перестраивает контексты для **активных** сессий. Склеивает все сообщения сессии в один или несколько контекстов (до ~40000 символов каждый). Запускайте после Sync, чтобы новые данные стали доступны для поиска. |
| 🗑️ **DB Reset** | **Опасно.** Удаляет ВСЕ сессии, сообщения и контексты из PostgreSQL. Кеш модели эмбеддингов тоже очищается. **Чаты НЕ теряются** — они восстановятся из Zed при следующей синхронизации. Вынесен в отдельную строку, чтобы не нажать случайно. |

#### 🗄️ Zed DB
Сырые данные тредов напрямую из `threads.db` Zed — без фильтрации, со всеми типами блоков (Thinking, ToolUse, Mentions, Images и т.д.). Полезно для сравнения того, что реально хранит Zed, с тем что попадает в PostgreSQL.

**Возможности:**
- **Фильтр по проекту** и **статусу** (Активные/Архивные/С ошибками)
- **Клик по треду** — показывает все сырые сообщения с индикаторами типов блоков
- **🧹 Очистить от мусора** — удаляет Thinking/Mention/Image блоки из `threads.db` для этого треда, сохраняя ToolUse

#### 🧵 Threads
Табличное представление таблицы `sidebar_threads` из `db.sqlite` Zed. Показывает метаданные всех известных тредов. Полезно для отладки.

**Возможности:**
- **Сортировка по колонкам** — клик по заголовку
- **Фильтр** — по названию/папке/проекту

### Отображение сообщений

- В обеих вкладках (Сессии и Zed DB) сообщения показываются **от новых к старым**
- Лимит сообщений на сессию — **10000** (весь чат виден)
- В Сессиях — только чистый текст (Thinking/ToolUse отфильтрованы)
- В Zed DB — полные сырые данные со всеми блоками

---

## Фильтрация контента

### При синхронизации (sync_agent.py)
При парсинге треда в `_parse_messages()`:
- **Text** — сохраняется
- **ToolUse** — сохраняется (нужен для API-контракта DeepSeek)
- **ToolResult** — пропускается (может ломать API DeepSeek без tool_calls)
- **Thinking** — пропускается (шум)
- **Mention** — пропускается (шум)
- **Image** — пропускается (шум)

### Ручная очистка в Zed DB
Кнопка **🧹 Очистить от мусора** удаляет блоки Thinking, Mention, Image, image_url из `threads.db`. ToolUse сохраняется. `tool_results` обнуляются если контент пустой.

Пустые сообщения (без контента и tool_results) пропускаются.

---

## MCP-сервер (Model Context Protocol)

MCP-сервер даёт AI-ассистенту Zed прямой доступ к контексту из PostgreSQL.
Запускается как отдельный процесс, общается через stdio по протоколу MCP.

### Инструменты

| Инструмент | Описание | Параметры |
|-----------|----------|-----------|
| `search_context` | Гибридный поиск (ILIKE + cosine similarity) | `query` (обяз), `project`, `limit` |
| `get_context_detail` | Полный контент контекста по ID | `context_id` (обяз) |
| `get_session` | Детали сессии + сниппеты сообщений | `session_id` (обяз) |
| `list_sessions` | Список сессий (без контента) | `project`, `status`, `limit` |
| `get_recent_contexts` | Последние контексты без поиска | `project`, `limit` |

Примеры скилов в папке "Skills"

### Настройка в Zed

В `~/.config/zed/settings.json`:

```json
{
  "context_servers": {
    "cacheproxy": {
      "enabled": true,
      "env": {},
      "command": "D:\\Projects\\CacheProxy\\.venv\\Scripts\\python.exe",
      "args": ["D:\\Projects\\CacheProxy\\mcp_server.py"],
    }
  },
```

### Требования
- Установлен пакет `mcp` (`pip install mcp`)
- В `.env` указан `DB_HOST` (localhost для локальной БД или IP сервера)
- CacheProxy не обязательно запущен — MCP читает БД напрямую

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
│   │   ├── messages.py      # CRUD (лимит 10000)
│   │   ├── context.py       # CRUD + гибридный поиск
│   │   └── admin.py         # Sync, daemon, db-reset, сырые треды, очистка
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

`sync_agent.py` работает на клиенте (где установлен Zed). Читает локальные SQLite базы Zed и отправляет данные в CacheProxy через API.

**Логика:**
1. Читает треды из `sidebar_threads` (db.sqlite) и контент из `threads` (threads.db)
2. Парсит JSON, извлекает сообщения, фильтрует мусор
3. Проверяет, существует ли уже сессия в PostgreSQL (по названию+проекту)
4. Если сессия есть — сравнивает количество сообщений. Если в threads.db больше — досылает только новые
5. Если сессии нет — создаёт новую и отправляет все сообщения
6. Архивирует сессию

**Важно:** Sync **никогда не пишет** в threads.db — только читает. Для очистки от мусора используйте **🧹 Очистить выбранные** в интерфейсе.

**Из кода:**
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
- После восстановления `threads.db` из бэкапа может потребоваться полная пересинхронизация
