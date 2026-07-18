from __future__ import annotations

import hmac
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .config import Settings
from .engine import OpsEngine
from .models import ApprovalDecision
from .web import build_web_router, static_dir


class DeployRequest(BaseModel):
    manifest: str = Field(min_length=1, max_length=200_000)


class DiagnoseRequest(BaseModel):
    service: str = Field(min_length=1, max_length=63)
    symptom: str = Field(default="", max_length=1000)


class SecurityScanRequest(BaseModel):
    service: str = Field(min_length=1, max_length=63)


class RollbackRequest(BaseModel):
    revision_id: str = Field(min_length=1, max_length=64)


def create_app(settings: Settings, engine: OpsEngine) -> FastAPI:
    app = FastAPI(
        title="Local AI Ops Agent",
        version="0.2.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.settings = settings
    app.state.engine = engine
    app.mount("/static", StaticFiles(directory=static_dir()), name="static")

    def require_operator_token(x_aiops_token: str | None = Header(default=None)) -> None:
        if not x_aiops_token or not hmac.compare_digest(x_aiops_token, engine.operator_token):
            raise HTTPException(status_code=401, detail="valid local operator token required")

    @app.exception_handler(KeyError)
    async def key_error_handler(_request: Request, exc: KeyError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=404)

    @app.exception_handler(ValueError)
    async def value_error_handler(_request: Request, exc: ValueError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=422)

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        return {"ok": True, "profile": settings.profile}

    @app.get("/", include_in_schema=False)
    async def root() -> RedirectResponse:
        return RedirectResponse("/ui/", status_code=307)

    @app.get("/v1/capabilities", dependencies=[Depends(require_operator_token)])
    async def capabilities() -> dict[str, Any]:
        return engine.capabilities()

    @app.get("/v1/services", dependencies=[Depends(require_operator_token)])
    async def list_services() -> list[dict[str, Any]]:
        return engine.store.list_services()

    @app.get("/v1/services/{service_name}", dependencies=[Depends(require_operator_token)])
    async def get_service(service_name: str) -> dict[str, Any]:
        return {
            "service": engine.store.get_service(service_name),
            "revisions": [item.model_dump(mode="json") for item in engine.store.list_revisions(service_name)],
        }

    @app.post("/v1/tasks/deploy", dependencies=[Depends(require_operator_token)], status_code=202)
    async def deploy(request: DeployRequest) -> dict[str, Any]:
        return engine.submit_deploy(request.manifest).model_dump(mode="json")

    @app.post("/v1/tasks/diagnose", dependencies=[Depends(require_operator_token)], status_code=202)
    async def diagnose(request: DiagnoseRequest) -> dict[str, Any]:
        return engine.submit_diagnosis(request.service, request.symptom).model_dump(mode="json")

    @app.post("/v1/tasks/security", dependencies=[Depends(require_operator_token)], status_code=202)
    async def security_scan(request: SecurityScanRequest) -> dict[str, Any]:
        return engine.submit_security_scan(request.service).model_dump(mode="json")

    @app.post("/v1/tasks/rollback", dependencies=[Depends(require_operator_token)], status_code=202)
    async def rollback(request: RollbackRequest) -> dict[str, Any]:
        return engine.submit_rollback(request.revision_id).model_dump(mode="json")

    @app.get("/v1/tasks", dependencies=[Depends(require_operator_token)])
    async def list_tasks(limit: int = 100) -> list[dict[str, Any]]:
        return [item.model_dump(mode="json") for item in engine.store.list_tasks(min(max(limit, 1), 500))]

    @app.get("/v1/tasks/{task_id}", dependencies=[Depends(require_operator_token)])
    async def task_detail(task_id: str) -> dict[str, Any]:
        return engine.task_detail(task_id)

    @app.get("/v1/tasks/{task_id}/events", dependencies=[Depends(require_operator_token)])
    async def task_events(task_id: str, after: int = 0) -> list[dict[str, Any]]:
        engine.store.get_task(task_id)
        return engine.store.list_events(task_id, after_seq=max(after, 0))

    @app.post("/v1/tasks/{task_id}/cancel", dependencies=[Depends(require_operator_token)])
    async def cancel_task(task_id: str) -> dict[str, Any]:
        return engine.cancel_task(task_id).model_dump(mode="json")

    @app.get("/v1/actions/{action_id}", dependencies=[Depends(require_operator_token)])
    async def get_action(action_id: str) -> dict[str, Any]:
        return engine.store.get_action(action_id).model_dump(mode="json")

    @app.post("/v1/actions/{action_id}/decision", dependencies=[Depends(require_operator_token)])
    async def decide_action(action_id: str, decision: ApprovalDecision) -> dict[str, Any]:
        return engine.decide_action(action_id, decision).model_dump(mode="json")

    @app.get("/v1/events", dependencies=[Depends(require_operator_token)])
    async def audit_events(limit: int = 200) -> list[dict[str, Any]]:
        return engine.store.list_events(limit=min(max(limit, 1), 500))

    @app.post("/v1/web/login-code", dependencies=[Depends(require_operator_token)])
    async def create_login_code() -> dict[str, Any]:
        from .security import new_token

        code = new_token()
        engine.store.create_login_code(code)
        return {"url": f"http://{settings.web_host}:{settings.web_port}/login?code={code}", "expires_in": 120}

    app.include_router(build_web_router(settings, engine))
    return app
