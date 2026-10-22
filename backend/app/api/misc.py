"""Jobs, summaries, usage, anonymous clusters, events and health endpoints."""
from __future__ import annotations
from datetime import datetime
from fastapi import APIRouter,Depends,Request,Query
from fastapi.responses import JSONResponse
from sqlalchemy import and_,or_,func,select
from sqlalchemy.orm import Session
from app.main import get_db
from app.models import AIJob,AIUsage,Summary,PersonCluster,PersonAppearance,ActivitySegment,utc_now_ms
from app.schemas import SummaryRegenerateRequest,PersonMergeRequest,PersonDisableRequest
from app.events import last_event_id_from_headers,sse_response
from .common import fail,ok,require_session,require_write,iso,job_dto,summary_dto,publish,encode_cursor,decode_cursor
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
  db.add(job);s.ai_job=job;s.state="queued";db.commit()
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
 j.state="queued";j.attempts=0;j.available_at_ms=utc_now_ms();j.finished_at_ms=None;j.error_code=None;j.error_safe_message=None;j.updated_at_ms=utc_now_ms();db.commit()
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
def merge_person(person_id:str,payload:PersonMergeRequest,db:Session=Depends(get_db),_=Depends(require_write)):
 target=db.get(PersonCluster,person_id)
 if target is None:fail(404,"PERSON_NOT_FOUND","匿名人物不存在")
 ids=set(payload.source_person_ids)|{person_id};rows=list(db.scalars(select(PersonCluster).where(PersonCluster.id.in_(ids))))
 if len(rows)!=len(ids):fail(404,"PERSON_NOT_FOUND","部分匿名人物不存在")
 if any(x.camera_id!=target.camera_id for x in rows):fail(422,"PERSON_CAMERA_MISMATCH","只能合并同一摄像头内的人物")
 for x in rows:
  if x.id==target.id:continue
  for appearance in db.scalars(select(PersonAppearance).where(PersonAppearance.cluster_id==x.id)):appearance.cluster_id=target.id
  x.enabled=False
 db.commit();return ok({"id":target.id,"merged_ids":[x.id for x in rows if x.id!=target.id]})
@router.post("/persons/{person_id}/disable")
def disable_person(person_id:str,payload:PersonDisableRequest,db:Session=Depends(get_db),_=Depends(require_write)):
 p=db.get(PersonCluster,person_id)
 if p is None:fail(404,"PERSON_NOT_FOUND","匿名人物不存在")
 for appearance in db.scalars(select(PersonAppearance).where(PersonAppearance.cluster_id==p.id)):
  appearance.cluster_id=None
  if hasattr(appearance,"feature_ciphertext"):appearance.feature_ciphertext=None
 p.enabled=False;db.commit();return ok({"id":p.id,"enabled":False,"features_removed":True})
@router.get("/events")
def events(request:Request,_=Depends(require_session)):
 return sse_response(request.app.state.outbox,request=request,last_event_id=last_event_id_from_headers(request.headers))
@health_router.get("/health/live")
def live():return {"status":"ok","alive":True,"timestamp_ms":utc_now_ms()}
@health_router.get("/health/ready")
def ready(request:Request):
 from app.health import readiness
 result=readiness(database=request.app.state.engine,data_dir=request.app.state.config.data_dir,
  migrations_complete=request.app.state.migrations_ready,worker_configured=True,scheduler_configured=True)
 return JSONResponse(result,status_code=200 if result.get("ready",result.get("status")=="ready") else 503)
@health_router.get("/health/dependencies")
def dependencies(request:Request):
 from app.health import dependencies as check
 return check(database=request.app.state.engine,data_dir=request.app.state.config.data_dir)
