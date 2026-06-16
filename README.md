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
| Семантический поиск | Cosine similarity (numpy) |

---

## Архитектура

```
POST /sessions                     — создать сессию чата
GET  /sessions?project=...         — список сессий (с фильтром по проекту)
GET  /sessions/{id}                — детали сессии
DELETE /sessions/{id}              — удалить сессию

POST /messages                           — добавить сообщение (авто-сохранение контекста)
GET  /messages/{session_id}              — сообщения сессии

POST /context                                    — сохранить контекст вручную (авто-эмбеддинг)
GET  /context/search?query=...&project=...       — семантический поиск (с фильтром по проекту)
DELETE /context/{id}                              — удалить контекст
```

---

## Модели данных

### sessions
| Поле | Тип | Описание |
|------|-----|----------|
| id | UUID | Первичный ключ |
| title | VARCHAR(255) | Название сессии |
| project | VARCHAR(255) | Идентификатор проекта (репозиторий/путь) |
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

### Модель
- **`paraphrase-multilingual-MiniLM-L12-v2`** (384d)
- Мультиязычная (~50 языков, включая русский)
- ONNX Runtime (CPU), ~252MB на диске
- Загрузка модели ~12с, эмбеддинг ~0.01с на текст

### Как работает
1. При `POST /context` эмбеддинг генерируется автоматически из `content` + `keywords`
2. При `GET /context/search` запрос эмбеддится, ищется cosine similarity со всеми сохранёнными контекстами
3. Результаты сортируются по убыванию score

### Примеры поиска

| Запрос | Топ-результат | Score |
|--------|---------------|-------|
| «подвеска для бельгийской трассы» | Трасса Спа | 0.66 |
| «настройки для городской трассы» | Монако | 0.46 |
| «дождь и резина» | Дождевая резина | 0.36 |

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
2. Скачивается и кэшируется модель эмбеддингов (~252MB) — потребуется ~10-15с
3. API готов к работе

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

После запуска открой **http://127.0.0.1:8100** — три вкладки:

| Вкладка | Что делает |
|---------|-----------|
| 💬 **Чат** | Написать сообщение → автомат сохраняется в messages + contexts (с эмбеддингом) |
| 🔍 **Поиск** | Семантический поиск по всем контекстам, фильтр по проекту, удаление лишнего ✕ |
| 📁 **Сессии** | Список диалогов с фильтром по проекту, удаление сессии целиком ✕ |

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
│   ├── main.py              # FastAPI app
│   ├── config.py            # pydantic-settings
│   ├── database.py          # engine + session
│   ├── schemas.py           # Pydantic модели
│   ├── models/
│   │   ├── __init__.py
│   │   ├── session.py       # SQLAlchemy: sessions
│   │   ├── message.py       # SQLAlchemy: messages
│   │   └── context.py       # SQLAlchemy: contexts
│   ├── routes/
│   │   ├── __init__.py
│   │   ├── sessions.py      # CRUD сессий
│   │   ├── messages.py      # CRUD сообщений
│   │   └── context.py       # CRUD + семантический поиск
│   └── services/
│       └── __init__.py      # EmbeddingService
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
- [ ] Интеграция с Zed (extension)
- [ ] Авто-суммаризация контекста через LLM
- [ ] pgvector вместо brute-force
- [ ] Batch-эмбеддинг для больших объёмов

---

## Changelog

### 2026-06-16

#### Исправлено
- **Модель эмбеддингов загружалась при первом запросе** (10-15с).
  Теперь предзагружается в `lifespan` при старте сервера.
- **Модель кешировалась во временную папку** `%TEMP%\zed-agent-terminal-*\fastembed_cache`,
  которая очищается между сессиями. Теперь кеш в `app/.model_cache` — постоянный.
- **При ошибке эмбеддинга сообщение не сохранялось**.
  Теперь сообщение сохраняется в любом случае, эмбеддинг опционален.
- **Два uvicorn-процесса на один порт** — убивали друг друга.

#### Добавлено
- **`GET /context/search?query=`** — пустой query возвращает последние контексты
  (сортировка по `created_at DESC`). Фильтр по `project` работает.
- **Кнопка отправки блокируется** на время запроса (`⏳ Отправка...`).
  Предотвращает двойные нажатия.
- **Авто-обновление списка сессий** после отправки сообщения.
- **Авто-загрузка контекстов** при переключении на вкладку поиска.

#### Технический долг
- Модель `paraphrase-multilingual-MiniLM-L12-v2` (252MB) скачивается
  однократно в `app/.model_cache`. При первом запуске — ~20-30с.

---

## Лицензия

MIT
