from motor.motor_asyncio import AsyncIOMotorClient
from app.config import settings
import logging

logger = logging.getLogger(__name__)

class Database:
    client: AsyncIOMotorClient = None
    db = None

db_instance = Database()

async def connect_db():
    db_instance.client = AsyncIOMotorClient(settings.MONGODB_URI)
    db_instance.db = db_instance.client[settings.DB_NAME]
    logger.info(f"Connected to MongoDB: {settings.DB_NAME}")

async def disconnect_db():
    if db_instance.client:
        db_instance.client.close()

def get_db():
    return db_instance.db
