"""Embedding Generator 接口（设计说明书 §10 ~ §11）。

Semantic Cache 的核心假设：文本 -> 向量 -> 向量相似度 -> 语义相似度。
开发阶段用 MockEmbeddingGenerator，之后替换为 Qwen/BGE/OpenAI，
Cache 层通过该接口隔离，不受影响。
"""
from __future__ import annotations

from abc import ABC, abstractmethod


class EmbeddingGenerator(ABC):
    dim: int

    @abstractmethod
    async def embed(self, text: str) -> list[float]:
        """把一段文本变成 ``self.dim`` 维的 L2 归一化向量。"""
        raise NotImplementedError

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return [await self.embed(t) for t in texts]
