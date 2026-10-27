from __future__ import annotations

import os
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import sessionmaker

DB_FILE = Path(os.environ.get("SHOP_DB", Path(__file__).resolve().parent.parent / "shop.db"))

async_engine = create_async_engine(f"sqlite+aiosqlite:///{DB_FILE}")
sync_engine = create_engine(f"sqlite:///{DB_FILE}")

AsyncSession = async_sessionmaker(async_engine, expire_on_commit=False)
SyncSession = sessionmaker(sync_engine, expire_on_commit=False)
