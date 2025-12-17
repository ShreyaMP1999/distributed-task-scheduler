import os

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+psycopg2://postgres:postgres@localhost:5432/tasks"
)
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
