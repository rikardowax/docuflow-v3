"""
DocuFlow - Production Queue Service
RabbitMQ / Kafka with DLQ, exponential backoff retry, and priority queues.
"""
import asyncio
import json
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Awaitable

from app.core.config import settings
from app.core.logging import get_logger
from app.core.redis_client import increment_stat, record_processing_time

logger = get_logger(__name__)

# In-memory fallback for dev
_dev_queue: asyncio.PriorityQueue = asyncio.PriorityQueue()
_dlq: list = []
_active_workers: int = 0
_processed_count: int = 0
_failed_count: int = 0

PRIORITY_MAP = {"high": 0, "normal": 5, "low": 9}


class QueueService:
    """Unified queue abstraction: RabbitMQ on-premise, Kafka cloud."""

    def __init__(self):
        self.queue_type = settings.QUEUE_TYPE
        self._connection = None
        self._channel = None
        self._producer = None

    async def connect(self):
        if self.queue_type == "rabbitmq":
            await self._connect_rabbitmq()
        elif self.queue_type == "kafka":
            await self._connect_kafka()
        else:
            logger.info("Queue: in-memory mode (dev)")

    async def _connect_rabbitmq(self):
        try:
            import aio_pika
            self._connection = await aio_pika.connect_robust(
                settings.RABBITMQ_URL,
                heartbeat=60,
                connection_attempts=5,
                retry_delay=3,
            )
            self._channel = await self._connection.channel()
            await self._channel.set_qos(prefetch_count=settings.MAX_PARALLEL_WORKERS)
            # Main queue
            await self._channel.declare_queue(
                "docuflow.process",
                durable=True,
                arguments={"x-dead-letter-exchange": "docuflow.dlx",
                           "x-message-ttl": settings.WORKER_TIMEOUT * 1000}
            )
            # DLQ
            dlx = await self._channel.declare_exchange("docuflow.dlx", aio_pika.ExchangeType.DIRECT)
            dlq = await self._channel.declare_queue("docuflow.dlq", durable=True)
            await dlq.bind(dlx, "docuflow.process")
            # Priority queue
            await self._channel.declare_queue(
                "docuflow.process.high",
                durable=True,
                arguments={"x-max-priority": 10}
            )
            logger.info("RabbitMQ connected")
        except Exception as e:
            logger.warning(f"RabbitMQ unavailable ({e}), falling back to in-memory queue")

    async def _connect_kafka(self):
        try:
            from aiokafka import AIOKafkaProducer
            self._producer = AIOKafkaProducer(
                bootstrap_servers=settings.KAFKA_BOOTSTRAP_SERVERS,
                value_serializer=lambda v: json.dumps(v).encode(),
                acks="all",
                enable_idempotence=True,
                max_in_flight_requests_per_connection=1,
            )
            await self._producer.start()
            logger.info("Kafka producer connected")
        except Exception as e:
            logger.warning(f"Kafka unavailable ({e}), falling back to in-memory")

    async def publish(self, message: dict, priority: str = "normal") -> str:
        job_id = str(uuid.uuid4())
        payload = {
            "job_id": job_id,
            "priority": priority,
            "retry_count": 0,
            "enqueued_at": datetime.now(timezone.utc).isoformat(),
            "payload": message,
        }
        if self._channel:
            try:
                import aio_pika
                queue_name = "docuflow.process.high" if priority == "high" else "docuflow.process"
                await self._channel.default_exchange.publish(
                    aio_pika.Message(
                        body=json.dumps(payload).encode(),
                        delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                        priority=PRIORITY_MAP.get(priority, 5),
                    ),
                    routing_key=queue_name,
                )
                return job_id
            except Exception as e:
                logger.error(f"RabbitMQ publish error: {e}")

        if self._producer:
            try:
                await self._producer.send_and_wait(settings.KAFKA_TOPIC, payload)
                return job_id
            except Exception as e:
                logger.error(f"Kafka publish error: {e}")

        # Fallback: in-memory
        prio_val = PRIORITY_MAP.get(priority, 5)
        await _dev_queue.put((prio_val, time.time(), payload))
        return job_id

    async def consume(self, handler: Callable[[dict], Awaitable[None]], max_workers: int = None):
        workers = max_workers or settings.MAX_PARALLEL_WORKERS
        semaphore = asyncio.Semaphore(workers)

        if self._channel:
            await self._consume_rabbitmq(handler, semaphore)
        else:
            await self._consume_inmemory(handler, semaphore)

    async def _consume_inmemory(self, handler, semaphore):
        global _active_workers
        async def process_one(payload):
            global _active_workers, _processed_count, _failed_count
            async with semaphore:
                _active_workers += 1
                start = time.time()
                try:
                    await asyncio.wait_for(handler(payload["payload"]), timeout=settings.WORKER_TIMEOUT)
                    _processed_count += 1
                    elapsed = int((time.time() - start) * 1000)
                    await record_processing_time(elapsed)
                    await increment_stat("processed_total")
                except asyncio.TimeoutError:
                    logger.error(f"Worker timeout: {payload.get('job_id')}")
                    _failed_count += 1
                    await self._send_to_dlq(payload, "timeout")
                except Exception as e:
                    _failed_count += 1
                    retry = payload.get("retry_count", 0)
                    if retry < settings.QUEUE_MAX_RETRIES:
                        delay = settings.QUEUE_RETRY_DELAY * (2 ** retry)
                        payload["retry_count"] = retry + 1
                        logger.warning(f"Retrying job {payload.get('job_id')} in {delay}s (attempt {retry+1})")
                        await asyncio.sleep(delay)
                        prio = PRIORITY_MAP.get(payload.get("priority", "normal"), 5)
                        await _dev_queue.put((prio, time.time(), payload))
                    else:
                        logger.error(f"Job {payload.get('job_id')} sent to DLQ after {retry} retries")
                        await self._send_to_dlq(payload, str(e))
                finally:
                    _active_workers -= 1

        while True:
            try:
                _, _, payload = await asyncio.wait_for(_dev_queue.get(), timeout=1.0)
                asyncio.create_task(process_one(payload))
            except asyncio.TimeoutError:
                continue

    async def _consume_rabbitmq(self, handler, semaphore):
        queue = await self._channel.get_queue("docuflow.process")
        async with queue.iterator() as it:
            async for message in it:
                async with message.process(requeue=False):
                    payload = json.loads(message.body)
                    asyncio.create_task(self._handle_with_semaphore(handler, payload, semaphore))

    async def _handle_with_semaphore(self, handler, payload, semaphore):
        async with semaphore:
            try:
                await asyncio.wait_for(handler(payload["payload"]), timeout=settings.WORKER_TIMEOUT)
            except Exception as e:
                logger.error(f"Worker error: {e}")

    async def _send_to_dlq(self, payload: dict, reason: str):
        payload["dlq_reason"] = reason
        payload["dlq_at"] = datetime.now(timezone.utc).isoformat()
        _dlq.append(payload)
        logger.error(f"DLQ: {payload.get('job_id')} reason={reason}")

    def get_active_workers(self) -> int:
        return _active_workers

    def get_queue_depth(self) -> int:
        return _dev_queue.qsize()

    def get_dlq_depth(self) -> int:
        return len(_dlq)

    def get_stats(self) -> dict:
        return {
            "active_workers": _active_workers,
            "queue_depth": _dev_queue.qsize(),
            "dlq_depth": len(_dlq),
            "processed_total": _processed_count,
            "failed_total": _failed_count,
        }


