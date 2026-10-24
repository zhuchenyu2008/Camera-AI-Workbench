"""Jobs, summaries, usage, anonymous clusters, events and health endpoints."""
from __future__ import annotations
from datetime import datetime
from fastapi import APIRouter,Depends,Request,Query
from fastapi.responses import JSONResponse
from sqlalchemy import and_,or_,func,select
from sqlalchemy.orm import Session
import shutil
from app.main import get_db
from app.models import AIJob,AIUsage,Summary,PersonCluster,PersonAppearance,ActivitySegment,AuditLog,Camera,AIBinding,AIProvider,utc_now_ms
from app.schemas import SummaryRegenerateRequest,PersonMergeRequest,PersonDisableRequest,DiagnosticsActionRequest
from app.events import last_event_id_from_headers,sse_response
from app.runtime_state import read_heartbeats
from .common import fail,ok,require_session,require_write,iso,job_dto,summary_dto,publish,encode_cursor,decode_cursor,audit
router=APIRouter();health_router=APIRouter();legacy_router=APIRouter()
@router.get("/summaries")
def summaries(camera_id:str|None=None,period_kind:str|None=None,from_:datetime|None=Query(None,alias="from"),to:datetime|None=None,
 db:Session=Depends(get_db),_=Depends(require_session)):
 q=select(Summary).order_by(Summary.period_start_ms.desc()).limit(200)
 if camera_id:q=q.where(Summary.camera_id==camera_id)
 if period_kind:q=q.where(Summary.period_kind==period_kind)
 if from_:
  if from_.tzinfo is None:fail(422,"VALIDATION_ERROR","时间必须包含时区")
  q=q.where(Summary.period_end_ms>int(from_.timestamp()*1000))
 if to:
  if to.tzinfo is None:fail(422,"VALIDATION_ERROR","时间必须包含时区")
  q=q.where(Summary.period_start_ms<int(to.timestamp()*1000))
 return ok([summary_dto(x) for x in db.scalars(q)])
@router.post("/summaries/{summary_id}/regenerate",status_code=202)
def regenerate(summary_id:str,payload:SummaryRegenerateRequest,request:Request,db:Session=Depends(get_db),_=Depends(require_write),idempotency_key:str|None=Query(default=None,alias="idempotency_key")):
 s=db.get(Summary,summary_id)
 if s is None:fail(404,"SUMMARY_NOT_FOUND","摘要不存在")
 key=idempotency_key or f"summary:{s.id}:{s.version+1}"
 job=db.scalar(select(AIJob).where(AIJob.idempotency_key==key))
 if job is None:
  job=AIJob(idempotency_key=key,layer="L3",camera_id=s.camera_id,period_start_ms=s.period_start_ms,period_end_ms=s.period_end_ms,
   priority=200,input_version=s.version,prompt_version="v1",schema_version="v1",input_frame_ids_json=[],
   input_ref_json={"summary_id":s.id,"source_segment_ids":s.source_segment_ids_json})
  db.add(job);s.ai_job=job;s.state="queued";audit(db,request,"summary.regenerate","summary",s.id,{"job_id":job.id,"force":payload.force});db.commit()
 return ok(job_dto(job),status=202)
@router.get("/jobs")
def jobs(state:str|None=None,layer:str|None=None,camera_id:str|None=None,limit:int=Query(50,ge=1,le=200),cursor:str|None=None,
 db:Session=Depends(get_db),_=Depends(require_session)):
 filters=[]
 if state:filters.append(AIJob.state==state)
 if layer:filters.append(AIJob.layer==layer.upper())
 if camera_id:filters.append(AIJob.camera_id==camera_id)
 if cursor:
  created,cursor_id=decode_cursor(cursor,2)
  try:created=int(created)
  except (TypeError,ValueError):fail(400,"INVALID_CURSOR","分页游标无效")
  filters.append(or_(AIJob.created_at_ms<created,and_(AIJob.created_at_ms==created,AIJob.id<str(cursor_id))))
 rows=list(db.scalars(select(AIJob).where(*filters).order_by(AIJob.created_at_ms.desc(),AIJob.id.desc()).limit(limit+1)))
 has_more=len(rows)>limit;rows=rows[:limit]
 next_cursor=encode_cursor(rows[-1].created_at_ms,rows[-1].id) if has_more and rows else None
 return ok([job_dto(x) for x in rows],meta={"next_cursor":next_cursor,"limit":limit,"has_more":has_more})
