"""CacheProxy — FastAPI приложение для кеширования чатов в PostgreSQL."""

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

from app.config import settings
from app.database import Base, engine
from app.routes import admin, context, messages, sessions
from app.services.archiver import start_archiver


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Create tables and preload embedding model on startup."""
    Base.metadata.create_all(bind=engine)

    # Migrate existing tables — add columns that may not exist yet
    from sqlalchemy import inspect
    from sqlalchemy import text as sa_text

    inspector = inspect(engine)
    cols = {c["name"] for c in inspector.get_columns("sessions")}
    with engine.connect() as conn:
        if "status" not in cols:
            conn.execute(
                sa_text(
                    "ALTER TABLE sessions ADD COLUMN status VARCHAR(16) NOT NULL DEFAULT 'active'"
                )
            )
            conn.commit()
            print("✓ Added sessions.status column")
        if "archived_at" not in cols:
            conn.execute(
                sa_text("ALTER TABLE sessions ADD COLUMN archived_at TIMESTAMPTZ")
            )
            conn.commit()
            print("✓ Added sessions.archived_at column")

    # Preload embedding model — first request loads ~252MB model
    # Doing it here avoids 10-15s cold start on first message
    from app.services import EmbeddingService

    # Ensure cache directory exists
    cache_dir = EmbeddingService._get_cache_dir()
    Path(cache_dir).mkdir(parents=True, exist_ok=True)
    try:
        EmbeddingService._get_model()
        print(f"✓ Embedding model loaded (cache: {cache_dir})")
    except Exception as e:
        print(f"⚠ Embedding model load failed: {e}")

    # Start background auto-archiver
    start_archiver()

    # Auto-open browser
    try:
        import webbrowser

        webbrowser.open("http://127.0.0.1:8100/")
    except Exception:
        pass

    yield


app = FastAPI(
    title="CacheProxy",
    description="Прокси-сервис для кеширования контекста чатов в PostgreSQL",
    version="0.1.0",
    lifespan=lifespan,
)

# CORS — разрешаем любые источники (dev mode)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Подключаем роуты
app.include_router(sessions.router)
app.include_router(messages.router)
app.include_router(context.router)
app.include_router(admin.router)


@app.get("/")
def index():
    """Serve the web UI."""
    return FileResponse(Path(__file__).parent / "static" / "index.html")


@app.get("/health")
def health():
    """Health check."""
    return {"status": "ok", "service": "CacheProxy", "version": "0.1.0"}


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    """Catch-all exception handler."""
    return JSONResponse(
        status_code=500,
        content={"detail": f"Internal error: {exc}"},
    )
