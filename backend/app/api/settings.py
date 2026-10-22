"""AI provider/model bindings, activity catalog and settings routes."""
from __future__ import annotations
from urllib.parse import urlsplit
from fastapi import APIRouter,Depends,Request,Query,Header
from sqlalchemy import func,select
from sqlalchemy.orm import Session,selectinload
from app.main import get_db
from app.models import AIProvider,AIBinding,ActivityCatalog,ActivityAlias,ActivitySegment,AppSetting,utc_now_ms
from app.schemas import (ProviderCreateRequest,ProviderUpdateRequest,ProviderTestRequest,BindingsUpdateRequest,
 ActivityCatalogCreateRequest,ActivityCatalogUpdateRequest,RetentionSettingsUpdate,AnalysisSettingsUpdate)
from app.ai_gateway import OpenAICompatibleGateway, CapabilitySet, ProviderError, validate_provider_url
from .common import fail,ok,require_session,require_write,provider_dto,activity_dto,setting,save_setting,encrypt_app_secret,decrypt_app_secret,idempotency_replay,idempotency_store
router=APIRouter()
def policy(request,url,allow_local=False):
 config=request.app.state.config
 dev=config.environment in {"development","dev","test"}
 allowed=config.provider_allowed_hosts
 local=config.provider_local_endpoints if allow_local else ()
 if not dev and urlsplit(url).scheme.lower()!="https" and not allow_local:
  fail(422,"PROVIDER_HTTPS_REQUIRED","生产 Provider 必须使用 HTTPS")
 try:validate_provider_url(url,allowed_hosts=allowed,local_endpoints=local,development_mode=dev)
 except ProviderError as exc:fail(422,exc.code,exc.message)
def key_for(request,p):
 if not p.api_key_ciphertext:return None
 try:return decrypt_app_secret(request,p.api_key_ciphertext)
 except Exception:fail(503,"SECRET_DECRYPT_FAILED","Provider 密钥无法解密，请检查主密钥")
def aliases(db,item,values):
 normalized={" ".join(x.casefold().split()):x.strip() for x in values if x.strip()}
 if len(normalized)!=len([x for x in values if x.strip()]):fail(422,"ALIAS_CONFLICT","别名重复")
 for norm,value in normalized.items():
  conflict=db.scalar(select(ActivityAlias).where(ActivityAlias.normalized_alias==norm,ActivityAlias.catalog_id!=item.id))
  if conflict:fail(409,"ALIAS_CONFLICT","别名已被其他活动使用",{"alias":value})
 for old in list(item.aliases):db.delete(old)
 item.aliases=[ActivityAlias(alias=value,normalized_alias=norm) for norm,value in normalized.items()]
@router.get("/ai/providers")
def providers(db:Session=Depends(get_db),_=Depends(require_session)):
 return ok([provider_dto(x) for x in db.scalars(select(AIProvider).order_by(AIProvider.label))])
@router.post("/ai/providers",status_code=201)
def create_provider(payload:ProviderCreateRequest,request:Request,db:Session=Depends(get_db),_=Depends(require_write),idempotency_key:str|None=Header(default=None,alias="Idempotency-Key")):
 replay=idempotency_replay(request,db,"provider.create",idempotency_key,payload.model_dump())
 if replay is not None:return replay
 policy(request,payload.base_url,payload.allow_insecure_local)
 encrypted=encrypt_app_secret(request,payload.api_key) if payload.api_key else None
 item=AIProvider(label=payload.label,base_url=payload.base_url,api_key_ciphertext=encrypted,
  timeout_ms=payload.timeout_ms,enabled=payload.enabled,allow_insecure_local=payload.allow_insecure_local)
 db.add(item);db.flush();body=ok(provider_dto(item),status=201)
 replay=idempotency_store(request,db,body,201)
 if replay is not None:return replay
 db.commit();return body
@router.patch("/ai/providers/{provider_id}")
def update_provider(provider_id:str,payload:ProviderUpdateRequest,request:Request,db:Session=Depends(get_db),_=Depends(require_write)):
 p=db.get(AIProvider,provider_id)
 if p is None:fail(404,"PROVIDER_NOT_FOUND","Provider 不存在")
 data=payload.model_dump(exclude_unset=True,exclude={"api_key","clear_api_key","allow_insecure_local"})
 approved_local=p.allow_insecure_local if payload.allow_insecure_local is None else payload.allow_insecure_local
 target_url=data.get("base_url",p.base_url)
 policy(request,target_url,approved_local)
 for name,value in data.items():setattr(p,name,value)
 p.allow_insecure_local=approved_local
 if payload.api_key is not None:p.api_key_ciphertext=encrypt_app_secret(request,payload.api_key)
 elif payload.clear_api_key:p.api_key_ciphertext=None
 db.commit();return ok(provider_dto(p))
