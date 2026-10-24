"""Timeline, segment editing and frame evidence routes."""
from datetime import datetime
from fastapi import APIRouter,Depends,Request,Query,Header
from fastapi.responses import FileResponse
from sqlalchemy import and_,or_,select,update
from sqlalchemy.orm import Session,selectinload
from app.main import get_db
from app.models import ActivitySegment,ActivityInterval,ActivityCatalog,CameraGap,MinuteObservation,L2Window,Frame,Summary,UserEdit,utc_now_ms
from app.schemas import SegmentEditRequest,SegmentSplitRequest,SegmentMergeRequest,SegmentReprocessRequest,FrameFavoriteRequest
from .common import fail,ok,require_session,require_write,ms,iso,ensure_segment,segment_dto,frame_dto,publish,encode_cursor,decode_cursor,audit
router=APIRouter()
def _time(v):return ms(v)
def _obs(o):return {"id":o.id,"camera_id":o.camera_id,"start":iso(o.start_at_ms),"end":iso(o.end_at_ms),"source":o.source,
 "state":o.state,"observation":o.observation_json,"candidates":o.candidates_json,"evidence_frame_ids":o.evidence_frame_ids_json,
 "gaps":o.gaps_json,"coverage_ratio":o.coverage_ratio,"confidence":o.confidence,"version":o.version}
@router.get("/timeline")
def timeline(request:Request,camera_id:str|None=None,from_:datetime=Query(alias="from"),to:datetime=Query(),include_observations:bool=False,
 limit:int=Query(50,ge=1,le=200),cursor:str|None=None,db:Session=Depends(get_db),_=Depends(require_session)):
 start,end=_time(from_),_time(to)
 if start>=end or end-start>31*86400000:fail(422,"INVALID_TIME_RANGE","查询范围必须为正且不超过 31 天")
 type_order={"segment":0,"gap":1,"observation":2,"window":3}
 cursor_time=None;cursor_type=None;cursor_id=None
 if cursor:
  values=decode_cursor(cursor,3)
  try:
   cursor_time=int(values[0]);cursor_type=str(values[1]);cursor_id=str(values[2])
   if cursor_type not in type_order:raise ValueError
  except (ValueError,TypeError):fail(400,"INVALID_CURSOR","分页游标无效")
 def after_type(stmt,model,kind):
  if cursor_time is None:return stmt
  if type_order[kind]>type_order[cursor_type]:return stmt.where(model.start_at_ms>=cursor_time)
  if type_order[kind]<type_order[cursor_type]:return stmt.where(model.start_at_ms>cursor_time)
  return stmt.where(or_(model.start_at_ms>cursor_time,and_(model.start_at_ms==cursor_time,model.id>cursor_id)))
 sq=select(ActivitySegment).options(selectinload(ActivitySegment.intervals),selectinload(ActivitySegment.canonical_activity)).where(
  ActivitySegment.start_at_ms<end,ActivitySegment.end_at_ms>start)
 gq=select(CameraGap).where(CameraGap.start_at_ms<end,or_(CameraGap.end_at_ms.is_(None),CameraGap.end_at_ms>start))
 oq=select(MinuteObservation).where(MinuteObservation.start_at_ms<end,MinuteObservation.end_at_ms>start)
 wq=select(L2Window).where(L2Window.start_at_ms<end,L2Window.end_at_ms>start)
 if camera_id:
  sq=sq.where(ActivitySegment.camera_id==camera_id);gq=gq.where(CameraGap.camera_id==camera_id)
  oq=oq.where(MinuteObservation.camera_id==camera_id);wq=wq.where(L2Window.camera_id==camera_id)
 sq=after_type(sq,ActivitySegment,"segment").order_by(ActivitySegment.start_at_ms,ActivitySegment.id).limit(limit+1)
 gq=after_type(gq,CameraGap,"gap").order_by(CameraGap.start_at_ms,CameraGap.id).limit(limit+1)
 wq=after_type(wq,L2Window,"window").order_by(L2Window.start_at_ms,L2Window.id).limit(limit+1)
 segments=list(db.scalars(sq));gaps=list(db.scalars(gq));windows=list(db.scalars(wq))
 observations=[]
 if include_observations:
  oq=after_type(oq,MinuteObservation,"observation").order_by(MinuteObservation.start_at_ms,MinuteObservation.id).limit(limit+1)
  observations=list(db.scalars(oq))
 merged=[]
 merged.extend((x.start_at_ms,type_order["segment"],x.id,"segment",x) for x in segments)
 merged.extend((x.start_at_ms,type_order["gap"],x.id,"gap",x) for x in gaps)
 merged.extend((x.start_at_ms,type_order["observation"],x.id,"observation",x) for x in observations)
 merged.extend((x.start_at_ms,type_order["window"],x.id,"window",x) for x in windows)
 merged.sort(key=lambda item:(item[0],item[1],item[2]))
 has_more=len(merged)>limit;page=merged[:limit]
 page_segments=[item[4] for item in page if item[3]=="segment"]
 page_gaps=[item[4] for item in page if item[3]=="gap"]
 page_observations=[item[4] for item in page if item[3]=="observation"]
 page_windows=[item[4] for item in page if item[3]=="window"]
 next_cursor=encode_cursor(page[-1][0],page[-1][3],page[-1][2]) if has_more and page else None
 return ok({"segments":[segment_dto(x) for x in page_segments],"observations":[_obs(x) for x in page_observations],
  "camera_gaps":[{"id":x.id,"camera_id":x.camera_id,"start":iso(x.start_at_ms),"end":iso(x.end_at_ms),"reason":x.reason} for x in page_gaps],
  "window_states":[{"id":x.id,"camera_id":x.camera_id,"start":iso(x.start_at_ms),"end":iso(x.end_at_ms),"state":x.state,"version":x.version} for x in page_windows]},
  meta={"timezone":request.app.state.config.timezone,"next_cursor":next_cursor,"limit":limit,"has_more":has_more})
