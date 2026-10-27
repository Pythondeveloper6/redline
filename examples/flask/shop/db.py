from __future__ import annotations

import os
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

DB_FILE = Path(os.environ.get("SHOP_DB", Path(__file__).resolve().parent.parent / "shop.db"))

sync_engine = create_engine(f"sqlite:///{DB_FILE}")
Session = sessionmaker(sync_engine, expire_on_commit=False)
