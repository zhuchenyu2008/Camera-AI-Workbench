"""API router assembly."""
from fastapi import APIRouter
from .auth import router as auth_router
from .cameras import router as camera_router
from .timeline import router as timeline_router
from .settings import router as settings_router
from .misc import router as misc_router,health_router,legacy_router
router=APIRouter()
for subrouter in (auth_router,camera_router,timeline_router,settings_router,misc_router):
    router.include_router(subrouter)
__all__=["router","health_router","legacy_router"]
