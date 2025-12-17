from datetime import datetime
from pydantic import BaseModel, Field

class TaskCreate(BaseModel):
    name: str
    payload: dict = Field(default_factory=dict)
    priority: int = Field(default=5, ge=1, le=10)
    run_at: datetime
    max_retries: int = Field(default=3, ge=0, le=20)

class TaskOut(BaseModel):
    id: int
    name: str
    payload: dict
    status: str
    priority: int
    attempts: int
    max_retries: int
    run_at: datetime
    last_error: str | None

    class Config:
        from_attributes = True
