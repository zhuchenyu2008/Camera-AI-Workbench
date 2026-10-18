from __future__ import annotations

import re
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, File, Form, Query, Request, UploadFile
from fastapi.responses import Response
from pydantic import BeforeValidator
from sqlalchemy import and_, func, select

from app.api.common import mutation, page_rows, range_ms, respond
from app.errors import AppError
from app.repositories import schema as s
from app.repositories.db import one
from app.schemas import (
    AnalysisCreate,
    AnalysisPreviewDto,
    AnalysisRequest,
    CameraControl,
    CameraControlDto,
    CameraCreate,
    CameraDto,
    CameraPatch,
    CameraPreview,
    Crop,
    EntityCreate,
    EntityDto,
    EntityPatch,
    Envelope,
    EventCorrection,
    EventDto,
    EventUpdateDto,
    EvidenceDto,
    ImpactPreviewDto,
    JobDto,
    ListEnvelope,
    Login,
    MinuteDto,
    MinuteEvidenceSlotDto,
    OverviewDto,
    PasswordChange,
    PreviewDto,
    PromoteReference,
    ProviderDto,
    ProviderTest,
    ProviderWrite,
    ReferenceDto,
    ReferencePatch,
    RegenerateSummary,
    SessionDto,
    SettingsDto,
    SettingsPreview,
    SettingsUpdate,
    SettingsUpdateDto,
    StatusDto,
    SummaryDto,
    TimelineDto,
    UsageDto,
)
from app.security.secrets import digest, safe_path
from app.services.analysis import Analysis
from app.services.assets import decode_image
from app.services.records import Records
from app.summaries.service import Summaries
from app.timeutils import BEIJING, day_range, iso
from app.worker.queue import TERMINAL, enqueue, job_dto

router = APIRouter()
Limit = Annotated[int, Query(ge=1, le=100)]


@router.get("/health/live", operation_id="getHealth")
def health():
    return {"alive": True}


@router.post("/api/session", operation_id="login", response_model=Envelope[SessionDto])
def login(request: Request, dto: Login):
    w = request.app.state.workbench
    token, data = w.auth.login(
        dto.username, dto.password, request.client.host if request.client else "unknown"
    )
    result = respond(request, data)
    result.set_cookie(
        "caw_session",
        token,
        max_age=w.config.session_seconds,
        secure=w.config.secure_cookies,
        httponly=True,
        samesite="strict",
        path="/",
    )
    return result


@router.get("/api/session", operation_id="getSession", response_model=Envelope[SessionDto])
def session(request: Request):
    return respond(request, request.app.state.workbench.auth.session(request.state.session))


@router.delete("/api/session", operation_id="logout", status_code=204)
def logout(request: Request):
    request.app.state.workbench.auth.logout(request.state.session)
    result = respond(request, status=204)
    result.delete_cookie(
        "caw_session",
        path="/",
        secure=request.app.state.workbench.config.secure_cookies,
        httponly=True,
        samesite="strict",
    )
    return result


@router.patch("/api/account/password", operation_id="updatePassword", status_code=204)
def password(request: Request, dto: PasswordChange):
    request.app.state.workbench.auth.password(request.state.session, dto.current_password, dto.new_password)
    return respond(request, status=204)


@router.get("/api/cameras", operation_id="listCameras", response_model=ListEnvelope[CameraDto])
def cameras(request: Request, limit: Limit = 50, cursor: str | None = None):
    w = request.app.state.workbench
    with w.db.read() as conn:
        rows, page = page_rows(
            w,
            conn,
            select(s.cameras).where(s.cameras.c.deleted_at_ms.is_(None)),
            s.cameras.c.created_at_ms,
            s.cameras.c.id,
            filters={"op": "listCameras"},
            limit=limit,
            cursor=cursor,
        )
        return respond(request, [w.camera(conn, r["id"]) for r in rows], page=page)


@router.post("/api/cameras", operation_id="createCamera", status_code=201, response_model=Envelope[CameraDto])
def create_camera(request: Request, dto: CameraCreate):
    return mutation(
        request,
        "createCamera",
        dto.model_dump(mode="json"),
        lambda conn: (201, request.app.state.workbench.create_camera(conn, dto)),
    )


@router.get("/api/cameras/{camera_id}", operation_id="getCamera", response_model=Envelope[CameraDto])
def get_camera(request: Request, camera_id: str):
    w = request.app.state.workbench
    with w.db.read() as conn:
        return respond(request, w.camera(conn, camera_id))


@router.post(
    "/api/cameras/{camera_id}/preview-update",
    operation_id="previewCameraUpdate",
    response_model=Envelope[ImpactPreviewDto],
)
def preview_camera(request: Request, camera_id: str, dto: CameraPreview):
    w = request.app.state.workbench
    free = w.disk_free()
    with w.db.read() as conn:
        patch = dto.patch.model_dump(mode="json", exclude_unset=True, exclude={"preview_token"})
        return respond(request, w.impact(conn, patch, camera_id, dto.base_revision, disk_free=free))


