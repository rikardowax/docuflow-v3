"""
DocuFlow - Background Tasks (APScheduler)
Scheduled: GDPR purge, stats aggregation, DLQ replay, health checks.
"""
import asyncio
import logging
from datetime import datetime, timezone, timedelta
from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)


class BackgroundTaskManager:
    """Manages all scheduled background tasks."""

    def __init__(self):
        self._tasks = []
        self._running = False

    async def start(self):
        """Start all background tasks."""
        self._running = True
        self._tasks = [
            asyncio.create_task(self._gdpr_purge_loop()),
            asyncio.create_task(self._stats_aggregation_loop()),
            asyncio.create_task(self._dlq_monitor_loop()),
            asyncio.create_task(self._health_check_loop()),
        ]
        logger.info(f"Started {len(self._tasks)} background tasks")

    async def stop(self):
        """Gracefully stop all tasks."""
        self._running = False
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        logger.info("Background tasks stopped")

    async def _gdpr_purge_loop(self):
        """Run GDPR document purge every hour."""
        while self._running:
            try:
                await asyncio.sleep(3600)
                await self._run_gdpr_purge()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"GDPR purge error: {e}")

    async def _run_gdpr_purge(self):
        """Delete documents past their retention deadline."""
        from app.services.storage import storage_service
        # In production: query DB for documents where purge_at < now()
        # then delete from storage and update DB record
        now = datetime.now(timezone.utc)
        logger.info(f"GDPR purge run at {now.isoformat()}")
        # Pseudocode for production:
        # async with get_db() as db:
        #     expired = await db.execute(
        #         select(DocumentRecord).where(
        #             DocumentRecord.purge_at < now,
        #             DocumentRecord.storage_key.isnot(None)
        #         )
        #     )
        #     for doc in expired.scalars():
        #         await storage_service.delete(doc.storage_key)
        #         doc.storage_key = None
        #         doc.status = ProcessingStatus.EXPIRED
        #     await db.commit()

    async def _stats_aggregation_loop(self):
        """Aggregate stats every 5 minutes."""
        while self._running:
            try:
                await asyncio.sleep(300)
                await self._aggregate_stats()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Stats aggregation error: {e}")

    async def _aggregate_stats(self):
        """Aggregate and persist stats to Redis."""
        try:
            from app.core.redis_client import get_stat, get_avg_latency
            total = await get_stat("processed_total")
            avg   = await get_avg_latency()
            logger.info(f"Stats: processed={total}, avg_latency={avg:.0f}ms")
        except Exception as e:
            logger.warning(f"Stats aggregation skipped: {e}")

    async def _dlq_monitor_loop(self):
        """Monitor Dead Letter Queue every 10 minutes."""
        while self._running:
            try:
                await asyncio.sleep(600)
                await self._check_dlq()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"DLQ monitor error: {e}")

    async def _check_dlq(self):
        """Alert if DLQ is growing."""
        try:
            from app.services.queue import queue_service
            depth = queue_service.get_dlq_depth()
            if depth > 10:
                logger.warning(f"DLQ depth alert: {depth} messages in DLQ")
                # In production: send alert to Slack/PagerDuty
        except Exception as e:
            logger.warning(f"DLQ check skipped: {e}")

    async def _health_check_loop(self):
        """Internal health check every 30 seconds."""
        while self._running:
            try:
                await asyncio.sleep(30)
                await self._internal_health_check()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Health check error: {e}")

    async def _internal_health_check(self):
        """Verify all critical dependencies are reachable."""
        try:
            from app.core.redis_client import check_redis_health
            from app.core.database import check_db_health
            redis_ok = await check_redis_health()
            db_ok    = await check_db_health()
            if not redis_ok:
                logger.error("HEALTH: Redis is DOWN")
            if not db_ok:
                logger.error("HEALTH: Database is DOWN")
        except Exception as e:
            logger.warning(f"Internal health check failed: {e}")


task_manager = BackgroundTaskManager()
