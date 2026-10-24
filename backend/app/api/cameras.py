"""Camera, overview and health REST routes."""
from __future__ import annotations
import asyncio,os
from urllib.parse import urlsplit
from fastapi import APIRouter,Depends,Request,Query,Header
from sqlalchemy import func,select
from sqlalchemy.orm import Session
from app.main import get_db
from app.models import Camera,Frame,FrameBlob,CameraGap,AIJob,AIUsage,AIBinding,RetentionPolicy,utc_now_ms
from app.retention import SafeFileDeleter
from app.schemas import CameraCreateRequest,CameraUpdateRequest,CameraTestRequest
from app.capture import CameraConfig,FFmpegCameraSource
from .common import fail,ok,require_session,require_write,camera_dto,frame_dto,ensure_camera,publish,audit,iso,encrypt_app_secret,decrypt_app_secret,idempotency_replay,idempotency_store
router=APIRouter()
def _source(request,payload):
    source=payload.source
    if source is None:return payload.rtsp_url
    url=source.rtsp_url
    if source.username is None and source.password is None:return url
    parsed=urlsplit(url)
    username=source.username or ""
    password=source.password or ""
    from urllib.parse import quote
    auth=quote(username,safe="")
    if source.password is not None:auth += ":" + quote(password,safe="")
    netloc=auth + "@" + (parsed.hostname or "")
    if parsed.port:netloc += f":{parsed.port}"
    return parsed._replace(netloc=netloc).geturl()
def _validate(request,url):
    if not url:return
    p=urlsplit(url)
    if p.scheme.lower() not in {"rtsp","rtsps"} or not p.hostname:fail(422,"RTSP_URL_INVALID","RTSP 地址无效")
    try: port=p.port or 554
    except ValueError:fail(422,"RTSP_URL_INVALID","RTSP 端口无效")
    ports={int(x.strip()) for x in os.getenv("APP_RTSP_ALLOWED_PORTS","554,8554").split(",") if x.strip().isdigit()}
    hosts={x.strip().lower() for x in os.getenv("APP_RTSP_ALLOWED_HOSTS","").split(",") if x.strip()}
    dev=request.app.state.config.environment in {"development","dev","test"}
    if port not in ports:fail(422,"RTSP_PORT_DENIED","RTSP 端口不在允许列表中")
    if not dev and p.hostname.lower() not in hosts:fail(422,"RTSP_HOST_DENIED","RTSP 主机不在部署允许列表中")
def _encrypt(request,url):return encrypt_app_secret(request,url) if url else None
@router.get("/overview")
def overview(request:Request,db:Session=Depends(get_db),_=Depends(require_session)):
    cameras=list(db.scalars(select(Camera).order_by(Camera.created_at_ms)))
    counts=dict(db.execute(select(AIJob.state,func.count()).group_by(AIJob.state)).all())
    root=request.app.state.config.data_dir; used=0
    for folder in (root/"frames",root/"thumbs"):
        if folder.exists():
            for f in folder.rglob("*"):
                if f.is_file():
                    try:used+=f.stat().st_size
                    except OSError:pass
    import shutil
    disk=shutil.disk_usage(root)
    return ok({"cameras":[camera_dto(x) for x in cameras],"current_window":{"now":iso(utc_now_ms()),"timezone":request.app.state.config.timezone},
      "jobs":{"by_state":counts,"queued":counts.get("queued",0)},"usage":{"requests":db.scalar(select(func.count(AIUsage.id))) or 0,
      "total_tokens":db.scalar(select(func.sum(AIUsage.total_tokens))) or 0},"storage":{"stored_bytes":used,"free_bytes":disk.free},
      "capabilities":{"capture":"configured","ai":bool(db.scalar(select(func.count(AIBinding.id))) or 0)}})
@router.get("/cameras")
def cameras(db:Session=Depends(get_db),_=Depends(require_session)):
    return ok([camera_dto(c) for c in db.scalars(select(Camera).order_by(Camera.created_at_ms.desc()))])
