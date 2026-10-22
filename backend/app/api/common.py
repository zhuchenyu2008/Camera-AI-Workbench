"""Common API DTO, auth, persistence and event helpers."""
from __future__ import annotations
from datetime import UTC, datetime
import base64
import hashlib
import json
from typing import Any
from fastapi import Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
from app.main import APIError, get_db
from app.security import decrypt_secret, encrypt_secret
from app.events import SQLiteOutbox
from app.models import AppSetting, AuditLog, utc_now_ms
from sqlalchemy import select

def fail(status:int, code:str, message:str, details:dict|None=None):
    raise APIError(status,code,message,details)
def ok(data:Any, *, meta:dict|None=None, status:int=200):
    return {"data":data,"meta":meta or {}}
def iso(ms:int|None):
    if ms is None:return None
    return datetime.fromtimestamp(ms/1000,UTC).isoformat().replace("+00:00","Z")
def encode_cursor(*values: Any) -> str:
    raw=json.dumps(list(values),ensure_ascii=False,separators=(",",":"),default=str).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

def decode_cursor(value: str, expected: int) -> tuple[Any, ...]:
    try:
        padded=value + "=" * (-len(value) % 4)
        decoded=json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
    except (ValueError, TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        fail(400,"INVALID_CURSOR","分页游标无效")
    if not isinstance(decoded,list) or len(decoded)!=expected:
        fail(400,"INVALID_CURSOR","分页游标无效")
    return tuple(decoded)
def ms(value:datetime)->int:
    if value.tzinfo is None: fail(422,"VALIDATION_ERROR","时间必须包含时区")
    return int(value.astimezone(UTC).timestamp()*1000)
def _idempotency_hash(payload: Any) -> str:
    raw=json.dumps(payload,ensure_ascii=False,sort_keys=True,separators=(",",":"),default=str).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()

def idempotency_replay(request: Request, db: Session, scope: str, key: str | None, payload: Any):
    if not key:
        return None
    key = key.strip()
    if not key or len(key) > 255:
        fail(400, "INVALID_IDEMPOTENCY_KEY", "Idempotency-Key 无效")
    table = request.app.state.api_idempotency
    now = utc_now_ms()
    row = db.execute(select(table).where(table.c.scope == scope, table.c.key == key)).mappings().first()
    if row is not None and int(row["expires_at_ms"]) <= now:
        db.execute(table.delete().where(table.c.scope == scope, table.c.key == key))
        row = None
    if row is None:
        request.state.idempotency = (scope, key, _idempotency_hash(payload))
        return None
    if row["request_hash"] != _idempotency_hash(payload):
        fail(409, "IDEMPOTENCY_CONFLICT", "同一 Idempotency-Key 对应了不同请求")
    return JSONResponse(row["response_json"], status_code=int(row["status_code"]))


def idempotency_store(request: Request, db: Session, response_payload: Any, status_code: int):
    """在资源事务内保存响应；并发重复请求只回放已提交的第一份结果。"""
    values = getattr(request.state, "idempotency", None)
    if not values:
        return None
    scope, key, request_hash = values
    table = request.app.state.api_idempotency
    now = utc_now_ms()
    try:
        with db.begin_nested():
            db.execute(table.insert().values(
                scope=scope,
                key=key,
                request_hash=request_hash,
                status_code=status_code,
                response_json=response_payload,
                created_at_ms=now,
                expires_at_ms=now + 86_400_000,
            ))
    except IntegrityError:
        row = db.execute(select(table).where(table.c.scope == scope, table.c.key == key)).mappings().first()
        if row is None:
            raise
        if row["request_hash"] != request_hash:
            fail(409, "IDEMPOTENCY_CONFLICT", "同一 Idempotency-Key 对应了不同请求")
        # The current resource mutation must not commit when another request won
        # the key.  The caller returns this response immediately.
        db.rollback()
        return JSONResponse(row["response_json"], status_code=int(row["status_code"]))
    return None
def setting(db:Session,key:str,default:Any):
    row=db.scalar(select(AppSetting).where(AppSetting.key==key))
    return row.value_json if row else default
def save_setting(db:Session,key:str,value:Any,description:str=""):
    row=db.scalar(select(AppSetting).where(AppSetting.key==key))
    if row is None: db.add(AppSetting(key=key,value_json=value,description=description))
    else: row.value_json=value; row.updated_at_ms=utc_now_ms()
def encrypt_app_secret(request: Request, value: str) -> bytes:
    return encrypt_secret(value, key=request.app.state.master_key)

def decrypt_app_secret(request: Request, value: bytes) -> str:
    return decrypt_secret(value, key=request.app.state.master_key).decode("utf-8")

def outbox(request:Request):
    return request.app.state.outbox
def publish(request:Request,event:str,data:dict,camera_id:str|None=None):
    outbox(request).publish(event,data,camera_id=camera_id,request_id=getattr(request.state,"request_id",None))
def require_session(request:Request,db:Session=Depends(get_db)):
    if request.app.state.config.auth_disabled:return None
    auth=request.headers.get("Authorization","")
    token=auth[7:].strip() if auth.lower().startswith("bearer ") else request.cookies.get(request.app.state.auth.cookie_name)
    if not token:fail(401,"AUTH_REQUIRED","请先登录")
    session=request.app.state.auth.authenticate(db,token)
    request.state.auth_session=session; request.state.auth_token=token
    return session
def require_write(request:Request,session=Depends(require_session)):
    if session is not None and not request.headers.get("Authorization","").lower().startswith("bearer "):
        request.app.state.auth.validate_csrf(session,request.headers.get("X-CSRF-Token"))
    return session
def audit(db:Session,request:Request,action:str,target_type:str|None=None,target_id:str|None=None,details:dict|None=None):
    db.add(AuditLog(action=action,target_type=target_type,target_id=target_id,
      request_id=getattr(request.state,"request_id",None),details_json=details or {}))
def camera_dto(c):
    return {"id":c.id,"name":c.name,"enabled":c.enabled,"status":c.status,
      "source_configured":bool(c.rtsp_url_encrypted),"source_host":None,
      "sampling_interval_ms":c.sampling_interval_ms,"frame_width":c.frame_width,"frame_height":c.frame_height,
      "last_frame_at":iso(c.last_frame_at_ms),"created_at":iso(c.created_at_ms),"updated_at":iso(c.updated_at_ms),"version":1}
def job_dto(j):
    return {"id":j.id,"layer":j.layer,"camera_id":j.camera_id,"state":j.state,"priority":j.priority,
      "period_start":iso(j.period_start_ms),"period_end":iso(j.period_end_ms),"attempts":j.attempts,
      "max_attempts":j.max_attempts,"available_at":iso(j.available_at_ms),"started_at":iso(j.started_at_ms),
      "finished_at":iso(j.finished_at_ms),"error_code":j.error_code,"error_message":j.error_safe_message,
      "input_frame_count":len(j.input_frame_ids_json or []),"created_at":iso(j.created_at_ms)}
def segment_dto(s):
    return {"id":s.id,"camera_id":s.camera_id,"l2_window_id":s.l2_window_id,
      "start":iso(s.start_at_ms),"end":iso(s.end_at_ms),"canonical_activity_id":s.canonical_activity_id,
      "canonical_activity":s.canonical_activity.name if s.canonical_activity else None,
      "display_description":s.display_description,"category":s.category,"confidence":s.confidence,
      "evidence_coverage":s.evidence_coverage,"state":s.state,"locked":s.locked,"version":s.version,
      "source_observation_ids":s.source_observation_ids_json,
      "intervals":[{"id":i.id,"start":iso(i.start_at_ms),"end":iso(i.end_at_ms),"evidence_kind":i.evidence_kind,
       "confidence":i.confidence,"reason":i.reason,"anchor_frame_ids":i.anchor_frame_ids_json} for i in sorted(s.intervals,key=lambda x:x.start_at_ms)]}
def frame_dto(f):
    expired=f.state!="active"
    return {"id":f.id,"camera_id":f.camera_id,"captured_at":iso(f.captured_at_ms),"width":f.width,"height":f.height,
      "byte_size":f.byte_size,"state":"expired" if expired else "available","favorite":f.favorite,"expired":expired,
      "content_url":None if expired else f"/api/v1/frames/{f.id}/content",
      "thumbnail_url":None if expired or not f.thumbnail_relative_path else f"/api/v1/frames/{f.id}/thumbnail"}
def summary_dto(s):
    return {"id":s.id,"camera_id":s.camera_id,"period_kind":s.period_kind,"period_start":iso(s.period_start_ms),
      "period_end":iso(s.period_end_ms),"timezone":s.timezone,"body":s.body,"statistics":s.statistics_json,
      "source_segment_ids":s.source_segment_ids_json,"state":s.state,"version":s.version,"stale":s.stale,
      "created_at":iso(s.created_at_ms),"updated_at":iso(s.updated_at_ms)}
def provider_dto(p):
    from urllib.parse import urlsplit
    u=urlsplit(p.base_url)
    return {"id":p.id,"label":p.label,"base_url":f"{u.scheme}://{u.netloc}{u.path}".rstrip("/"),
      "base_host":u.hostname,"timeout_ms":p.timeout_ms,"enabled":p.enabled,
      "allow_insecure_local":bool(p.allow_insecure_local),
      "api_key_configured":bool(p.api_key_ciphertext),"capabilities":p.capability_json or {},"version":1}
def activity_dto(item,db):
    from app.models import ActivitySegment
    from sqlalchemy import func,select
    return {"id":item.id,"name":item.name,"category":item.category,"aliases":[x.alias for x in item.aliases],
      "enabled":item.enabled,"description":item.description,"usage_count":db.scalar(select(func.count(ActivitySegment.id)).where(ActivitySegment.canonical_activity_id==item.id)) or 0,"version":1}
def ensure_camera(db,camera_id):
    from app.models import Camera
    item=db.get(Camera,camera_id)
    if item is None:fail(404,"CAMERA_NOT_FOUND","摄像头不存在")
    return item
def ensure_segment(db,segment_id):
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload
    from app.models import ActivitySegment
    item=db.scalar(select(ActivitySegment).options(selectinload(ActivitySegment.intervals),selectinload(ActivitySegment.canonical_activity)).where(ActivitySegment.id==segment_id))
    if item is None:fail(404,"SEGMENT_NOT_FOUND","活动段不存在")
    return item
