from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
from app.config import settings
from app.database import connect_db, disconnect_db
from app.routes import webhook, auth, automation, dashboard, plans, admin, contacts, billing
from app.security import limiter
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
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
    lifespan=lifespan,
    docs_url="/docs" if settings.ENVIRONMENT.lower() == "development" else None,
    redoc_url="/redoc" if settings.ENVIRONMENT.lower() == "development" else None,
    openapi_url="/openapi.json" if settings.ENVIRONMENT.lower() == "development" else None,
)

app.state.limiter = limiter


@app.exception_handler(RateLimitExceeded)
async def rate_limit_handler(request, exc):
    return JSONResponse(status_code=429, content={"detail": "Too many requests, slow down"})


@app.middleware("http")
async def add_security_headers(request, call_next):
    response = await call_next(request)
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
    if request.url.path in {"/docs", "/redoc", "/openapi.json"}:
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline' 'unsafe-eval' https://cdn.jsdelivr.net; "
            "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
            "img-src 'self' data: https://fastapi.tiangolo.com; "
            "font-src 'self' data: https://cdn.jsdelivr.net; "
            "connect-src 'self'"
        )
    else:
        response.headers["Content-Security-Policy"] = "default-src 'self'"
    return response

environment = settings.ENVIRONMENT.lower()
if environment == "production" and not settings.FRONTEND_URL:
    raise RuntimeError("FRONTEND_URL must be set in production")

if environment == "production" and settings.FRONTEND_URL:
    configured_admin_origins = [
        origin.strip()
        for origin in settings.ADMIN_FRONTEND_URLS.split(",")
        if origin.strip()
    ]
    derived_admin_origin = (
        settings.FRONTEND_URL
        .replace("https://pinguru.me", "https://admin.pinguru.me")
        .replace("http://pinguru.me", "http://admin.pinguru.me")
    )
    allowed_origins = list({settings.FRONTEND_URL, derived_admin_origin, *configured_admin_origins})
else:
    allowed_origins = [
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ]

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.add_middleware(SlowAPIMiddleware)

app.include_router(webhook.router,    prefix="/webhook",    tags=["Webhook"])
app.include_router(auth.router,       prefix="/auth",       tags=["Auth"])
app.include_router(automation.router, prefix="/automation", tags=["Automation"])
app.include_router(dashboard.router,  prefix="/dashboard",  tags=["Dashboard"])
app.include_router(plans.router,      prefix="/plans",      tags=["Plans"])
app.include_router(billing.router,    prefix="/billing",    tags=["Billing"])
app.include_router(admin.router,      prefix="/admin",      tags=["Admin"])
app.include_router(contacts.router,   prefix="/contacts",   tags=["Contacts"])

@app.get("/")
async def root():
    return {"status": "PinGuru is live 🚀", "version": "1.0.0"}

@app.get("/health")
async def health():
    return {"status": "ok"}
