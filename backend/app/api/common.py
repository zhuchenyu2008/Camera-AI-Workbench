from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy import and_, or_

from app.errors import AppError
from app.security.secrets import fingerprint
from app.timeutils import iso, timestamp


def meta(request: Request):
    return {
        "request_id": request.state.request_id,
        "server_time": iso(request.app.state.workbench.now()),
        "schema_version": "1.0",
    }


def respond(request, data=None, status=200, page=None):
    if status == 204:
        return Response(status_code=204)
    body = {"data": data, "meta": meta(request)}
    if page is not None:
        body["page"] = page
    headers = {}
    resource = data.get("settings", data) if isinstance(data, dict) else None
    rev = resource.get("revision") if isinstance(resource, dict) else None
    if rev is None and isinstance(resource, dict):
        rev = resource.get("pricing_revision")
    if isinstance(rev, int):
        headers["ETag"] = f'"rev-{rev}"'
    return JSONResponse(body, status_code=status, headers=headers)


def error_response(request, error):
    headers = {}
    if error.status == 429:
        headers["Retry-After"] = str(error.details.get("retry_after_seconds", 60))
    return JSONResponse(
        {
            "error": {
                "code": error.code,
                "message": error.message,
                "field_errors": error.fields,
                "retryable": error.retryable,
                "details": error.details,
            },
            "meta": meta(request),
        },
        status_code=error.status,
        headers=headers,
    )


def range_ms(from_, to, max_hours=31 * 24):
    try:
        a, b = timestamp(from_), timestamp(to)
    except (ValueError, TypeError):
        raise AppError(400, "INVALID_TIME_RANGE", "请提供带时区的起止时间") from None
    if b <= a or b - a > max_hours * 3600000:
        raise AppError(400, "INVALID_TIME_RANGE", "查询时间范围错误或超限")
    return a, b


def page_rows(w, conn, statement, time_col, id_col, *, filters, limit, cursor, descending=False):
    binding = fingerprint(filters)
    if cursor:
        try:
            token = w.secrets.verify(cursor)
            if token["expires"] <= w.now() or token["filters"] != binding:
                raise ValueError
            last_time, last_id = token["last"]
        except (AppError, ValueError, KeyError, TypeError):
            raise AppError(400, "INVALID_CURSOR", "游标无效、过期或与筛选不匹配") from None
        statement = statement.where(
            or_(time_col < last_time, and_(time_col == last_time, id_col < last_id))
            if descending
            else or_(time_col > last_time, and_(time_col == last_time, id_col > last_id))
        )
    ordering = (time_col.desc(), id_col.desc()) if descending else (time_col, id_col)
    rows = list(conn.execute(statement.order_by(*ordering).limit(limit + 1)).mappings())
    more = len(rows) > limit
    rows = rows[:limit]
    next_cursor = None
    if more:
        last = rows[-1]
        next_cursor = w.secrets.sign(
            {
                "filters": binding,
                "last": [last[time_col.name], last[id_col.name]],
                "expires": w.now() + 86400000,
            }
        )
    return [dict(r) for r in rows], {"limit": limit, "next_cursor": next_cursor, "has_more": more}


def mutation(request, operation, payload, callback):
    w = request.app.state.workbench
    status, data = w.write(
        request.state.session["user_id"],
        operation,
        request.headers.get("Idempotency-Key"),
        {"path": request.url.path, "payload": payload},
        callback,
        request.state.request_id,
    )
    return respond(request, data, status)