@router.get("/segments/{segment_id}")
def segment_detail(segment_id:str,db:Session=Depends(get_db),_=Depends(require_session)):
 s=ensure_segment(db,segment_id)
 evidence_ids=list(dict.fromkeys(fid for interval in s.intervals for fid in interval.anchor_frame_ids_json))
 frames=list(db.scalars(select(Frame).where(Frame.id.in_(evidence_ids)))) if evidence_ids else []
 edits=list(db.scalars(select(UserEdit).where(UserEdit.target_type=="segment",UserEdit.target_id==s.id).order_by(UserEdit.created_at_ms.desc())))
 return ok({**segment_dto(s),"evidence_frames":[frame_dto(f) for f in frames],"revision_history":[{"id":e.id,"operation":e.operation,
  "before":e.before_json,"after":e.after_json,"reason":e.reason,"created_at":iso(e.created_at_ms)} for e in edits]})
@router.patch("/segments/{segment_id}")
def edit_segment(segment_id:str,payload:SegmentEditRequest,request:Request,db:Session=Depends(get_db),_=Depends(require_write)):
 s=ensure_segment(db,segment_id)
 if s.version!=payload.expected_version:fail(409,"VERSION_CONFLICT","活动段版本冲突",{"current_version":s.version})
 before=segment_dto(s);data=payload.model_dump(exclude_unset=True,exclude={"expected_version","edit_reason"})
 if data.get("start") is not None:s.start_at_ms=_time(data["start"])
 if data.get("end") is not None:s.end_at_ms=_time(data["end"])
 if s.end_at_ms<=s.start_at_ms:fail(422,"INVALID_SEGMENT_RANGE","活动段时间范围无效")
 if "canonical_activity_id" in data:
  aid=data.pop("canonical_activity_id")
  if aid and db.get(ActivityCatalog,aid) is None:fail(422,"ACTIVITY_NOT_FOUND","标准活动不存在")
  s.canonical_activity_id=aid
 for field in ("display_description","category","locked"):
  if field in data:setattr(s,field,data[field])
 s.version+=1;s.state="final";s.updated_at_ms=utc_now_ms();after=segment_dto(s)
 db.add(UserEdit(target_type="segment",target_id=s.id,operation="edit",before_json=before,after_json=after,reason=payload.edit_reason))
 audit(db,request,"segment.update","segment",s.id,{"version":s.version,"reason":payload.edit_reason})
 db.execute(update(Summary).where(Summary.camera_id==s.camera_id,Summary.period_start_ms<s.end_at_ms,Summary.period_end_ms>s.start_at_ms).values(stale=True,state="stale",updated_at_ms=utc_now_ms()))
 db.commit();publish(request,"timeline.updated",{"camera_id":s.camera_id,"segment_id":s.id,"start":iso(s.start_at_ms),"end":iso(s.end_at_ms),"version":s.version},s.camera_id)
 return ok(after)