@router.post("/cameras",status_code=201)
def create_camera(payload:CameraCreateRequest,request:Request,db:Session=Depends(get_db),_=Depends(require_write),idempotency_key:str|None=Header(default=None,alias="Idempotency-Key")):
    replay=idempotency_replay(request,db,"camera.create",idempotency_key,payload.model_dump())
    if replay is not None:return replay
    url=_source(request,payload);_validate(request,url)
    policy=None
    if payload.retention_minutes:
        days=payload.retention_minutes/1440
        policy=RetentionPolicy(name=f"摄像头留存-{payload.name[:80]}",ordinary_frame_days=days,event_frame_days=days)
        db.add(policy);db.flush()
    item=Camera(name=payload.name.strip(),enabled=payload.enabled,sampling_interval_ms=payload.sampling_interval_ms,
      frame_width=payload.frame_width,frame_height=payload.frame_height,rtsp_url_encrypted=_encrypt(request,url),
      retention_policy=policy,status="starting" if payload.enabled else "offline")
    db.add(item);db.flush();audit(db,request,"camera.create","camera",item.id,{"name":item.name,"enabled":item.enabled})
    dto=camera_dto(item);body=ok(dto,status=201);replay=idempotency_store(request,db,body,201)
    if replay is not None:return replay
    db.commit();publish(request,"camera.status",{"camera":dto},item.id);return body
@router.get("/cameras/{camera_id}")
def camera(camera_id:str,db:Session=Depends(get_db),_=Depends(require_session)):
    return ok(camera_dto(ensure_camera(db,camera_id)))

@router.get("/cameras/{camera_id}/latest-frame")
def latest_frame(camera_id:str,request:Request,db:Session=Depends(get_db),_=Depends(require_session)):
    """Return the latest persisted thumbnail and an explicit freshness state."""
    item=ensure_camera(db,camera_id)
    frame=db.scalar(select(Frame).where(Frame.camera_id==item.id,Frame.state=="active").order_by(Frame.captured_at_ms.desc(),Frame.id.desc()))
    now=utc_now_ms()
    stale_after_ms=max(1_000,int(item.sampling_interval_ms or 1_000)*2)
    if frame is None:
        state="offline" if not item.enabled or item.status in {"offline","error"} else "unavailable"
        return ok({"camera_id":item.id,"frame":None,"state":state,"age_ms":None,"stale_after_ms":stale_after_ms,
          "camera_status":item.status})
    age_ms=max(0,now-int(frame.captured_at_ms))
    state="offline" if not item.enabled else ("stale" if age_ms>stale_after_ms else "fresh")
    return ok({"camera_id":item.id,"frame":frame_dto(frame),"state":state,"age_ms":age_ms,
      "stale_after_ms":stale_after_ms,"camera_status":item.status})
@router.patch("/cameras/{camera_id}")
def update_camera(camera_id:str,payload:CameraUpdateRequest,request:Request,db:Session=Depends(get_db),_=Depends(require_write)):
    item=ensure_camera(db,camera_id);url=_source(request,payload)
    if payload.expected_version is None:
        fail(428,"VERSION_REQUIRED","更新摄像头配置必须提供 expected_version")
    if int(payload.expected_version)!=int(item.config_version or 1):
        fail(409,"VERSION_CONFLICT","摄像头配置版本冲突",{"current_version":int(item.config_version or 1)})
    before=camera_dto(item)
    if url:_validate(request,url);item.rtsp_url_encrypted=_encrypt(request,url)
    elif payload.clear_credentials:item.rtsp_url_encrypted=None
    fields=payload.model_dump(exclude_unset=True,exclude={"expected_version","source","rtsp_url","clear_credentials","retention_minutes","timezone","metadata"})
    for key,value in fields.items():setattr(item,key,value)
    if payload.retention_minutes:
        days=payload.retention_minutes/1440
        if item.retention_policy is None:item.retention_policy=RetentionPolicy(name=f"摄像头留存-{item.name[:80]}")
        item.retention_policy.ordinary_frame_days=days;item.retention_policy.event_frame_days=days
    item.status="starting" if item.enabled else "offline";item.config_version=int(item.config_version or 1)+1;item.updated_at_ms=utc_now_ms()
    dto=camera_dto(item);audit(db,request,"camera.update","camera",item.id,{"before":before,"after":dto,"changed_fields":sorted(fields)})
    db.commit();publish(request,"camera.status",{"camera":dto},item.id);return ok(dto)

