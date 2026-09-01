"""semantic-cache —— LLM 语义缓存系统。

项目目标（设计说明书 §3）：让语义相似但字符串不同的请求命中缓存，
从而省掉一次 LLM 调用，同时通过 Threshold / Safety / TTL 控制错误命中风险。
"""
from .config import CacheConfig

__version__ = "0.1.0"

__all__ = ["CacheConfig", "__version__"]