@router.patch("/api/cameras/{camera_id}", operation_id="updateCamera", response_model=Envelope[CameraDto])
def update_camera(request: Request, camera_id: str, dto: CameraPatch):
    w = request.app.state.workbench
    free = w.disk_free()
    patch = dto.model_dump(mode="json", exclude_unset=True, exclude={"preview_token"})
    return mutation(
        request,
        "updateCamera",
        dto.model_dump(mode="json", exclude_unset=True),
        lambda conn: (
            200,
            w.update_camera(
                conn, camera_id, patch, dto.preview_token, request.headers.get("If-Match"), disk_free=free
            ),
        ),
    )


@router.delete(
    "/api/cameras/{camera_id}", operation_id="deleteCamera", status_code=202, response_model=Envelope[JobDto]
)
def delete_camera(request: Request, camera_id: str):
    w = request.app.state.workbench
    return mutation(
        request,
        "deleteCamera",
        {},
        lambda conn: (202, w.delete_camera(conn, camera_id, request.headers.get("If-Match"))),
    )


@router.post(
    "/api/cameras/{camera_id}/test",
    operation_id="testCamera",
    status_code=202,
    response_model=Envelope[JobDto],
)
def test_camera(request: Request, camera_id: str):
    w = request.app.state.workbench

    def apply(conn):
        camera = one(conn, s.cameras, camera_id)
        job = enqueue(
            conn,
            w.now(),
            "camera_test",
            {"camera_id": camera_id, "source_revision": camera["source_revision"]},
            f"camera-test:{camera_id}:{request.headers.get('Idempotency-Key')}",
            total=1,
        )
        return 202, job_dto(job)

    return mutation(request, "testCamera", {}, apply)


def camera_control(request, camera_id, dto, mode, operation):
    w = request.app.state.workbench
    return mutation(
        request,
        operation,
        dto.model_dump(mode="json"),
        lambda conn: (
            202,
            w.control_camera(conn, camera_id, mode, dto.expires_at, request.headers.get("If-Match")),
        ),
    )


@router.post(
    "/api/cameras/{camera_id}/start",
    operation_id="startCamera",
    status_code=202,
    response_model=Envelope[CameraControlDto],
)
def start_camera(request: Request, camera_id: str, dto: CameraControl):
    return camera_control(request, camera_id, dto, "force_on", "startCamera")


@router.post(
    "/api/cameras/{camera_id}/stop",
    operation_id="stopCamera",
    status_code=202,
    response_model=Envelope[CameraControlDto],
)
def stop_camera(request: Request, camera_id: str, dto: CameraControl):
    return camera_control(request, camera_id, dto, "force_off", "stopCamera")


@router.post(
    "/api/cameras/{camera_id}/resume-schedule",
    operation_id="resumeCameraSchedule",
    status_code=202,
    response_model=Envelope[CameraControlDto],
)
def resume_camera(request: Request, camera_id: str):
    return camera_control(request, camera_id, CameraControl(), None, "resumeCameraSchedule")


def preview_info(w, conn, camera_id):
    camera = one(conn, s.cameras, camera_id)
    runtime = (
        conn.execute(select(s.camera_runtime).where(s.camera_runtime.c.camera_id == camera_id))
        .mappings()
        .one()
    )
    stale_ms = w.settings_config(conn).source_stale_seconds * 1000
    fresh = (
        camera["enabled"]
        and runtime["capture_state"] == "online"
        and runtime["last_frame_at_ms"] is not None
        and w.now() - runtime["last_frame_at_ms"] < stale_ms
    )
    sid = runtime["stream_session_id"]
    hls = (
        fresh
        and runtime["diagnostics_json"].get("codec") == "h264"
        and sid
        and safe_path(w.config.data_dir, f"hls/{sid}/live.m3u8").is_file()
    )
    return dict(
        camera_id=camera_id,
        capture_state=runtime["capture_state"],
        mode="hls" if hls else "snapshot" if fresh else "unavailable",
        session_id=sid if fresh else None,
        manifest_url=f"/api/cameras/{camera_id}/preview/{sid}/live.m3u8" if hls else None,
        snapshot_url=f"/api/cameras/{camera_id}/snapshot" if fresh else None,
        captured_at=iso(runtime["last_frame_at_ms"]),
        stale_after_ms=stale_ms,
        reason=None if fresh else runtime["stop_reason"] or "source_stale",
    )


@router.get(
    "/api/cameras/{camera_id}/preview/status",
    operation_id="getPreviewStatus",
    response_model=Envelope[PreviewDto],
)
def preview_status(request: Request, camera_id: str):
    w = request.app.state.workbench
    with w.db.read() as conn:
        return respond(request, preview_info(w, conn, camera_id))


