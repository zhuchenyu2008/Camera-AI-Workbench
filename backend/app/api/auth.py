"""Authentication and session routes."""
from fastapi import APIRouter,Depends,Request,Response
from sqlalchemy.orm import Session
from app.main import get_db
from app.schemas import LoginRequest,PasswordChangeRequest
from .common import fail,ok,require_session,require_write,audit
router=APIRouter()
@router.post("/session")
def login(payload:LoginRequest,request:Request,response:Response,db:Session=Depends(get_db)):
    origin=request.headers.get("Origin","").rstrip("/")
    if origin and origin not in request.app.state.config.allowed_origins:fail(403,"ORIGIN_DENIED","登录来源不受允许")
    token,dto=request.app.state.auth.login(db,payload.username,payload.password)
    response.set_cookie(request.app.state.auth.cookie_name,token,httponly=True,secure=request.app.state.config.secure_cookies,samesite="strict",max_age=request.app.state.config.session_ttl_seconds,path="/")
    audit(db,request,"session.login","admin","admin");db.commit();return {"data":dto}
@router.get("/session")
def get_session(request:Request,session=Depends(require_session)):
    if session is None:return {"data":{"authenticated":True,"username":"local"}}
    return {"data":request.app.state.auth.session_dto(session)}
@router.delete("/session")
def logout(request:Request,response:Response,db:Session=Depends(get_db),_=Depends(require_write)):
    request.app.state.auth.logout(db,request.state.auth_session);response.delete_cookie(request.app.state.auth.cookie_name,path="/");return ok({"logged_out":True})
@router.post("/session/password")
def password(payload:PasswordChangeRequest,request:Request,db:Session=Depends(get_db),_=Depends(require_write)):
    request.app.state.auth.change_password(db,request.state.auth_session,payload.current_password,payload.new_password)
    return ok({"changed":True})