@router.post("/segments/{segment_id}/split",status_code=201)
def split_segment(segment_id:str,payload:SegmentSplitRequest,request:Request,db:Session=Depends(get_db),_=Depends(require_write)):
 s=ensure_segment(db,segment_id);point=_time(payload.split_at)
 if s.version!=payload.expected_version:fail(409,"VERSION_CONFLICT","活动段版本冲突")
 if s.locked:fail(409,"SEGMENT_LOCKED","锁定活动段不能拆分")
 if not s.start_at_ms<point<s.end_at_ms:fail(422,"INVALID_SPLIT_POINT","拆分点必须在活动段内部")
 old=segment_dto(s);right=ActivitySegment(camera_id=s.camera_id,l2_window_id=s.l2_window_id,start_at_ms=point,end_at_ms=s.end_at_ms,
  canonical_activity_id=s.canonical_activity_id,display_description=s.display_description,category=s.category,confidence=s.confidence,
  evidence_coverage=s.evidence_coverage,state="final",source_observation_ids_json=s.source_observation_ids_json)
 s.end_at_ms=point;s.version+=1
 for interval in list(s.intervals):
  if interval.start_at_ms>=point:
   db.add(ActivityInterval(segment=right,start_at_ms=interval.start_at_ms,end_at_ms=interval.end_at_ms,evidence_kind=interval.evidence_kind,
    confidence=interval.confidence,reason=interval.reason,anchor_frame_ids_json=interval.anchor_frame_ids_json));db.delete(interval)
  elif interval.end_at_ms>point:
   end=interval.end_at_ms;interval.end_at_ms=point
   db.add(ActivityInterval(segment=right,start_at_ms=point,end_at_ms=end,evidence_kind=interval.evidence_kind,confidence=interval.confidence,
    reason=interval.reason,anchor_frame_ids_json=interval.anchor_frame_ids_json))
 db.add(right);db.add(UserEdit(target_type="segment",target_id=s.id,operation="split",before_json=old,after_json={"end":iso(point),"right_id":right.id},reason=payload.reason));audit(db,request,"segment.split","segment",s.id,{"right_id":right.id,"reason":payload.reason})
 db.commit();return ok({"segments":[segment_dto(s),segment_dto(right)]},status=201)
@router.post("/segments/merge",status_code=201)
def merge_segments(payload:SegmentMergeRequest,request:Request,db:Session=Depends(get_db),_=Depends(require_write)):
 items=list(db.scalars(select(ActivitySegment).options(selectinload(ActivitySegment.intervals),selectinload(ActivitySegment.canonical_activity)).where(ActivitySegment.id.in_(payload.segment_ids))))
 if len(items)!=len(payload.segment_ids):fail(404,"SEGMENT_NOT_FOUND","部分活动段不存在")
 items.sort(key=lambda x:x.start_at_ms)
 if any(x.locked for x in items):fail(409,"SEGMENT_LOCKED","锁定活动段不能合并")
 for x in items:
  if payload.expected_versions.get(x.id,x.version)!=x.version:fail(409,"VERSION_CONFLICT","活动段版本冲突",{"segment_id":x.id})
 if len({x.camera_id for x in items})!=1 or len({x.canonical_activity_id for x in items})!=1:fail(422,"INCOMPATIBLE_SEGMENTS","活动类型或摄像头不一致")
 if any(b.start_at_ms<a.end_at_ms for a,b in zip(items,items[1:])):fail(422,"SEGMENT_OVERLAP","活动段重叠")
 first=items[0];merged=ActivitySegment(camera_id=first.camera_id,l2_window_id=first.l2_window_id,start_at_ms=first.start_at_ms,end_at_ms=items[-1].end_at_ms,
  canonical_activity_id=first.canonical_activity_id,display_description=first.display_description,category=first.category,state="final",
  source_observation_ids_json=list(dict.fromkeys(i for x in items for i in x.source_observation_ids_json)))
 db.add(merged);db.flush()
 for x in items:
  for interval in x.intervals:db.add(ActivityInterval(segment_id=merged.id,start_at_ms=interval.start_at_ms,end_at_ms=interval.end_at_ms,evidence_kind=interval.evidence_kind,
   confidence=interval.confidence,reason=interval.reason,anchor_frame_ids_json=interval.anchor_frame_ids_json))
  db.add(UserEdit(target_type="segment",target_id=x.id,operation="merged_into",after_json={"segment_id":merged.id},reason=payload.reason));audit(db,request,"segment.merge","segment",x.id,{"merged_into":merged.id,"reason":payload.reason});db.delete(x)
 db.commit();return ok(segment_dto(merged),status=201)