@router.get(
    "/api/cameras/{camera_id}/preview/{session_id}/{filename}",
    operation_id="getPreviewMedia",
    response_class=Response,
    responses={200: {"content": {"application/vnd.apple.mpegurl": {}, "video/mp2t": {}}}},
)
def preview_media(request: Request, camera_id: str, session_id: str, filename: str):
    w = request.app.state.workbench
    if filename != "live.m3u8" and not re.fullmatch(r"seg_\d{6,12}\.ts", filename):
        raise AppError(404, "RESOURCE_NOT_FOUND", "媒体不存在")
    with w.db.read() as conn:
        preview = preview_info(w, conn, camera_id)
        if preview["mode"] != "hls" or preview["session_id"] != session_id:
            raise AppError(404, "RESOURCE_NOT_FOUND", "预览会话已失效")
    path = safe_path(w.config.data_dir, f"hls/{session_id}/{filename}")
    if not path.is_file() or path.stat().st_mtime * 1000 < w.now() - 60000:
        raise AppError(404, "RESOURCE_NOT_FOUND", "媒体已移除")
    data = path.read_bytes()
    if filename.endswith("m3u8"):
        text = data.decode("utf-8")
        if any(
            line and not line.startswith("#") and not re.fullmatch(r"seg_\d{6,12}\.ts", line)
            for line in text.splitlines()
        ) or any("URI=" in line for line in text.splitlines()):
            raise AppError(503, "PREVIEW_UNAVAILABLE", "预览清单校验失败")
    return Response(
        data, media_type="application/vnd.apple.mpegurl" if filename.endswith("m3u8") else "video/mp2t"
    )


@router.get(
    "/api/cameras/{camera_id}/snapshot",
    operation_id="getSnapshot",
    response_class=Response,
    responses={200: {"content": {"image/jpeg": {}}}},
)
def snapshot(request: Request, camera_id: str):
    w = request.app.state.workbench
    with w.db.read() as conn:
        if preview_info(w, conn, camera_id)["mode"] == "unavailable":
            raise AppError(503, "PREVIEW_UNAVAILABLE", "当前没有新鲜画面", retryable=True)
        frame = conn.execute(
            select(s.frames.c.id)
            .where(s.frames.c.camera_id == camera_id, s.frames.c.state == "available")
            .order_by(s.frames.c.captured_at_ms.desc())
            .limit(1)
        ).first()
    if not frame:
        raise AppError(503, "PREVIEW_UNAVAILABLE", "当前没有可读快照", retryable=True)
    data, meta = w.assets.read_evidence(frame.id)
    return Response(
        data,
        media_type="image/jpeg",
        headers={"X-Captured-At": meta["captured_at"], "X-Evidence-ID": meta["id"]},
    )


@router.get("/api/overview", operation_id="getOverview", response_model=Envelope[OverviewDto])
def overview(
    request: Request,
    from_: str = Query(alias="from"),
    to: str = Query(),
    camera_ids: list[str] | None = Query(None),
    subject_id: str | None = None,
):
    w = request.app.state.workbench
    a, b = range_ms(from_, to)
    today = datetime.fromtimestamp(w.now() / 1000, BEIJING).date().isoformat()
    if datetime.fromtimestamp(a / 1000, BEIJING).date().isoformat() == today:
        b = min(b, w.now())
    if b <= a:
        raise AppError(400, "INVALID_TIME_RANGE", "裁剪至当前时间后没有可查询区间")
    with w.db.read() as conn:
        scope = w.ledger.scope(conn, camera_ids, subject_id, a, b)
        result = w.ledger.calculate(conn, a, b, scope)
        result.pop("slices")
        events = event_rows(w, conn, a, b, scope, None, 10)
        result |= {"from": iso(a), "to": iso(b), "scope": scope, "latest_events": events, "warnings": []}
        return respond(request, result)


def event_rows(w, conn, a, b, scope, activity, limit=100):
    stmt = (
        select(s.events.c.id)
        .join(
            s.event_revisions,
            and_(
                s.events.c.id == s.event_revisions.c.event_id,
                s.events.c.current_revision == s.event_revisions.c.revision,
            ),
        )
        .where(
            s.event_revisions.c.summary_json["start_at"].as_string() < iso(b),
            s.event_revisions.c.summary_json["end_at"].as_string() > iso(a),
            s.event_revisions.c.summary_json["source_state"].as_string() == "current",
        )
    )
    if activity:
        stmt = stmt.where(s.event_revisions.c.summary_json["activity"].as_string() == activity)
    if scope["subject_id"]:
        stmt = stmt.where(s.event_revisions.c.summary_json["subject_id"].as_string() == scope["subject_id"])
    result = []
    for row in conn.execute(stmt.order_by(s.event_revisions.c.summary_json["start_at"].as_string().desc(), s.events.c.id).limit(limit * 10)):
        event = w.ledger.event(conn, row.id)
        if set(scope["camera_ids"]) & set(event["camera_ids"]):
            result.append(event)
            if len(result) >= limit:
                break
    return result


@router.get("/api/timeline", operation_id="getTimeline", response_model=Envelope[TimelineDto])
def timeline(
    request: Request,
    from_: str = Query(alias="from"),
    to: str = Query(),
    camera_ids: list[str] | None = Query(None),
    subject_id: str | None = None,
    bucket_seconds: Annotated[Literal[60, 300, 900], BeforeValidator(int)] = 60,
):
    w = request.app.state.workbench
    a, b = range_ms(from_, to, 24)
    if datetime.fromtimestamp(a / 1000, BEIJING).date() == datetime.fromtimestamp(w.now() / 1000, BEIJING).date():
        b = min(b, w.now())
    if b <= a:
        raise AppError(400, "INVALID_TIME_RANGE", "此范围尚未发生")
    buckets = []
    with w.db.read() as conn:
        scope = w.ledger.scope(conn, camera_ids, subject_id, a, b)
        for start in range(a, b, bucket_seconds * 1000):
            end = min(b, start + bucket_seconds * 1000)
            result = w.ledger.calculate(conn, start, end, scope)
            buckets.append(
                {
                    "start_at": iso(start),
                    "end_at": iso(end),
                    "coverage": result["coverage"],
                    "activities": result["activities"],
                }
            )
        events = event_rows(w, conn, a, b, scope, None, 101)
        return respond(
            request,
            {
                "from": iso(a),
                "to": iso(b),
                "scope": scope,
                "bucket_seconds": bucket_seconds,
                "projection_revision": w.ledger.revision(conn),
                "buckets": buckets,
                "event_refs": [{"id": e["id"], "revision": e["revision"]} for e in events[:100]],
                "truncated": len(events) > 100,
            },
        )


