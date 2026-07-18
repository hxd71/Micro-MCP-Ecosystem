from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, Protocol

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

from .config import Settings
from .engine import OpsEngine
from .models import ApprovalDecision
from .security import new_token
from .store import StateStore

BASE_DIR = Path(__file__).resolve().parent


class DisconnectAware(Protocol):
    async def is_disconnected(self) -> bool: ...


async def stream_task_events(
    request: DisconnectAware,
    store: StateStore,
    task_id: str,
    after: int = 0,
    poll_seconds: float = 1,
) -> AsyncIterator[str]:
    cursor = max(after, 0)
    while not await request.is_disconnected():
        events = store.list_events(task_id, after_seq=cursor)
        for event in events:
            cursor = max(cursor, int(event["seq"]))
            yield f"id: {event['seq']}\nevent: audit\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
        yield ": keepalive\n\n"
        await asyncio.sleep(poll_seconds)


def static_dir() -> Path:
    return BASE_DIR / "static"


def build_web_router(settings: Settings, engine: OpsEngine) -> APIRouter:
    router = APIRouter()
    templates = Jinja2Templates(directory=BASE_DIR / "templates")

    def session_for(request: Request) -> dict[str, Any] | None:
        token = request.cookies.get("aiops_session", "")
        return engine.store.get_web_session(token) if token else None

    def require_session(request: Request) -> dict[str, Any]:
        session = session_for(request)
        if session is None:
            raise HTTPException(status_code=401, detail="Web session required. Run: aiops web login")
        return session

    def login_redirect(request: Request) -> RedirectResponse | None:
        return None if session_for(request) else RedirectResponse("/login", status_code=303)

    async def require_csrf(request: Request) -> dict[str, Any]:
        session = require_session(request)
        supplied = request.headers.get("x-csrf-token", "")
        if not supplied or supplied != session["csrf_token"]:
            raise HTTPException(status_code=403, detail="CSRF validation failed")
        return session

    def context(request: Request, **values: Any) -> dict[str, Any]:
        session = require_session(request)
        return {
            "request": request,
            "csrf_token": session["csrf_token"],
            "profile": settings.profile,
            "operator": settings.operator_name,
            **values,
        }

    @router.get("/login", response_class=HTMLResponse)
    async def login(request: Request, code: str = "") -> Any:
        if not code or not engine.store.consume_login_code(code):
            return templates.TemplateResponse(
                request=request,
                name="login.html",
                context={"request": request, "error": "登录链接无效或已过期。"},
                status_code=401,
            )
        session_token = new_token()
        csrf_token = new_token()
        engine.store.create_web_session(session_token, csrf_token, settings.session_ttl_seconds)
        response = RedirectResponse("/ui/", status_code=303)
        response.set_cookie(
            "aiops_session",
            session_token,
            max_age=settings.session_ttl_seconds,
            httponly=True,
            samesite="strict",
            secure=False,
            path="/",
        )
        return response

    @router.get("/ui/", response_class=HTMLResponse)
    async def dashboard(request: Request) -> Any:
        if redirect := login_redirect(request):
            return redirect
        capabilities = engine.capabilities()
        services = engine.store.list_services()
        tasks = engine.store.list_tasks(limit=12)
        findings = engine.store.list_findings(limit=12)
        gpu_devices = capabilities.get("nvidia", {}).get("devices", [])
        return templates.TemplateResponse(
            request=request,
            name="dashboard.html",
            context=context(
                request,
                page="dashboard",
                title="运行总览",
                capabilities=capabilities,
                services=services,
                tasks=tasks,
                findings=findings,
                gpu_devices=gpu_devices,
            ),
        )

    @router.get("/ui/services/{service_name}", response_class=HTMLResponse)
    async def service_detail(request: Request, service_name: str) -> Any:
        if redirect := login_redirect(request):
            return redirect
        service = engine.store.get_service(service_name)
        revisions = engine.store.list_revisions(service_name)
        tasks = [item for item in engine.store.list_tasks(limit=100) if item.target.name == service_name][:20]
        container = await asyncio.to_thread(engine.docker.inspect_service, service_name)
        gpu = await asyncio.to_thread(engine.nvidia.status)
        return templates.TemplateResponse(
            request=request,
            name="service.html",
            context=context(
                request,
                page="services",
                title=service_name,
                service=service,
                revisions=revisions,
                tasks=tasks,
                container=container,
                gpu=gpu,
            ),
        )

    @router.get("/ui/tasks/{task_id}", response_class=HTMLResponse)
    async def task_detail(request: Request, task_id: str) -> Any:
        if redirect := login_redirect(request):
            return redirect
        detail = engine.task_detail(task_id)
        latest_seq = max((event["seq"] for event in detail["events"]), default=0)
        return templates.TemplateResponse(
            request=request,
            name="task.html",
            context=context(
                request,
                page="tasks",
                title=f"任务 {task_id[:8]}",
                detail=detail,
                latest_seq=latest_seq,
            ),
        )

    @router.get("/ui/tasks", response_class=HTMLResponse)
    async def tasks_page(request: Request) -> Any:
        if redirect := login_redirect(request):
            return redirect
        return templates.TemplateResponse(
            request=request,
            name="tasks.html",
            context=context(request, page="tasks", title="任务", tasks=engine.store.list_tasks(limit=200)),
        )

    @router.get("/ui/audit", response_class=HTMLResponse)
    async def audit_page(request: Request, event_type: str = "", task_id: str = "") -> Any:
        if redirect := login_redirect(request):
            return redirect
        events = engine.store.list_events(task_id=task_id or None, limit=300)
        if event_type:
            events = [event for event in events if event["event_type"] == event_type]
        return templates.TemplateResponse(
            request=request,
            name="audit.html",
            context=context(
                request,
                page="audit",
                title="审计",
                events=events,
                event_type=event_type,
                task_id=task_id,
                chain_valid=engine.store.verify_event_chain(),
            ),
        )

    @router.post("/ui-api/diagnose")
    async def submit_diagnosis(request: Request) -> JSONResponse:
        await require_csrf(request)
        body = await request.json()
        service = str(body.get("service", "")).strip()
        symptom = str(body.get("symptom", "")).strip()
        task = engine.submit_diagnosis(service, symptom)
        return JSONResponse({"task_id": task.id, "location": f"/ui/tasks/{task.id}"}, status_code=202)

    @router.post("/ui-api/actions/{action_id}/decision")
    async def action_decision(request: Request, action_id: str) -> JSONResponse:
        await require_csrf(request)
        body = await request.json()
        decision = ApprovalDecision.model_validate(body)
        action = engine.decide_action(action_id, decision)
        return JSONResponse(
            {"action": action.model_dump(mode="json"), "location": f"/ui/tasks/{action.task_id}"}
        )

    @router.get("/ui/tasks/{task_id}/stream")
    async def task_stream(request: Request, task_id: str, after: int = 0) -> StreamingResponse:
        require_session(request)
        engine.store.get_task(task_id)
        return StreamingResponse(
            stream_task_events(request, engine.store, task_id, after),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache"},
        )

    return router
