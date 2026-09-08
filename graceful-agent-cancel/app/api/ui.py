"""静态演示控制台 —— 提供那个有红色 Stop 按钮的页面。"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse

router = APIRouter(tags=["ui"])

_STATIC = Path(__file__).resolve().parents[2] / "static"


@router.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(_STATIC / "index.html")


@router.get("/ui", include_in_schema=False)
async def ui() -> FileResponse:
    return FileResponse(_STATIC / "index.html")