@router.get("/api/events", operation_id="listEvents", response_model=ListEnvelope[EventDto])
def events(
    request: Request,
    from_: str = Query(alias="from"),
    to: str = Query(),
    camera_ids: list[str] | None = Query(None),
    subject_id: str | None = None,
    activity: str | None = None,
    identity_status: Literal["matched", "possible", "unknown"] | None = None,
    limit: Limit = 50,
    cursor: str | None = None,
):
    w = request.app.state.workbench
    a, b = range_ms(from_, to)
    with w.db.read() as conn:
        scope = w.ledger.scope(conn, camera_ids, subject_id, a, b)
        event_started_at = s.event_revisions.c.summary_json["start_at"].as_string().label("event_started_at")
        stmt = (
            select(s.events, event_started_at)
            .join(
                s.event_revisions,
                and_(
                    s.events.c.id == s.event_revisions.c.event_id,
                    s.events.c.current_revision == s.event_revisions.c.revision,
                ),
            )
            .where(
                s.event_revisions.c.summary_json["start_at"].as_string() < iso(b),
                s.event_revisions.c.summary_json["end_at"].as_string() > iso(a),
                s.event_revisions.c.summary_json["source_state"].as_string() == "current",
            )
        )
        if subject_id:
            stmt = stmt.where(s.event_revisions.c.summary_json["subject_id"].as_string() == subject_id)
        if activity:
            stmt = stmt.where(s.event_revisions.c.summary_json["activity"].as_string() == activity)
        if identity_status:
            stmt = stmt.where(s.event_revisions.c.summary_json["identity_status"].as_string() == identity_status)
        # JSON 数组包含筛选由 SQLite json_each 执行，分页前应用。
        from sqlalchemy import exists

        member = func.json_each(s.event_revisions.c.summary_json, "$.camera_ids").table_valued("value")
        stmt = stmt.where(
            exists(select(1).select_from(member).where(member.c.value.in_(scope["camera_ids"])))
        )
        rows, page = page_rows(
            w,
            conn,
            stmt,
            event_started_at,
            s.events.c.id,
            filters={"op": "listEvents", "from": a, "to": b, "scope": scope, "activity": activity, "identity_status": identity_status},
            limit=limit,
            cursor=cursor,
            descending=True,
        )
        data = []
        for row in rows:
            event = w.ledger.event(conn, row["id"])
            event["segments"] = event["segments"][:3]
            data.append(event)
        return respond(request, data, page=page)


@router.get("/api/events/{event_id}", operation_id="getEvent", response_model=Envelope[EventDto])
def event(request: Request, event_id: str, revision: int | None = Query(None, ge=1)):
    w = request.app.state.workbench
    with w.db.read() as conn:
        return respond(request, w.ledger.event(conn, event_id, revision))


@router.patch("/api/events/{event_id}", operation_id="updateEvent", response_model=Envelope[EventUpdateDto])
def correct_event(request: Request, event_id: str, dto: EventCorrection):
    w = request.app.state.workbench
    return mutation(
        request,
        "updateEvent",
        dto.model_dump(mode="json"),
        lambda conn: (200, w.ledger.correct_event(conn, event_id, dto, request.headers.get("If-Match"))),
    )


@router.get("/api/minutes", operation_id="listMinutes", response_model=ListEnvelope[MinuteDto])
def minutes(
    request: Request,
    from_: str = Query(alias="from"),
    to: str = Query(),
    camera_id: str | None = None,
    analysis_state: str | None = None,
    limit: Limit = 50,
    cursor: str | None = None,
):
    w = request.app.state.workbench
    a, b = range_ms(from_, to, 24)
    stmt = select(s.minute_records).where(
        s.minute_records.c.start_at_ms < b, s.minute_records.c.start_at_ms + 60000 > a
    )
    if camera_id:
        stmt = stmt.where(s.minute_records.c.camera_id == camera_id)
    if analysis_state:
        stmt = stmt.where(s.minute_records.c.analysis_state == analysis_state)
    with w.db.read() as conn:
        rows, page = page_rows(
            w,
            conn,
            stmt,
            s.minute_records.c.start_at_ms,
            s.minute_records.c.id,
            filters={
                "op": "listMinutes",
                "from": a,
                "to": b,
                "camera_id": camera_id,
                "analysis_state": analysis_state,
            },
            limit=limit,
            cursor=cursor,
        )
        return respond(request, [Records(w).minute(conn, r["id"], compact=True) for r in rows], page=page)


