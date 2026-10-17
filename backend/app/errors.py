from __future__ import annotations


class AppError(Exception):
    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        *,
        details: dict | None = None,
        retryable: bool = False,
        fields: list | None = None,
    ):
        super().__init__(code)
        self.status = status
        self.code = code
        self.message = message
        self.details = details or {}
        self.retryable = retryable
        self.fields = fields or []


def require_revision(row: dict, header: str | None) -> None:
    if header is None:
        raise AppError(428, "REVISION_REQUIRED", "请提供资源版本 If-Match")
    if header != f'"rev-{row["revision"]}"':
        raise AppError(
            412, "REVISION_CONFLICT", "资源已更新，请重新读取", details={"revision": row["revision"]}
        )


def not_found() -> AppError:
    return AppError(404, "RESOURCE_NOT_FOUND", "资源不存在")