@router.delete("/ai/providers/{provider_id}")
def delete_provider(provider_id:str,db:Session=Depends(get_db),_=Depends(require_write)):
 p=db.get(AIProvider,provider_id)
 if p is None:fail(404,"PROVIDER_NOT_FOUND","Provider 不存在")
 if p.bindings:fail(409,"PROVIDER_IN_USE","Provider 仍绑定分析层")
 db.delete(p);db.commit();return ok({"deleted":True})
@router.post("/ai/providers/{provider_id}/test")
async def test_provider(provider_id:str,payload:ProviderTestRequest,request:Request,db:Session=Depends(get_db),_=Depends(require_write)):
 p=db.get(AIProvider,provider_id)
 if p is None:fail(404,"PROVIDER_NOT_FOUND","Provider 不存在")
 binding=db.scalar(select(AIBinding).where(AIBinding.provider_id==p.id))
 if binding is None:fail(422,"MODEL_REQUIRED","请先配置模型绑定")
 policy(request,p.base_url,p.allow_insecure_local)
 config=request.app.state.config
 gateway=OpenAICompatibleGateway({"base_url":p.base_url,"api_key":key_for(request,p),"model_name":binding.model_name,
  "timeout_ms":payload.timeout_ms or p.timeout_ms},allowed_hosts=config.provider_allowed_hosts,
  local_endpoints=config.provider_local_endpoints if p.allow_insecure_local else (),
  development_mode=config.environment in {"development","dev","test"},max_retries=0)
 try:
  result=await gateway.probe(CapabilitySet(
   image_support=payload.test_vision,
   tools_support=payload.test_tools,
   json_mode_support=True,
   usage_support=payload.test_usage,
  ))
  caps={"text":result.text_support,
        "image":result.capabilities.image_support,"tools":result.capabilities.tools_support,
        "json_mode":result.capabilities.json_mode_support,"usage":result.capabilities.usage_support,
        "model":binding.model_name,"last_test_at_ms":utc_now_ms(),"latency_ms":result.latency_ms,
        "last_test_error":result.code}
  p.capability_json=caps;db.commit()
  return ok({"ok":result.ok,"provider_id":p.id,"model":binding.model_name,"capabilities":caps,
             "error":({"code":result.code,"message":result.message} if not result.ok else None)})
 except ProviderError as exc:
  caps={**(p.capability_json or {}),"last_test_at_ms":utc_now_ms(),"last_test_error":exc.code}
  p.capability_json=caps;db.commit()
  return ok({"ok":False,"provider_id":p.id,"model":binding.model_name,"capabilities":caps,
             "error":{"code":exc.code,"message":exc.message}})
 finally:await gateway.aclose()
@router.get("/ai/bindings")
def bindings(db:Session=Depends(get_db),_=Depends(require_session)):
 rows=list(db.scalars(select(AIBinding).options(selectinload(AIBinding.provider)).order_by(AIBinding.layer)))
 return ok([{"layer":x.layer.lower(),"provider_id":x.provider_id,"model_name":x.model_name,"fallback_provider_id":x.fallback_provider_id,
  "temperature":x.temperature,"max_output_tokens":x.max_output_tokens,"concurrency":x.concurrency,"timeout_ms":x.provider.timeout_ms} for x in rows])
@router.put("/ai/bindings")
def save_bindings(payload:BindingsUpdateRequest,db:Session=Depends(get_db),_=Depends(require_write)):
 existing={x.layer.lower():x for x in db.scalars(select(AIBinding))}
 for item in payload.bindings:
  if db.get(AIProvider,item.provider_id) is None:fail(422,"PROVIDER_NOT_FOUND","Provider 不存在")
  if item.fallback_provider_id and db.get(AIProvider,item.fallback_provider_id) is None:fail(422,"PROVIDER_NOT_FOUND","备用 Provider 不存在")
  row=existing.get(item.layer.lower()) or AIBinding(layer=item.layer.upper(),provider_id=item.provider_id,model_name=item.model_name)
  row.layer=item.layer.upper();row.provider_id=item.provider_id;row.model_name=item.model_name;row.fallback_provider_id=item.fallback_provider_id
  row.temperature=item.temperature;row.max_output_tokens=item.max_output_tokens;row.concurrency=item.concurrency;db.add(row)
 db.commit();return bindings(db)