@router.get("/api/minutes/{minute_id}", operation_id="getMinute", response_model=Envelope[MinuteDto])
def minute(request: Request, minute_id: str, revision: int | None = Query(None, ge=1)):
    w = request.app.state.workbench
    with w.db.read() as conn:
        return respond(request, Records(w).minute(conn, minute_id, revision))


@router.get(
    "/api/minutes/{minute_id}/evidence",
    operation_id="listMinuteEvidence",
    response_model=ListEnvelope[MinuteEvidenceSlotDto],
)
def minute_evidence(request: Request, minute_id: str, limit: Limit = 50, cursor: str | None = None):
    w = request.app.state.workbench
    with w.db.read() as conn:
        slots = Records(w).evidence_slots(conn, minute_id)
    start = 0
    if cursor:
        try:
            info = w.secrets.verify(cursor)
        except AppError:
            raise AppError(400, "INVALID_CURSOR", "证据游标无效") from None
        if info.get("minute") != minute_id or info.get("expires", 0) <= w.now():
            raise AppError(400, "INVALID_CURSOR", "证据游标无效")
        start = info.get("offset", 0)
        if not isinstance(start, int) or start < 0 or start > 60:
            raise AppError(400, "INVALID_CURSOR", "证据游标无效")
    end = start + limit
    more = end < len(slots)
    next_cursor = (
        w.secrets.sign({"minute": minute_id, "offset": end, "expires": w.now() + 86400000}) if more else None
    )
    return respond(
        request, slots[start:end], page={"limit": limit, "next_cursor": next_cursor, "has_more": more}
    )


@router.get(
    "/api/evidence/{evidence_id}/metadata",
    operation_id="getEvidenceMetadata",
    response_model=Envelope[EvidenceDto],
)
def evidence_metadata(request: Request, evidence_id: str):
    w = request.app.state.workbench
    with w.db.read() as conn:
        return respond(request, w.assets.metadata(conn, evidence_id))


@router.get(
    "/api/evidence/{evidence_id}",
    operation_id="getEvidence",
    response_class=Response,
    responses={200: {"content": {"image/jpeg": {}}}},
)
def evidence(request: Request, evidence_id: str):
    data, _ = request.app.state.workbench.assets.read_evidence(evidence_id)
    return Response(data, media_type="image/jpeg")


@router.post(
    "/api/analysis-jobs/preview", operation_id="previewAnalysis", response_model=Envelope[AnalysisPreviewDto]
)
def preview_analysis(request: Request, dto: AnalysisRequest):
    w = request.app.state.workbench
    with w.db.write() as conn:
        result, _, _ = Analysis(w).preview(conn, dto)
        return respond(request, result)


@router.post(
    "/api/analysis-jobs", operation_id="createAnalysisJob", status_code=202, response_model=Envelope[JobDto]
)
def create_analysis(request: Request, dto: AnalysisCreate):
    w = request.app.state.workbench
    return mutation(
        request,
        "createAnalysisJob",
        dto.model_dump(mode="json", by_alias=True),
        lambda conn: (202, Analysis(w).create(conn, dto)),
    )


@router.get("/api/jobs/{job_id}", operation_id="getJob", response_model=Envelope[JobDto])
def get_job(request: Request, job_id: str):
    w = request.app.state.workbench
    with w.db.read() as conn:
        return respond(request, job_dto(one(conn, s.jobs, job_id)))


@router.get("/api/jobs", operation_id="listJobs", response_model=ListEnvelope[JobDto])
def jobs(
    request: Request,
    from_: str = Query(alias="from"),
    to: str = Query(),
    kind: str | None = None,
    job_state: str | None = None,
    limit: Limit = 50,
    cursor: str | None = None,
):
    w = request.app.state.workbench
    a, b = range_ms(from_, to)
    stmt = select(s.jobs).where(s.jobs.c.created_at_ms >= a, s.jobs.c.created_at_ms < b)
    if kind:
        stmt = stmt.where(s.jobs.c.kind == kind)
    if job_state:
        stmt = stmt.where(s.jobs.c.state == job_state)
    with w.db.read() as conn:
        rows, page = page_rows(
            w,
            conn,
            stmt,
            s.jobs.c.created_at_ms,
            s.jobs.c.id,
            filters={"op": "listJobs", "from": a, "to": b, "kind": kind, "job_state": job_state},
            limit=limit,
            cursor=cursor,
        )
        return respond(request, [job_dto(r) for r in rows], page=page)


@router.post(
    "/api/jobs/{job_id}/cancel",
    operation_id="cancelJob",
    status_code=202,
    response_model=Envelope[JobDto],
    responses={200: {"model": Envelope[JobDto]}},
)
def cancel_job(request: Request, job_id: str):
    w = request.app.state.workbench

    def apply(conn):
        job = one(conn, s.jobs, job_id)
        return (200 if job["state"] in TERMINAL else 202), job_dto(w.queue.cancel(conn, job))

    return mutation(request, "cancelJob", {}, apply)