@router.delete("/cameras/{camera_id}/source")
def remove_camera_source(camera_id:str,request:Request,db:Session=Depends(get_db),_=Depends(require_write)):
    item=ensure_camera(db,camera_id)
    item.enabled=False;item.status="offline";item.rtsp_url_encrypted=None
    item.config_version=int(item.config_version or 1)+1;item.updated_at_ms=utc_now_ms()
    audit(db,request,"camera.source.remove","camera",item.id,{"preserved_history":True})
    db.commit();dto=camera_dto(item);publish(request,"camera.status",{"camera":dto},item.id)
    return ok({"camera":dto,"source_removed":True})
@router.delete("/cameras/{camera_id}")
def delete_camera(camera_id:str,request:Request,delete_history:bool=Query(False),db:Session=Depends(get_db),_=Depends(require_write)):
    item=ensure_camera(db,camera_id)
    if not delete_history:
        item.enabled=False;item.status="offline";item.config_version=int(item.config_version or 1)+1;item.updated_at_ms=utc_now_ms()
        audit(db,request,"camera.disable","camera",item.id,{"preserved_source":True,"preserved_history":True})
        db.commit();dto=camera_dto(item);publish(request,"camera.status",{"camera":dto},item.id)
        return ok({"deleted":False,"disabled":True,"camera":dto})

    # Stop future capture before taking the snapshot. Files are removed only
    # after the database transaction commits, and only through SafeFileDeleter.
    item.enabled=False;item.status="offline";item.config_version=int(item.config_version or 1)+1
    frames=list(db.scalars(select(Frame).where(Frame.camera_id==item.id)))
    paths=[path for frame in frames for path in (frame.relative_path,frame.thumbnail_relative_path) if path]
    blob_ids={frame.blob_sha256 for frame in frames if frame.blob_sha256}
    favorite_count=sum(1 for frame in frames if frame.favorite)
    audit(db,request,"camera.delete.request","camera",item.id,{"delete_history":True,"frame_count":len(frames),"favorite_count":favorite_count})
    db.delete(item);db.commit()
    # Each capture blob key is camera-scoped; remove its metadata with the
    # camera after the frame cascade has completed.
    for blob_id in blob_ids:
        blob=db.get(FrameBlob,blob_id)
        if blob is not None:db.delete(blob)
    db.commit()
    cleanup={"deleted":[],"bytes_freed":0,"failed":[]}
    try:
        deleter=SafeFileDeleter(request.app.state.config.data_dir)
        deleted,bytes_freed=deleter.delete(paths)
        cleanup.update({"deleted":deleted,"bytes_freed":bytes_freed})
    except Exception as exc:
        cleanup["failed"]=[type(exc).__name__]
    # FrameBlob rows can outlive a camera when deduplication has no remaining refs.
    # The file cleanup above remains safe even if a blob is shared by another camera.
    audit(db,request,"camera.delete.result","camera",camera_id,{"delete_history":True,"cleanup":cleanup})
    db.commit()
    return ok({"deleted":True,"cleanup":{"file_count":len(cleanup["deleted"]),"bytes_freed":cleanup["bytes_freed"],"failed":cleanup["failed"]}})
