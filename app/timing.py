from datetime import datetime, timezone


def now_utc():
    return datetime.now(timezone.utc)


def backoff_seconds(attempts: int, base: int = 2, cap: int = 60) -> int:
    return min(cap, base * (2 ** max(0, attempts - 1)))
