"""观测 API 包。运行入口见 memory_engine.main(``uvicorn memory_engine.main:app``)。"""
from __future__ import annotations

from .routes import router

__all__ = ["router"]
