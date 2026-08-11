
"""DocuFlow - Async Database with connection pooling"""
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.pool import NullPool, StaticPool
from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)

_is_sqlite = settings.DATABASE_URL.startswith("sqlite")

if _is_sqlite:
    engine = create_async_engine(
        settings.DATABASE_URL,
        echo=settings.DEBUG,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
else:
    engine = create_async_engine(
        settings.DATABASE_URL,
        echo=settings.DEBUG,
        pool_pre_ping=True,
        pool_size=settings.DB_POOL_SIZE,
        max_overflow=settings.DB_MAX_OVERFLOW,
        pool_timeout=30,
        pool_recycle=3600,
    )

AsyncSessionLocal = async_sessionmaker(
    engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
)


class Base(DeclarativeBase):
    pass


async def init_db():
    # Import models so they register on Base.metadata before create_all
    import app.models.models  # noqa: F401

    def _create_tables(connection):
        # Create each table individually so a pre-existing index on one
        # table doesn't prevent the remaining tables from being created.
        for table in Base.metadata.sorted_tables:
            try:
                table.create(connection, checkfirst=True)
            except Exception:
                pass  # table/index already exists — safe to skip

    async with engine.begin() as conn:
        await conn.run_sync(_create_tables)
    logger.info("Database initialized")


async def get_db():
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


async def check_db_health() -> bool:
    try:
        async with AsyncSessionLocal() as session:
            await session.execute(__import__("sqlalchemy").text("SELECT 1"))
        return True
    except Exception as e:
        logger.error(f"DB health check failed: {e}")
        return False