@router.post("/cameras/{camera_id}/test")
async def test_camera(camera_id:str,payload:CameraTestRequest,request:Request,db:Session=Depends(get_db),_=Depends(require_write)):
    item=ensure_camera(db,camera_id)
    if not item.rtsp_url_encrypted:fail(422,"CAMERA_SOURCE_MISSING","摄像头未配置 RTSP")
    try:url=decrypt_app_secret(request,item.rtsp_url_encrypted)
    except Exception:fail(503,"SECRET_DECRYPT_FAILED","摄像头凭据解密失败")
    _validate(request,url)
    source=FFmpegCameraSource(CameraConfig(
        camera_id=item.id,url=url,interval_ms=item.sampling_interval_ms,
        connect_timeout_s=payload.timeout_ms/1000,read_timeout_s=payload.timeout_ms/1000,
        max_width=item.frame_width or 1920,max_height=item.frame_height or 1080,
        source_options={"fps":"1"}))
    try:
        await asyncio.wait_for(asyncio.to_thread(source.start),payload.timeout_ms/1000+1)
        frame=await asyncio.wait_for(asyncio.to_thread(source.read_frame,payload.timeout_ms/1000),payload.timeout_ms/1000+1)
        if frame is None:
            audit(db,request,"camera.connection_test","camera",item.id,{"result":"offline"});db.commit()
            return ok({"ok":False,"status":"offline","message":"无法读取图像"})
        height,width=frame.frame.shape[:2]
        audit(db,request,"camera.connection_test","camera",item.id,{"result":"online","width":int(width),"height":int(height)});db.commit()
        return ok({"ok":True,"status":"online","width":int(width),"height":int(height)})
    except asyncio.TimeoutError:
        audit(db,request,"camera.connection_test","camera",item.id,{"result":"timeout"});db.commit()
        return ok({"ok":False,"status":"timeout","message":"连接超时"})
    except TimeoutError:
        audit(db,request,"camera.connection_test","camera",item.id,{"result":"timeout"});db.commit()
        return ok({"ok":False,"status":"timeout","message":"连接超时"})
    except Exception as exc:
        import logging;logging.getLogger("camera_ai_workbench").warning("camera_test_error camera_id=%s type=%s",item.id,type(exc).__name__)
        audit(db,request,"camera.connection_test","camera",item.id,{"result":"error","error_type":type(exc).__name__});db.commit()
        return ok({"ok":False,"status":"error","message":"摄像头连接测试失败"})
    finally:
        try:await asyncio.to_thread(source.stop)
        except Exception:pass
@router.post("/cameras/{camera_id}/reconnect")
def reconnect(camera_id:str,request:Request,db:Session=Depends(get_db),_=Depends(require_write)):
    item=ensure_camera(db,camera_id)
    if not item.enabled:fail(409,"CAMERA_DISABLED","请先启用摄像头")
    item.status="reconnecting";item.updated_at_ms=utc_now_ms();audit(db,request,"camera.reconnect","camera",item.id,{"result":"accepted"});db.commit();publish(request,"camera.status",{"camera_id":item.id,"status":item.status},item.id)
    return ok({"camera_id":item.id,"status":item.status,"accepted":True})
@router.get("/cameras/{camera_id}/health")
def camera_health(camera_id:str,db:Session=Depends(get_db),_=Depends(require_session)):
    item=ensure_camera(db,camera_id);since=utc_now_ms()-3600000
    n=db.scalar(select(func.count(Frame.id)).where(Frame.camera_id==item.id,Frame.captured_at_ms>=since)) or 0
    gaps=list(db.scalars(select(CameraGap).where(CameraGap.camera_id==item.id).order_by(CameraGap.start_at_ms.desc()).limit(100)))
    latest=db.scalar(select(Frame).where(Frame.camera_id==item.id,Frame.state=="active").order_by(Frame.captured_at_ms.desc(),Frame.id.desc()))
    now=utc_now_ms();age=None if item.last_frame_at_ms is None else max(0,now-int(item.last_frame_at_ms));stale_after=max(1_000,int(item.sampling_interval_ms or 1_000)*2)
    return ok({"camera_id":item.id,"status":item.status,"last_frame_at":iso(item.last_frame_at_ms),"frames_last_hour":n,"fps_last_hour":n/3600,
      "latest_frame_id":latest.id if latest else None,"age_ms":age,"stale_after_ms":stale_after,"stale":age is None or age>stale_after,
      "reconnect_count":sum(int(g.retry_count or 0) for g in gaps),
      "gaps":[{"id":g.id,"start":iso(g.start_at_ms),"end":iso(g.end_at_ms),"reason":g.reason,"retry_count":g.retry_count} for g in gaps]})