class ProcessingOrchestrator:
    def __init__(self):
        from app.services.extraction import extraction_service
        from app.services.biometric import biometric_service
        from app.services.validation import validation_service, fuzzy_service
        from app.services.storage import storage_service
        self.extraction = extraction_service
        self.biometric  = biometric_service
        self.validation = validation_service
        self.fuzzy      = fuzzy_service
        self.storage    = storage_service

    async def process(self, doc_bytes: bytes, file_format: str,
                      template_config: dict, modules: list[str],
                      selfie_bytes: bytes = None,
                      reference_data: dict = None,
                      verso_bytes: bytes = None,
                      selfie_source: str = "upload") -> dict[str, Any]:
        start = time.time()
        result = {}
        try:
            if "extraction" in modules:
                # extraction.extract() already has its own internal timeout
                result["extraction"] = await self.extraction.extract(
                    doc_bytes, file_format, template_config, verso_bytes
                )
            if "biometric" in modules:
                result["biometric"] = await asyncio.wait_for(
                    self.biometric.verify(doc_bytes, selfie_bytes, source_type=selfie_source),
                    timeout=settings.FACE_DETECTION_TIMEOUT if hasattr(settings, "FACE_DETECTION_TIMEOUT") else 90
                )
            if "validation" in modules and "extraction" in result:
                result["validation"] = await self.validation.validate(
                    result["extraction"]["fields"], template_config
                )
            if "fuzzy" in modules and "extraction" in result and reference_data:
                result["fuzzy"] = await self.fuzzy.compare(
                    result["extraction"]["fields"], reference_data, template_config
                )
        except asyncio.TimeoutError as e:
            logger.error(f"Pipeline timeout: {e}")
            result["error"] = str(e)

        result["total_ms"] = int((time.time() - start) * 1000)
        result["global_decision"] = self._decide(result)
        return result

    def _decide(self, result: dict) -> str:
        bio = result.get("biometric", {})
        bio_decision = bio.get("decision", "")
        if bio_decision == "MISMATCH": return "REJECTED"
        if bio_decision == "NO_FACE_ON_SELFIE": return "REJECTED"
        if bio_decision == "MATCH_WITH_LIVENESS_WARNING": return "REVIEW"
        val = result.get("validation", {})
        if val and not val.get("passed") and val.get("rules_failed", 0) > 0:
            return "REVIEW"
        fuz = result.get("fuzzy", {})
        if fuz.get("overall") == "REJECTED": return "REJECTED"
        if fuz.get("overall") == "REVIEW":   return "REVIEW"
        return "VALIDATED"


queue_service    = QueueService()
orchestrator     = ProcessingOrchestrator()