@router.post("/jobs/{job_id}/retry",status_code=202)
def retry(job_id:str,request:Request,db:Session=Depends(get_db),_=Depends(require_write)):
 j=db.get(AIJob,job_id)
 if j is None:fail(404,"JOB_NOT_FOUND","任务不存在")
 if j.state not in {"failed","needs_review","evidence_expired"}:fail(409,"JOB_NOT_RETRYABLE","任务当前不可重试")
 j.state="queued";j.attempts=0;j.available_at_ms=utc_now_ms();j.finished_at_ms=None;j.error_code=None;j.error_safe_message=None;j.updated_at_ms=utc_now_ms();audit(db,request,"job.retry","job",j.id,{"camera_id":j.camera_id});db.commit()
 publish(request,"job.updated",job_dto(j),j.camera_id);return ok(job_dto(j),status=202)
@router.get("/ai/usage")
def usage(request:Request,from_:datetime=Query(alias="from"),to:datetime=Query(),camera_id:str|None=None,provider_id:str|None=None,layer:str|None=None,
 group_by:str="day",limit:int=Query(50,ge=1,le=200),cursor:str|None=None,db:Session=Depends(get_db),_=Depends(require_session)):
 if from_.tzinfo is None or to.tzinfo is None:fail(422,"VALIDATION_ERROR","时间必须包含时区")
 start,end=int(from_.timestamp()*1000),int(to.timestamp()*1000)
 if end<=start:fail(422,"INVALID_TIME_RANGE","查询区间无效")
 if group_by not in {"hour","day","provider","purpose","layer"}:fail(422,"INVALID_GROUP_BY","group_by 必须是 hour/day/provider/purpose/layer")
 filters=[AIUsage.created_at_ms>=start,AIUsage.created_at_ms<end]
 if provider_id:filters.append(AIUsage.provider_id==provider_id)
 if layer:filters.append(AIUsage.layer==layer.upper())
 if camera_id:filters.append(AIUsage.job_id.in_(select(AIJob.id).where(AIJob.camera_id==camera_id)))
 totals_row=db.execute(select(func.count(AIUsage.id),func.coalesce(func.sum(AIUsage.prompt_tokens),0),
  func.coalesce(func.sum(AIUsage.completion_tokens),0),func.coalesce(func.sum(AIUsage.total_tokens),0),
  func.coalesce(func.sum(AIUsage.image_bytes),0),func.coalesce(func.sum(AIUsage.cost_estimate),0)).where(*filters)).one()
 totals={"requests":int(totals_row[0] or 0),"prompt_tokens":int(totals_row[1] or 0),
  "completion_tokens":int(totals_row[2] or 0),"total_tokens":int(totals_row[3] or 0),
  "image_bytes":int(totals_row[4] or 0),"estimated_cost":float(totals_row[5] or 0)}
 metric_columns=(func.count(AIUsage.id).label("requests"),
  func.coalesce(func.sum(AIUsage.total_tokens),0).label("total_tokens"),
  func.coalesce(func.sum(AIUsage.image_bytes),0).label("image_bytes"),
  func.coalesce(func.sum(AIUsage.cost_estimate),0).label("estimated_cost"))
 by_provider_rows=db.execute(select(AIUsage.provider_id,AIUsage.model_name,*metric_columns).where(*filters)
  .group_by(AIUsage.provider_id,AIUsage.model_name).order_by(AIUsage.provider_id,AIUsage.model_name)).all()
 by_layer_rows=db.execute(select(AIUsage.layer,*metric_columns).where(*filters).group_by(AIUsage.layer).order_by(AIUsage.layer)).all()
 by_provider=[{"provider_id":r[0],"model_name":r[1],"requests":r[2],"total_tokens":r[3],"image_bytes":r[4],"estimated_cost":float(r[5] or 0)} for r in by_provider_rows]
 by_layer=[{"layer":r[0],"requests":r[1],"total_tokens":r[2],"image_bytes":r[3],"estimated_cost":float(r[4] or 0)} for r in by_layer_rows]
 if group_by in {"hour","day"}:
  fmt="%Y-%m-%d %H:00" if group_by=="hour" else "%Y-%m-%d"
  bucket=func.strftime(fmt,func.datetime(AIUsage.created_at_ms/1000.0,"unixepoch"))
  bucket_column=bucket.label("bucket")
  bucket_rows=db.execute(select(bucket_column,*metric_columns).where(*filters).group_by(bucket).order_by(bucket)).all()
  buckets=[{"bucket":r[0],"requests":r[1],"total_tokens":r[2],"image_bytes":r[3],"estimated_cost":float(r[4] or 0)} for r in bucket_rows]
 elif group_by=="provider":buckets=by_provider
 else:buckets=by_layer
 page_filters=list(filters)
 if cursor:
  created,cursor_id=decode_cursor(cursor,2)
  try:created=int(created)
  except (TypeError,ValueError):fail(400,"INVALID_CURSOR","分页游标无效")
  page_filters.append(or_(AIUsage.created_at_ms<created,and_(AIUsage.created_at_ms==created,AIUsage.id<str(cursor_id))))
 q=select(AIUsage).where(*page_filters).order_by(AIUsage.created_at_ms.desc(),AIUsage.id.desc()).limit(limit+1)
 rows=list(db.scalars(q));has_more=len(rows)>limit;rows=rows[:limit]
 items=[{"id":x.id,"job_id":x.job_id,"provider_id":x.provider_id,"layer":x.layer,"model_name":x.model_name,"frame_count":x.frame_count,
  "image_bytes":x.image_bytes,"prompt_tokens":x.prompt_tokens,"completion_tokens":x.completion_tokens,"total_tokens":x.total_tokens,
  "latency_ms":x.latency_ms,"retry_index":x.retry_index,"tool_count":x.tool_count,"cost_estimate":x.cost_estimate,"currency":x.currency,"created_at":iso(x.created_at_ms)} for x in rows]
 next_cursor=encode_cursor(rows[-1].created_at_ms,rows[-1].id) if has_more and rows else None
 return ok({"totals":totals,"buckets":buckets,"by_provider":by_provider,"by_layer":by_layer,"items":items},
  meta={"next_cursor":next_cursor,"limit":limit,"has_more":has_more,"timezone":request.app.state.config.timezone})