@router.get("/api/summaries", operation_id="listSummaries", response_model=ListEnvelope[SummaryDto])
def summaries(
    request: Request,
    type: Literal["hour", "day"],
    local_date: str,
    camera_ids: list[str] | None = Query(None),
    subject_id: str | None = None,
    limit: Limit = 50,
    cursor: str | None = None,
):
    w = request.app.state.workbench
    try:
        a, b = day_range(local_date)
    except ValueError:
        raise AppError(400, "INVALID_TIME_RANGE", "local_date 必须为 YYYY-MM-DD") from None
    with w.db.read() as conn:
        scope = w.ledger.scope(conn, camera_ids, subject_id, a, b)
        from app.security.secrets import canonical

        stmt = select(s.summaries).where(
            s.summaries.c.type == type,
            s.summaries.c.start_at_ms >= a,
            s.summaries.c.start_at_ms < b,
            s.summaries.c.scope_key == canonical(scope),
        )
        rows, page = page_rows(
            w,
            conn,
            stmt,
            s.summaries.c.start_at_ms,
            s.summaries.c.id,
            filters={"op": "listSummaries", "type": type, "local_date": local_date, "scope": scope},
            limit=limit,
            cursor=cursor,
        )
        return respond(request, [Summaries(w).get(conn, r["id"]) for r in rows], page=page)


@router.get("/api/summaries/{summary_id}", operation_id="getSummary", response_model=Envelope[SummaryDto])
def summary(request: Request, summary_id: str, revision: int | None = Query(None, ge=1)):
    w = request.app.state.workbench
    with w.db.read() as conn:
        return respond(request, Summaries(w).get(conn, summary_id, revision))


@router.post(
    "/api/summaries/regenerate",
    operation_id="regenerateSummary",
    status_code=202,
    response_model=Envelope[JobDto],
)
def regenerate(request: Request, dto: RegenerateSummary):
    w = request.app.state.workbench
    return mutation(
        request,
        "regenerateSummary",
        dto.model_dump(mode="json"),
        lambda conn: (202, Summaries(w).regenerate(conn, dto)),
    )


@router.get("/api/entities", operation_id="listEntities", response_model=ListEnvelope[EntityDto])
def entities(
    request: Request,
    kind: Literal["person", "object"] | None = None,
    enabled: bool | None = None,
    camera_id: str | None = None,
    limit: Limit = 50,
    cursor: str | None = None,
):
    w = request.app.state.workbench
    stmt = select(s.entities).where(s.entities.c.deleted_at_ms.is_(None))
    if kind:
        stmt = stmt.where(s.entities.c.kind == kind)
    if enabled is not None:
        stmt = stmt.where(s.entities.c.enabled == enabled)
    if camera_id:
        from sqlalchemy import exists, or_

        members = func.json_each(s.entities.c.camera_ids_json).table_valued("value")
        stmt = stmt.where(
            or_(
                func.json_array_length(s.entities.c.camera_ids_json) == 0,
                exists(select(1).select_from(members).where(members.c.value == camera_id)),
            )
        )
    with w.db.read() as conn:
        rows, page = page_rows(
            w,
            conn,
            stmt,
            s.entities.c.created_at_ms,
            s.entities.c.id,
            filters={"op": "listEntities", "kind": kind, "enabled": enabled, "camera_id": camera_id},
            limit=limit,
            cursor=cursor,
        )
        return respond(request, [w.assets.entity(conn, r["id"]) for r in rows], page=page)


@router.post(
    "/api/entities", operation_id="createEntity", status_code=201, response_model=Envelope[EntityDto]
)
def create_entity(request: Request, dto: EntityCreate):
    w = request.app.state.workbench
    return mutation(
        request,
        "createEntity",
        dto.model_dump(mode="json"),
        lambda conn: (201, w.assets.create_entity(conn, dto)),
    )


@router.get("/api/entities/{entity_id}", operation_id="getEntity", response_model=Envelope[EntityDto])
def get_entity(request: Request, entity_id: str):
    w = request.app.state.workbench
    with w.db.read() as conn:
        return respond(request, w.assets.entity(conn, entity_id))


@router.patch("/api/entities/{entity_id}", operation_id="updateEntity", response_model=Envelope[EntityDto])
def update_entity(request: Request, entity_id: str, dto: EntityPatch):
    w = request.app.state.workbench
    return mutation(
        request,
        "updateEntity",
        dto.model_dump(mode="json", exclude_unset=True),
        lambda conn: (
            200,
            w.assets.update_entity(
                conn, entity_id, dto.model_dump(exclude_unset=True), request.headers.get("If-Match")
            ),
        ),
    )


@router.delete(
    "/api/entities/{entity_id}", operation_id="deleteEntity", status_code=202, response_model=Envelope[JobDto]
)
def delete_entity(request: Request, entity_id: str):
    w = request.app.state.workbench
    return mutation(
        request,
        "deleteEntity",
        {},
        lambda conn: (202, w.assets.delete_entity(conn, entity_id, request.headers.get("If-Match"))),
    )


