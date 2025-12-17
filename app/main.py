from fastapi import FastAPI, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import select

from app.db import SessionLocal, engine, Base
from app.models import Task
from app.schemas import TaskCreate, TaskOut
from app.wait_for_db import wait_for_db

app = FastAPI(title="Distributed Task Scheduler (Simple + DLQ + Sweeper)")

# ✅ Wait for Postgres, then create tables
wait_for_db(max_seconds=60)
Base.metadata.create_all(bind=engine)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

@app.post("/tasks", response_model=TaskOut)
def create_task(payload: TaskCreate, db: Session = Depends(get_db)):
    t = Task(
        name=payload.name,
        payload=payload.payload,
        priority=payload.priority,
        run_at=payload.run_at,
        max_retries=payload.max_retries,
    )
    db.add(t)
    db.commit()
    db.refresh(t)
    return t

@app.get("/tasks/{task_id}", response_model=TaskOut)
def get_task(task_id: int, db: Session = Depends(get_db)):
    t = db.execute(select(Task).where(Task.id == task_id)).scalar_one_or_none()
    if not t:
        raise HTTPException(status_code=404, detail="Task not found")
    return t
