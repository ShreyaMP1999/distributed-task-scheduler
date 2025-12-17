import time
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from app.db import engine

def wait_for_db(max_seconds: int = 30, interval: float = 1.0) -> None:
    """Block until Postgres accepts connections (or timeout)."""
    deadline = time.time() + max_seconds
    last_err = None

    while time.time() < deadline:
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            return
        except OperationalError as e:
            last_err = e
            time.sleep(interval)

    raise RuntimeError(f"DB not ready after {max_seconds}s. Last error: {last_err}")