def store_reference(
    request,
    operation,
    entity_id,
    raw,
    mime,
    name,
    crop,
    enabled,
    *,
    asset_id=None,
    source_evidence_id=None,
    payload_extra=None,
):
    w = request.app.state.workbench
    payload = {
        "sha256": digest(raw),
        "name": name,
        "crop": crop.model_dump() if crop else None,
        "enabled": enabled,
        **(payload_extra or {}),
    }
    # 在解码、文件写入和资源版本检查前处理幂等重放。
    key = request.headers.get("Idempotency-Key")
    from app.security.secrets import fingerprint

    if key:
        with w.db.read() as conn:
            replay = w.replay(
                conn,
                request.state.session["user_id"],
                operation,
                key,
                fingerprint({"path": request.url.path, "payload": payload}),
            )
        if replay:
            return respond(request, replay[1], replay[0])
    data, width, height = decode_image(raw, mime, crop)
    path = w.assets.store("references", data)
    prepared = {"relpath": path, "bytes": len(data), "width": width, "height": height, "sha256": digest(data)}
    used = False

    def apply(conn):
        nonlocal used
        result = w.assets.attach_reference(
            conn,
            entity_id,
            prepared,
            name,
            enabled,
            request.headers.get("If-Match"),
            source_evidence_id,
            asset_id,
        )
        used = True
        return (200 if asset_id else 201), result

    try:
        return mutation(request, operation, payload, apply)
    finally:
        # 事务回滚或并发幂等重放时，不留下新的孤儿文件。
        with w.db.read() as conn:
            exists = conn.execute(
                select(s.reference_asset_versions.c.asset_id).where(
                    s.reference_asset_versions.c.relpath == path
                )
            ).first()
        if not exists:
            safe_path(w.config.data_dir, path).unlink(missing_ok=True)


def crop_form(value):
    if not value:
        return None
    try:
        return Crop.model_validate_json(value)
    except ValueError:
        raise AppError(422, "VALIDATION_ERROR", "crop 必须是有效的归一化裁剪矩形") from None


@router.post(
    "/api/entities/{entity_id}/references",
    operation_id="uploadReference",
    status_code=201,
    response_model=Envelope[ReferenceDto],
)
def upload_reference(
    request: Request,
    entity_id: str,
    file: UploadFile = File(),
    name: str | None = Form(None, max_length=64),
    crop: str | None = Form(None),
    enabled: bool = Form(True),
):
    return store_reference(
        request,
        "uploadReference",
        entity_id,
        file.file.read(8 * 1024 * 1024 + 1),
        file.content_type,
        name,
        crop_form(crop),
        enabled,
    )


@router.post(
    "/api/entities/{entity_id}/references/from-evidence",
    operation_id="promoteReference",
    status_code=201,
    response_model=Envelope[ReferenceDto],
)
def promote_reference(request: Request, entity_id: str, dto: PromoteReference):
    w = request.app.state.workbench
    # 原请求成功后现场图可能已经过期，先重放再读取图。
    payload = dto.model_dump(mode="json")
    key = request.headers.get("Idempotency-Key")
    from app.security.secrets import fingerprint

    if key:
        with w.db.read() as conn:
            replay = w.replay(
                conn,
                request.state.session["user_id"],
                "promoteReference",
                key,
                fingerprint({"path": request.url.path, "payload": payload}),
            )
        if replay:
            return respond(request, replay[1], replay[0])
    raw, _ = w.assets.read_evidence(dto.evidence_id)
    data, width, height = decode_image(raw, "image/jpeg", dto.crop)
    path = w.assets.store("references", data)
    prepared = {"relpath": path, "bytes": len(data), "width": width, "height": height, "sha256": digest(data)}
    try:
        return mutation(
            request,
            "promoteReference",
            payload,
            lambda conn: (
                201,
                w.assets.attach_reference(
                    conn,
                    entity_id,
                    prepared,
                    dto.name,
                    dto.enabled,
                    request.headers.get("If-Match"),
                    dto.evidence_id,
                ),
            ),
        )
    finally:
        with w.db.read() as conn:
            exists = conn.execute(
                select(s.reference_asset_versions.c.asset_id).where(
                    s.reference_asset_versions.c.relpath == path
                )
            ).first()
        if not exists:
            safe_path(w.config.data_dir, path).unlink(missing_ok=True)


@router.patch(
    "/api/reference-assets/{asset_id}", operation_id="updateReference", response_model=Envelope[ReferenceDto]
)
def update_reference(request: Request, asset_id: str, dto: ReferencePatch):
    w = request.app.state.workbench
    return mutation(
        request,
        "updateReference",
        dto.model_dump(exclude_unset=True),
        lambda conn: (
            200,
            w.assets.update_reference(
                conn, asset_id, dto.model_dump(exclude_unset=True), request.headers.get("If-Match")
            ),
        ),
    )


@router.put(
    "/api/reference-assets/{asset_id}/content",
    operation_id="replaceReference",
    response_model=Envelope[ReferenceDto],
)
def replace_reference(
    request: Request, asset_id: str, file: UploadFile = File(), crop: str | None = Form(None)
):
    w = request.app.state.workbench
    with w.db.read() as conn:
        asset = one(conn, s.reference_assets, asset_id, deleted=True)
    return store_reference(
        request,
        "replaceReference",
        asset["entity_id"],
        file.file.read(8 * 1024 * 1024 + 1),
        file.content_type,
        None,
        crop_form(crop),
        asset["enabled"],
        asset_id=asset_id,
    )


