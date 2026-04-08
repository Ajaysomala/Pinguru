from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
from app.database import connect_db, disconnect_db
from app.routes import webhook, auth, automation, dashboard, plans
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

@asynccontextmanager
async def lifespan(app: FastAPI):
    await connect_db()
    logger.info("✅ PinGuru backend started — MongoDB connected")
    yield
    await disconnect_db()
    logger.info("🛑 PinGuru backend shutting down")

app = FastAPI(
    title="PinGuru API",
    description="Instagram DM Automation SaaS Backend",
    version="1.0.0",
    lifespan=lifespan
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # restrict in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(webhook.router,    prefix="/webhook",    tags=["Webhook"])
app.include_router(auth.router,       prefix="/auth",       tags=["Auth"])
app.include_router(automation.router, prefix="/automation", tags=["Automation"])
app.include_router(dashboard.router,  prefix="/dashboard",  tags=["Dashboard"])
app.include_router(plans.router,      prefix="/plans",      tags=["Plans"])

@app.get("/")
async def root():
    return {"status": "PinGuru is live 🚀", "version": "1.0.0"}

@app.get("/health")
async def health():
    return {"status": "ok"}
