from __future__ import annotations

import re
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError
from starlette.exceptions import HTTPException

from app.api.common import error_response
from app.api.routes import router
from app.config import Config
from app.errors import AppError
from app.schemas import ErrorEnvelope
from app.services.workbench import Workbench


def create_app(config: Config | None = None, workbench: Workbench | None = None) -> FastAPI:
    config = config or Config.from_env()

    @asynccontextmanager
    async def lifespan(application):
        application.state.workbench = workbench or Workbench(config)
        if workbench is None:
            from app.observability.logging import configure_logging

            configure_logging(config, "api")
        try:
            yield
        finally:
            if workbench is None:
                application.state.workbench.db.close()

    application = FastAPI(
        title="摄像头 AI 工作台",
        version="1.0",
        lifespan=lifespan,
        responses={
            code: {"model": ErrorEnvelope}
            for code in (400, 401, 403, 404, 409, 410, 412, 413, 415, 422, 428, 429, 503)
        },
    )
    if workbench:
        application.state.workbench = workbench

    @application.middleware("http")
    async def boundary(request: Request, call_next):
        request_id = request.headers.get("X-Request-ID", "")
        request.state.request_id = (
            request_id if re.fullmatch(r"[A-Za-z0-9._-]{1,100}", request_id) else "req_" + uuid.uuid4().hex
        )
        try:
            w = request.app.state.workbench
            write = request.method not in {"GET", "HEAD", "OPTIONS"}
            is_login = request.url.path == "/api/session" and request.method == "POST"
            if is_login and request.headers.get("Origin") != config.allowed_origin:
                raise AppError(403, "ORIGIN_DENIED", "登录来源不受允许")
            if write and request.headers.get("Origin") not in {None, config.allowed_origin}:
                raise AppError(403, "ORIGIN_DENIED", "请求来源不受允许")
            if not is_login and request.url.path != "/health/live":
                import asyncio

                request.state.session = await asyncio.to_thread(
                    w.auth.authenticate,
                    request.cookies.get("caw_session"),
                    request.headers.get("X-CSRF-Token"),
                    write,
                )
            if write:
                upload = "multipart/form-data" in request.headers.get("Content-Type", "")
                maximum = 8 * 1024 * 1024 + 65536 if upload else 1024 * 1024
                try:
                    length = int(request.headers.get("Content-Length", "0"))
                except ValueError:
                    raise AppError(400, "VALIDATION_ERROR", "Content-Length 不合法") from None
                if length > maximum or length < 0:
                    raise AppError(
                        413, "UPLOAD_TOO_LARGE" if upload else "REQUEST_TOO_LARGE", "请求超过大小上限"
                    )
                body = bytearray()
                async for chunk in request.stream():
                    body.extend(chunk)
                    if len(body) > maximum:
                        raise AppError(
                            413, "UPLOAD_TOO_LARGE" if upload else "REQUEST_TOO_LARGE", "请求超过大小上限"
                        )
                request._body = bytes(body)
            response = await call_next(request)
        except AppError as exc:
            response = error_response(request, exc)
        except Exception:
            import logging

            logging.getLogger("caw").error(
                "request_failure request_id=%s code=INTERNAL_ERROR", request.state.request_id
            )
            response = error_response(request, AppError(500, "INTERNAL_ERROR", "请求处理失败"))
        response.headers["X-Request-ID"] = request.state.request_id
        response.headers["Cache-Control"] = "private, no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @application.exception_handler(AppError)
    async def app_error(request, error):
        return error_response(request, error)

    @application.exception_handler(RequestValidationError)
    @application.exception_handler(ValidationError)
    async def validation(request, error):
        fields = []
        for item in error.errors():
            loc = [p for p in item["loc"] if p not in {"body", "query", "path"}]
            path = ""
            for part in loc:
                path += f"[{part}]" if isinstance(part, int) else ("." if path else "") + str(part)
            # 不返回 input/ctx，密码、密钥、第三方异常都不能进入错误 JSON。
            fields.append({"path": path, "code": item["type"], "message": "字段不符合类型、范围或业务约束"})
        code = (
            "RETENTION_TOO_SHORT"
            if any("RETENTION_TOO_SHORT" in str(e.get("msg", "")) for e in error.errors())
            else "VALIDATION_ERROR"
        )
        return error_response(request, AppError(422, code, "请求字段校验失败", fields=fields))

    @application.exception_handler(HTTPException)
    async def http_error(request, error):
        return error_response(
            request,
            AppError(
                error.status_code,
                "RESOURCE_NOT_FOUND" if error.status_code == 404 else "VALIDATION_ERROR",
                "接口不存在或请求不受支持",
            ),
        )

    application.include_router(router)
    original_openapi = application.openapi

    def openapi():
        result = original_openapi()
        result.setdefault("components", {}).setdefault("securitySchemes", {})["SessionCookie"] = {
            "type": "apiKey",
            "in": "cookie",
            "name": "caw_session",
        }
        for path, methods in result["paths"].items():
            for method, operation in methods.items():
                if not isinstance(operation, dict) or "operationId" not in operation:
                    continue
                if path != "/health/live" and not (path == "/api/session" and method == "post"):
                    operation["security"] = [{"SessionCookie": []}]
                if method in {"post", "put", "patch", "delete"} and not (
                    path == "/api/session" and method == "post"
                ):
                    headers = [
                        {
                            "name": "X-CSRF-Token",
                            "in": "header",
                            "required": True,
                            "schema": {"type": "string"},
                        }
                    ]
                    excluded = {
                        "logout",
                        "updatePassword",
                        "previewCameraUpdate",
                        "previewSettings",
                        "previewAnalysis",
                    }
                    if operation["operationId"] not in excluded:
                        headers.append(
                            {
                                "name": "Idempotency-Key",
                                "in": "header",
                                "required": True,
                                "schema": {"type": "string", "minLength": 1, "maxLength": 200},
                            }
                        )
                    versions = {
                        "updateCamera",
                        "deleteCamera",
                        "startCamera",
                        "stopCamera",
                        "resumeCameraSchedule",
                        "updateEvent",
                        "updateEntity",
                        "mergeEntity",
                        "splitEntity",
                        "deleteEntity",
                        "uploadReference",
                        "promoteReference",
                        "updateReference",
                        "replaceReference",
                        "deleteReference",
                        "updateProvider",
                        "deleteProvider",
                        "updateProviderPricing",
                        "updateSettings",
                    }
                    if operation["operationId"] in versions:
                        headers.append(
                            {
                                "name": "If-Match",
                                "in": "header",
                                "required": True,
                                "schema": {"type": "string", "pattern": '^"rev-[0-9]+"$'},
                            }
                        )
                    parameters = operation.setdefault("parameters", [])
                    present = {(p.get("name"), p.get("in")) for p in parameters}
                    parameters.extend(p for p in headers if (p["name"], p["in"]) not in present)
        return result

    application.openapi = openapi
    return application


app = create_app()