@router.get("/activity-catalog")
def catalog(db:Session=Depends(get_db),_=Depends(require_session)):
 rows=list(db.scalars(select(ActivityCatalog).options(selectinload(ActivityCatalog.aliases)).order_by(ActivityCatalog.name)))
 return ok([activity_dto(x,db) for x in rows])
@router.post("/activity-catalog",status_code=201)
def create_activity(payload:ActivityCatalogCreateRequest,db:Session=Depends(get_db),_=Depends(require_write)):
 if db.scalar(select(ActivityCatalog.id).where(func.lower(ActivityCatalog.name)==payload.name.casefold())):fail(409,"ACTIVITY_EXISTS","活动已存在")
 x=ActivityCatalog(name=payload.name,category=payload.category,enabled=payload.enabled,description=payload.description);db.add(x);db.flush();aliases(db,x,payload.aliases);db.commit()
 return ok(activity_dto(x,db),status=201)
@router.patch("/activity-catalog/{activity_id}")
def update_activity(activity_id:str,payload:ActivityCatalogUpdateRequest,db:Session=Depends(get_db),_=Depends(require_write)):
 x=db.get(ActivityCatalog,activity_id)
 if x is None:fail(404,"ACTIVITY_NOT_FOUND","标准活动不存在")
 data=payload.model_dump(exclude_unset=True);alias_values=data.pop("aliases",None)
 for field,value in data.items():setattr(x,field,value)
 if alias_values is not None:aliases(db,x,alias_values)
 db.commit();return ok(activity_dto(x,db))
@router.delete("/activity-catalog/{activity_id}")
def delete_activity(activity_id:str,db:Session=Depends(get_db),_=Depends(require_write)):
 x=db.get(ActivityCatalog,activity_id)
 if x is None:fail(404,"ACTIVITY_NOT_FOUND","标准活动不存在")
 count=db.scalar(select(func.count(ActivitySegment.id)).where(ActivitySegment.canonical_activity_id==x.id)) or 0
 if count:x.enabled=False;result={"deleted":False,"disabled":True,"references":count}
 else:db.delete(x);result={"deleted":True}
 db.commit();return ok(result)
@router.get("/settings/retention")
def get_retention(db:Session=Depends(get_db),_=Depends(require_session)):
 default={"ordinary_frame_minutes":60,"event_frame_minutes":360,"favorite_keeps_forever":True,"keep_timeline_after_expiry":True,"low_disk_watermark_bytes":None,"revision":1}
 current=setting(db,"retention",default)
 return ok({**default,**current,"revision":int(current.get("revision",1))})

@router.put("/settings/retention")
def put_retention(payload:RetentionSettingsUpdate,db:Session=Depends(get_db),_=Depends(require_write)):
 default={"ordinary_frame_minutes":60,"event_frame_minutes":360,"favorite_keeps_forever":True,"keep_timeline_after_expiry":True,"low_disk_watermark_bytes":None,"revision":1}
 current={**default,**setting(db,"retention",default)}
 expected=payload.expected_version
 revision=int(current.get("revision",1))
 if expected is not None and expected!=revision:fail(409,"VERSION_CONFLICT","留存配置版本冲突",{"current_version":revision})
 data=payload.model_dump(exclude_unset=True,exclude={"expected_version"})
 current.update(data);current["revision"]=revision+1
 save_setting(db,"retention",current,"帧留存策略");db.commit();return ok(current)

@router.get("/settings/analysis")
def get_analysis(db:Session=Depends(get_db),_=Depends(require_session)):
 default={"sampling_interval_ms":1000,"stable_sample_seconds":10,"minute_window_seconds":60,"l2_window_minutes":5,
  "change_threshold":0.2,"max_inference_gap_minutes":5,"strict_mode":False,"revision":1}
 current=setting(db,"analysis",default)
 return ok({**default,**current,"revision":int(current.get("revision",1))})

@router.put("/settings/analysis")
def put_analysis(payload:AnalysisSettingsUpdate,db:Session=Depends(get_db),_=Depends(require_write)):
 default={"sampling_interval_ms":1000,"stable_sample_seconds":10,"minute_window_seconds":60,"l2_window_minutes":5,
  "change_threshold":0.2,"max_inference_gap_minutes":5,"strict_mode":False,"revision":1}
 current={**default,**setting(db,"analysis",default)}
 expected=payload.expected_version
 revision=int(current.get("revision",1))
 if expected is not None and expected!=revision:fail(409,"VERSION_CONFLICT","分析配置版本冲突",{"current_version":revision})
 data=payload.model_dump(exclude_unset=True,exclude={"expected_version"})
 current.update(data);current["revision"]=revision+1
 save_setting(db,"analysis",current,"分析策略");db.commit();return ok(current)
