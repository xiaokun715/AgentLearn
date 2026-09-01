"""Qwen Embedding Generator（设计说明书 §11）。

通过 DashScope 的 OpenAI 兼容接口调用 ``text-embedding-v4``。
需要环境变量 ``DASHSCOPE_API_KEY``。

替换点：只需把 embedding provider 从 ``mock`` 换成 ``qwen``，
Cache 层（SemanticCache / Storage / Policy）完全不用改。
"""
from __future__ import annotations

import os

import httpx

from .base import EmbeddingGenerator

_DEFAULT_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"


class QwenEmbeddingGenerator(EmbeddingGenerator):
    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str = _DEFAULT_BASE_URL,
        model: str = "text-embedding-v4",
        dim: int = 1024,
        timeout: float = 30.0,
    ):
        self._api_key = api_key or os.getenv("DASHSCOPE_API_KEY", "")
        if not self._api_key:
            raise ValueError("QwenEmbeddingGenerator 需要 DASHSCOPE_API_KEY 环境变量")
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.dim = dim
        self._timeout = timeout

    async def embed(self, text: str) -> list[float]:
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(
                f"{self.base_url}/embeddings",
                headers={"Authorization": f"Bearer {self._api_key}"},
                json={"model": self.model, "input": text, "encoding_format": "float"},
            )
            resp.raise_for_status()
            data = resp.json()
            return list(data["data"][0]["embedding"])

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(
                f"{self.base_url}/embeddings",
                headers={"Authorization": f"Bearer {self._api_key}"},
                json={"model": self.model, "input": texts, "encoding_format": "float"},
            )
            resp.raise_for_status()
            data = resp.json()
            data = sorted(data["data"], key=lambda d: d["index"])
            return [list(d["embedding"]) for d in data]
