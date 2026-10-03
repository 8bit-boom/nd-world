"""Poll / list / cancel the AI tasks started with `X-ND-Background: 1` — see app/ai_background.py."""
from fastapi import APIRouter, HTTPException, Request

from .. import ai_background as _bg

router = APIRouter()


def _user(request: Request):
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(401, "Login required")
    return user


@router.get("/api/ai/tasks")
def list_my_tasks(request: Request):
    """The caller's recent tasks (running first, newest first) — what the page-corner tray offers back after a reload."""
    user = _user(request)
    return {"tasks": [t.view() for t in _bg.tasks_for(user.id)][:50]}


@router.get("/api/ai/tasks/{task_id}")
def get_task(task_id: str, request: Request):
    """Status of one task; once done, the route's own status code, content type and body."""
    task = _bg.get_task(task_id, _user(request))
    if task is None:
        raise HTTPException(404, "Unknown AI task (it may have finished long ago, or the server restarted)")
    return task.view(with_body=True)


@router.delete("/api/ai/tasks/{task_id}")
def stop_or_forget_task(task_id: str, request: Request):
    """Cancel a running task, or drop a finished one from the list."""
    task = _bg.get_task(task_id, _user(request))
    if task is None:
        raise HTTPException(404, "Unknown AI task")
    if task.status == "running":
        _bg.cancel(task)
        return {"ok": True, "cancelled": True}
    _bg._TASKS.pop(task.id, None)
    return {"ok": True, "cancelled": False}