@router.post("/segments/{segment_id}/reprocess",status_code=202)
def reprocess_segment(segment_id:str,payload:SegmentReprocessRequest,request:Request,db:Session=Depends(get_db),_=Depends(require_write),idempotency_key:str|None=Header(default=None,alias="Idempotency-Key")):
 from app.models import AIJob
 s=ensure_segment(db,segment_id);start=_time(payload.from_) if payload.from_ else s.start_at_ms;end=_time(payload.to) if payload.to else s.end_at_ms
 if end<=start:fail(422,"INVALID_TIME_RANGE","重新分析时间范围无效")
 key=idempotency_key or f"segment-reprocess:{s.id}:{start}:{end}:{s.version}"
 job=db.scalar(select(AIJob).where(AIJob.idempotency_key==key))
 if job is None:
  job=AIJob(idempotency_key=key,layer="L2",camera_id=s.camera_id,period_start_ms=start,period_end_ms=end,priority=350,
   input_version=s.version,prompt_version="v1",schema_version="v1",input_frame_ids_json=[],input_ref_json={"segment_id":s.id,"reason":payload.reason})
  db.add(job);audit(db,request,"segment.reprocess","segment",s.id,{"job_id":job.id,"reason":payload.reason});db.commit()
 from .common import job_dto
 return ok(job_dto(job),status=202)
def _safe_frame_path(request:Request, relative_path:str|None, root_name:str):
 if not relative_path:fail(410,"EVIDENCE_EXPIRED","证据文件不存在")
 root=(request.app.state.config.data_dir/root_name).resolve();path=(request.app.state.config.data_dir/relative_path).resolve()
 if root not in path.parents or not path.is_file():fail(410,"EVIDENCE_EXPIRED","证据文件不存在")
 return path
@router.get("/frames/{frame_id}/content")
def frame_content(frame_id:str,request:Request,db:Session=Depends(get_db),_=Depends(require_session)):
 f=db.get(Frame,frame_id)
 if f is None:fail(404,"FRAME_NOT_FOUND","证据帧不存在")
 if f.state!="active":fail(410,"EVIDENCE_EXPIRED","证据已过期",{"frame_id":f.id})
 path=_safe_frame_path(request,f.relative_path,"frames")
 return FileResponse(path,media_type="image/jpeg",headers={"Cache-Control":"private, max-age=60"})
@router.get("/frames/{frame_id}/thumbnail")
def frame_thumbnail(frame_id:str,request:Request,db:Session=Depends(get_db),_=Depends(require_session)):
 f=db.get(Frame,frame_id)
 if f is None:fail(404,"FRAME_NOT_FOUND","证据帧不存在")
 if f.state!="active":fail(410,"EVIDENCE_EXPIRED","证据已过期",{"frame_id":f.id})
 path=_safe_frame_path(request,f.thumbnail_relative_path,"thumbs")
 return FileResponse(path,media_type="image/jpeg",headers={"Cache-Control":"private, max-age=60"})
@router.post("/frames/{frame_id}/favorite")
def favorite(frame_id:str,payload:FrameFavoriteRequest,request:Request,db:Session=Depends(get_db),_=Depends(require_write)):
 f=db.get(Frame,frame_id)
 if f is None:fail(404,"FRAME_NOT_FOUND","证据帧不存在")
 f.favorite=payload.favorite;audit(db,request,"frame.favorite","frame",f.id,{"favorite":f.favorite});db.commit();publish(request,"storage.updated",{"frame_id":f.id,"favorite":f.favorite},f.camera_id)
 return ok(frame_dto(f))
