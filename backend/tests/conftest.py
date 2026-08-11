"""
DocuFlow - Test Configuration & Shared Fixtures
Sets up test environment with mocked external dependencies.
"""
import asyncio
import os
import pytest
from unittest.mock import AsyncMock, patch

# Force test environment before any app imports
os.environ.update({
    "ENV": "development",
    "DEBUG": "true",
    "RATE_LIMIT_ENABLED": "false",
    "METRICS_ENABLED": "false",
    "JWT_SECRET": "test-secret-key-for-testing-only-32chars!!",
    "SECRET_KEY": "test-app-secret-key-for-testing-only-32!!",
    "LOG_FORMAT": "text",
    "LOG_LEVEL": "WARNING",
})

# In-memory stores for test isolation
_fake_store: dict = {}
_fake_counters: dict = {}


async def _fake_cache_result(doc_id, result, ttl=None):
    _fake_store[f"r:{doc_id}"] = result

async def _fake_get_cached(doc_id):
    return _fake_store.get(f"r:{doc_id}")

async def _fake_delete_result(doc_id):
    _fake_store.pop(f"r:{doc_id}", None)

async def _fake_increment(key, amount=1):
    _fake_counters[key] = _fake_counters.get(key, 0) + amount

async def _fake_get_stat(key):
    return _fake_counters.get(key, 0)

async def _fake_get_avg():
    return 1840.0

async def _fake_record_time(ms):
    pass

async def _fake_rate_limit(key, limit, window=60):
    return True, limit

async def _fake_redis_health():
    return True


# Apply all patches at module level (before app imports in tests)
patch("app.core.redis_client.cache_result",        _fake_cache_result).start()
patch("app.core.redis_client.get_cached_result",   _fake_get_cached).start()
patch("app.core.redis_client.delete_result",       _fake_delete_result).start()
patch("app.core.redis_client.increment_stat",      _fake_increment).start()
patch("app.core.redis_client.get_stat",            _fake_get_stat).start()
patch("app.core.redis_client.get_avg_latency",     _fake_get_avg).start()
patch("app.core.redis_client.record_processing_time", _fake_record_time).start()
patch("app.core.redis_client.check_rate_limit",    _fake_rate_limit).start()
patch("app.core.redis_client.check_redis_health",  _fake_redis_health).start()
patch("app.core.redis_client.init_redis",          AsyncMock()).start()
patch("app.core.database.check_db_health",         AsyncMock(return_value=True)).start()
patch("app.core.database.init_db",                 AsyncMock()).start()
patch("app.services.queue.queue_service.connect",  AsyncMock()).start()


@pytest.fixture(scope="session")
def event_loop():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    yield loop
    loop.close()


@pytest.fixture(autouse=True)
def reset_between_tests():
    """Clean shared state between tests."""
    _fake_store.clear()
    _fake_counters.clear()
    try:
        from app.api.v2 import _results_cache
        _results_cache.clear()
    except Exception:
        pass
    yield
