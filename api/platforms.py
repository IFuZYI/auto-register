from fastapi import APIRouter

from core.registry import list_platforms

router = APIRouter(prefix="/platforms", tags=["platforms"])


@router.get("")
def get_platforms():
    return list_platforms()
