from datetime import datetime
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

class TaskCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    payload: dict = Field(default_factory=dict)
    priority: int = Field(default=5, ge=1, le=10)
    run_at: AwareDatetime
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

    model_config = ConfigDict(from_attributes=True)
