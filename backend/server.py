from dotenv import load_dotenv
from pathlib import Path

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / '.env')

import os
import logging
from fastapi import FastAPI
from starlette.middleware.cors import CORSMiddleware

from db import db, client
from auth import router as auth_router, seed_admin
from store_routes import router as store_router, seed_categories
from admin_routes import router as admin_router
from storage import init_storage

app = FastAPI(title="Team Sport API")

app.include_router(auth_router)
app.include_router(store_router)
app.include_router(admin_router)

frontend_url = os.environ.get("FRONTEND_URL", "http://localhost:3000")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[frontend_url, "http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


@app.on_event("startup")
async def startup():
    await db.users.create_index("email", unique=True)
    await db.users.create_index("username", unique=True, sparse=True)
    await db.products.create_index("id", unique=True)
    await db.products.create_index([("active", 1), ("created_at", -1)])
    await db.orders.create_index("order_code", unique=True)
    await db.orders.create_index("user_id")
    await db.orders.create_index("customer.phone")
    await db.categories.create_index("id", unique=True)
    await db.login_attempts.create_index("identifier")
    await db.files.create_index("storage_path")
    await seed_admin()
    await seed_categories()
    try:
        init_storage()
        logger.info("Object storage initialized")
    except Exception as e:
        logger.error(f"Storage init failed: {e}")


@app.on_event("shutdown")
async def shutdown_db_client():
    client.close()