@router.delete(
    "/api/reference-assets/{asset_id}",
    operation_id="deleteReference",
    status_code=202,
    response_model=Envelope[JobDto],
)
def delete_reference(request: Request, asset_id: str):
    w = request.app.state.workbench
    return mutation(
        request,
        "deleteReference",
        {},
        lambda conn: (202, w.assets.delete_reference(conn, asset_id, request.headers.get("If-Match"))),
    )


@router.get(
    "/api/reference-assets/{asset_id}/content",
    operation_id="getReferenceContent",
    response_class=Response,
    responses={200: {"content": {"image/jpeg": {}}}},
)
def reference_content(request: Request, asset_id: str, version: int | None = Query(None, ge=1)):
    return Response(
        request.app.state.workbench.assets.read_reference(asset_id, version), media_type="image/jpeg"
    )


@router.get("/api/providers", operation_id="listProviders", response_model=ListEnvelope[ProviderDto])
def providers(request: Request, limit: Limit = 50, cursor: str | None = None):
    w = request.app.state.workbench
    with w.db.read() as conn:
        rows, page = page_rows(
            w,
            conn,
            select(s.providers).where(s.providers.c.deleted_at_ms.is_(None)),
            s.providers.c.created_at_ms,
            s.providers.c.id,
            filters={"op": "listProviders"},
            limit=limit,
            cursor=cursor,
        )
        return respond(request, [w.provider(conn, r["id"]) for r in rows], page=page)


@router.post(
    "/api/providers", operation_id="createProvider", status_code=201, response_model=Envelope[ProviderDto]
)
def create_provider(request: Request, dto: ProviderWrite):
    w = request.app.state.workbench
    return mutation(
        request,
        "createProvider",
        dto.model_dump(mode="json"),
        lambda conn: (201, w.write_provider(conn, dto)),
    )


@router.patch(
    "/api/providers/{provider_id}", operation_id="updateProvider", response_model=Envelope[ProviderDto]
)
def update_provider(request: Request, provider_id: str, dto: ProviderWrite):
    w = request.app.state.workbench
    return mutation(
        request,
        "updateProvider",
        dto.model_dump(mode="json"),
        lambda conn: (200, w.write_provider(conn, dto, provider_id, request.headers.get("If-Match"))),
    )


@router.delete("/api/providers/{provider_id}", operation_id="deleteProvider", status_code=204)
def delete_provider(request: Request, provider_id: str):
    w = request.app.state.workbench

    def apply(conn):
        w.delete_provider(conn, provider_id, request.headers.get("If-Match"))
        return 204, None

    return mutation(request, "deleteProvider", {}, apply)


@router.post(
    "/api/providers/test", operation_id="testProvider", status_code=202, response_model=Envelope[JobDto]
)
def test_provider(request: Request, dto: ProviderTest):
    w = request.app.state.workbench

    def apply(conn):
        provider = one(conn, s.providers, dto.provider_id)
        if provider["revision"] != dto.config_revision:
            raise AppError(412, "REVISION_CONFLICT", "模型配置已变化")
        job = enqueue(
            conn,
            w.now(),
            "provider_test",
            {"provider_id": dto.provider_id, "provider_revision": dto.config_revision},
            f"provider-test:{dto.provider_id}:{dto.config_revision}:{request.headers.get('Idempotency-Key')}",
            total=1,
        )
        return 202, job_dto(job)

    return mutation(request, "testProvider", dto.model_dump(), apply)


@router.get("/api/settings", operation_id="getSettings", response_model=Envelope[SettingsDto])
def settings(request: Request):
    w = request.app.state.workbench
    with w.db.read() as conn:
        return respond(request, w.settings(conn))


@router.post(
    "/api/settings/preview", operation_id="previewSettings", response_model=Envelope[ImpactPreviewDto]
)
def preview_settings(request: Request, dto: SettingsPreview):
    w = request.app.state.workbench
    free = w.disk_free()
    with w.db.read() as conn:
        return respond(request, w.impact(conn, dto.patch, base_revision=dto.base_revision, disk_free=free))


@router.patch("/api/settings", operation_id="updateSettings", response_model=Envelope[SettingsUpdateDto])
def update_settings(request: Request, dto: SettingsUpdate):
    w = request.app.state.workbench
    free = w.disk_free()
    return mutation(
        request,
        "updateSettings",
        dto.model_dump(mode="json"),
        lambda conn: (200, w.update_settings(conn, dto, request.headers.get("If-Match"), disk_free=free)),
    )


@router.get("/api/status", operation_id="getStatus", response_model=Envelope[StatusDto])
def status(request: Request):
    from app.observability.status import status_data

    return respond(request, status_data(request.app.state.workbench))


@router.get("/api/usage", operation_id="getUsage", response_model=Envelope[UsageDto])
def usage(
    request: Request,
    from_: str = Query(alias="from"),
    to: str = Query(),
    group_by: Literal["hour", "day", "provider", "purpose"] = "day",
    provider_id: str | None = None,
):
    from app.observability.status import usage_data

    a, b = range_ms(from_, to)
    return respond(request, usage_data(request.app.state.workbench, a, b, group_by, provider_id))