@router.get("/persons")
def persons(camera_id:str|None=None,db:Session=Depends(get_db),_=Depends(require_session)):
 q=select(PersonCluster).order_by(PersonCluster.last_seen_at_ms.desc())
 if camera_id:q=q.where(PersonCluster.camera_id==camera_id)
 rows=list(db.scalars(q));return ok([{"id":p.id,"label":p.anonymous_label,"enabled":p.enabled,"first_seen_at":iso(p.first_seen_at_ms),
  "last_seen_at":iso(p.last_seen_at_ms),"occurrence_count":db.scalar(select(func.count(PersonAppearance.id)).where(PersonAppearance.cluster_id==p.id)) or 0,"version":1} for p in rows])
@router.post("/persons/{person_id}/merge")
def merge_person(person_id:str,payload:PersonMergeRequest,request:Request,db:Session=Depends(get_db),_=Depends(require_write)):
 target=db.get(PersonCluster,person_id)
 if target is None:fail(404,"PERSON_NOT_FOUND","匿名人物不存在")
 ids=set(payload.source_person_ids)|{person_id};rows=list(db.scalars(select(PersonCluster).where(PersonCluster.id.in_(ids))))
 if len(rows)!=len(ids):fail(404,"PERSON_NOT_FOUND","部分匿名人物不存在")
 if any(x.camera_id!=target.camera_id for x in rows):fail(422,"PERSON_CAMERA_MISMATCH","只能合并同一摄像头内的人物")
 for x in rows:
  if x.id==target.id:continue
  for appearance in db.scalars(select(PersonAppearance).where(PersonAppearance.cluster_id==x.id)):appearance.cluster_id=target.id
  x.enabled=False
 audit(db,request,"person.merge","person",target.id,{"merged_ids":[x.id for x in rows if x.id!=target.id]});db.commit();return ok({"id":target.id,"merged_ids":[x.id for x in rows if x.id!=target.id]})
@router.post("/persons/{person_id}/disable")
def disable_person(person_id:str,payload:PersonDisableRequest,request:Request,db:Session=Depends(get_db),_=Depends(require_write)):
 p=db.get(PersonCluster,person_id)
 if p is None:fail(404,"PERSON_NOT_FOUND","匿名人物不存在")
 for appearance in db.scalars(select(PersonAppearance).where(PersonAppearance.cluster_id==p.id)):
  appearance.cluster_id=None
  if hasattr(appearance,"feature_ciphertext"):appearance.feature_ciphertext=None
 p.enabled=False;audit(db,request,"person.disable","person",p.id,{"features_removed":True,"reason":payload.reason});db.commit();return ok({"id":p.id,"enabled":False,"features_removed":True})
