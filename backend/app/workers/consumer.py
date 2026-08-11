"""DocuFlow - Background worker consumer."""
import asyncio
import logging
from app.services.queue import queue_service

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

async def handle_message(payload: dict):
    try:
        logger.info(f"Processing: {payload.get('url')} batch={payload.get('batch_id')}")
    except Exception as e:
        logger.error(f"Worker error: {e}")

async def main():
    await queue_service.connect()
    logger.info("Worker started, waiting for jobs...")
    await queue_service.consume(handle_message, max_workers=50)

if __name__ == "__main__":
    asyncio.run(main())