@router.get("/audit-logs")
def audit_logs(action:str|None=None,target_type:str|None=None,target_id:str|None=None,camera_id:str|None=None,
 from_:datetime|None=Query(None,alias="from"),to:datetime|None=None,request_id:str|None=None,
 limit:int=Query(50,ge=1,le=200),cursor:str|None=None,db:Session=Depends(get_db),_=Depends(require_session)):
    """分页查询脱敏后的操作审计，不返回密码、凭据或图像内容。"""
    filters=[]
    if action:filters.append(AuditLog.action==action)
    if target_type:filters.append(AuditLog.target_type==target_type)
    if target_id:filters.append(AuditLog.target_id==target_id)
    if camera_id:filters.append(AuditLog.target_id==camera_id)
    if request_id:filters.append(AuditLog.request_id==request_id)
    if from_:
        if from_.tzinfo is None:fail(422,"VALIDATION_ERROR","时间必须包含时区")
        filters.append(AuditLog.created_at_ms>=int(from_.timestamp()*1000))
    if to:
        if to.tzinfo is None:fail(422,"VALIDATION_ERROR","时间必须包含时区")
        filters.append(AuditLog.created_at_ms<int(to.timestamp()*1000))
    if cursor:
        created,log_id=decode_cursor(cursor,2)
        try:created=int(created)
        except (TypeError,ValueError):fail(400,"INVALID_CURSOR","分页游标无效")
        filters.append(or_(AuditLog.created_at_ms<created,
          and_(AuditLog.created_at_ms==created,AuditLog.id<str(log_id))))
    rows=list(db.scalars(select(AuditLog).where(*filters).order_by(AuditLog.created_at_ms.desc(),AuditLog.id.desc()).limit(limit+1)))
    has_more=len(rows)>limit;rows=rows[:limit]
    items=[{"id":row.id,"actor_id":row.actor_id,"action":row.action,"target_type":row.target_type,
      "target_id":row.target_id,"request_id":row.request_id,"details":row.details_json,"created_at":iso(row.created_at_ms)} for row in rows]
    next_cursor=encode_cursor(rows[-1].created_at_ms,rows[-1].id) if has_more and rows else None
    return ok(items,meta={"next_cursor":next_cursor,"limit":limit,"has_more":has_more})

@router.get("/events")
def events(request:Request,_=Depends(require_session)):
 return sse_response(request.app.state.outbox,request=request,last_event_id=last_event_id_from_headers(request.headers))
@health_router.get("/health/live")
def live():return {"status":"ok","alive":True,"timestamp_ms":utc_now_ms()}
@health_router.get("/health/ready")
def ready(request:Request,db:Session=Depends(get_db)):
 from app.health import readiness
 heartbeats=read_heartbeats(db)
 worker=heartbeats.get("worker") or {"status":"unknown","ok":None,"detail":"heartbeat_missing"}
 scheduler=heartbeats.get("scheduler") or {"status":"unknown","ok":None,"detail":"heartbeat_missing"}
 result=readiness(database=request.app.state.engine,data_dir=request.app.state.config.data_dir,
  migrations_complete=request.app.state.migrations_ready,worker_configured=worker,scheduler_configured=scheduler)
 # A missing optional service heartbeat is reported as unknown so local API
 # clients can still inspect the health page; an explicitly stale/offline
 # service remains a conventional 503 readiness failure.
 checks=result.get("checks",{})
 blocking=any(checks.get(name,{}).get("ok") is False for name in ("database","disk","migrations","worker","scheduler"))
 return JSONResponse(result,status_code=503 if blocking else 200)
@health_router.get("/health/dependencies")
def dependencies(request:Request,db:Session=Depends(get_db)):
 from app.health import dependencies as check
 now=utc_now_ms();heartbeats=read_heartbeats(db,now_ms=now)
 camera_checks={}
 for camera in db.scalars(select(Camera).order_by(Camera.created_at_ms)):
  if not camera.enabled:
   camera_checks[camera.id]={"status":"disabled","ok":None,"last_frame_at_ms":camera.last_frame_at_ms}
   continue
  age=None if camera.last_frame_at_ms is None else max(0,now-int(camera.last_frame_at_ms))
  stale_after=max(1_000,int(camera.sampling_interval_ms or 1_000)*2)
  healthy=camera.status=="online" and age is not None and age<=stale_after
  camera_checks[camera.id]={"status":"online" if healthy else (camera.status or "offline"),"ok":healthy,
    "last_frame_at_ms":camera.last_frame_at_ms,"age_ms":age,"stale_after_ms":stale_after}
 ai_checks={}
 providers={p.id:p for p in db.scalars(select(AIProvider))}
 for binding in db.scalars(select(AIBinding)):
  provider=providers.get(binding.provider_id)
  caps=provider.capability_json if provider else {}
  tested=caps.get("last_test_error") if isinstance(caps,dict) else None
  ai_checks[binding.layer.lower()]={"status":"online" if provider and provider.enabled and not tested else "degraded",
    "ok":bool(provider and provider.enabled and not tested),"provider_id":binding.provider_id,
    "last_test_at_ms":caps.get("last_test_at_ms") if isinstance(caps,dict) else None}
 if not ai_checks:
  ai_checks["default"]={"status":"unconfigured","ok":None}
 report=check(database=request.app.state.engine,data_dir=request.app.state.config.data_dir,
   cameras=camera_checks,ai=ai_checks)
 report["services"]={name:value for name,value in heartbeats.items() if name in {"worker","scheduler","capture"}}
 return report


@router.get("/diagnostics/events")
def diagnostics_events(request:Request,limit:int=Query(100,ge=1,le=200),db:Session=Depends(get_db),_=Depends(require_session)):
    """Aggregate actionable runtime diagnostics from health and task state."""
    now=utc_now_ms();events=[]
    heartbeat=read_heartbeats(db,now_ms=now)
    for service in ("worker","scheduler","capture"):
        item=heartbeat.get(service)
        if item and item.get("status") not in {"running","online"}:
            events.append({"id":f"service:{service}","severity":"critical","source":"service","event_type":"service.offline",
              "service":service,"message":f"{service} 服务心跳已过期或处于降级状态","state":"open",
              "created_at":iso(item.get("updated_at_ms"))})
    cameras=list(db.scalars(select(Camera).where(Camera.enabled.is_(True))))
    for camera in cameras:
        age=None if camera.last_frame_at_ms is None else max(0,now-int(camera.last_frame_at_ms))
        stale_after=max(1_000,int(camera.sampling_interval_ms or 1_000)*2)
        if camera.status!="online" or age is None or age>stale_after:
            events.append({"id":f"camera:{camera.id}","severity":"critical" if camera.status in {"offline","error"} else "warning",
              "source":"capture","event_type":"camera.stalled","camera_id":camera.id,"message":f"{camera.name} 未产生新帧",
              "state":"open","age_ms":age,"created_at":iso(camera.last_frame_at_ms)})
    for job in db.scalars(select(AIJob).where(AIJob.state.in_(("failed","needs_review","evidence_expired"))).order_by(AIJob.updated_at_ms.desc()).limit(limit)):
        events.append({"id":f"job:{job.id}","severity":"warning","source":"worker","event_type":"job.failed","job_id":job.id,
          "camera_id":job.camera_id,"message":job.error_safe_message or "分析任务需要处理","state":"open","created_at":iso(job.updated_at_ms)})
    try:
        free_bytes=shutil.disk_usage(request.app.state.config.data_dir).free
        if free_bytes<512*1024*1024:
            events.append({"id":"storage:disk","severity":"critical","source":"storage","event_type":"storage.low_disk",
              "message":"可用磁盘空间低于 512 MB","state":"open","free_bytes":int(free_bytes),"created_at":iso(now)})
    except OSError:pass
    events.sort(key=lambda item:(item.get("severity")!="critical",item.get("created_at") or ""))
    return ok(events[:limit])


@router.post("/diagnostics/actions",status_code=202)
def diagnostics_action(payload:DiagnosticsActionRequest,request:Request,db:Session=Depends(get_db),_=Depends(require_write)):
    if payload.action=="reconnect_camera":
        camera=db.get(Camera,payload.target_id)
        if camera is None:fail(404,"CAMERA_NOT_FOUND","摄像头不存在")
        if not camera.enabled:fail(409,"CAMERA_DISABLED","请先启用摄像头")
        camera.status="reconnecting";camera.updated_at_ms=utc_now_ms()
        audit(db,request,"diagnostics.camera.reconnect","camera",camera.id,{"source":"diagnostics"});db.commit()
        publish(request,"camera.status",{"camera_id":camera.id,"status":camera.status},camera.id)
        return ok({"accepted":True,"action":payload.action,"target_id":camera.id},status=202)
    job=db.get(AIJob,payload.target_id)
    if job is None:fail(404,"JOB_NOT_FOUND","任务不存在")
    if job.state not in {"failed","needs_review","evidence_expired"}:fail(409,"JOB_NOT_RETRYABLE","任务当前不可重试")
    job.state="queued";job.attempts=0;job.available_at_ms=utc_now_ms();job.finished_at_ms=None;job.error_code=None;job.error_safe_message=None;job.updated_at_ms=utc_now_ms()
    audit(db,request,"diagnostics.job.retry","job",job.id,{"source":"diagnostics"});db.commit();publish(request,"job.updated",job_dto(job),job.camera_id)
    return ok({"accepted":True,"action":payload.action,"target_id":job.id},status=202)
