"""Object Storage —— MinIO 的替身（说明书 §39 / §40）。

Tool 的返回结果**不能全部直接塞给 Agent**：一个 100MB 的日志不可能进 LLM Context（§37）。
所以 Result Processor 在结果超过 ``max_inline_size`` 时，把完整结果落到对象存储，
只把 ``artifact_id`` + ``preview`` 交回 Agent，Agent 需要细节时再调 ``get_artifact``。

本模块把 MinIO 的语义压到最小可行集：put / get / head / delete / presign。
Demo 用本地目录实现，接口与 ``minio`` SDK 对齐，换真机只需替换 :class:`ObjectStore` 的实现。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from typing import Optional

_SAFE = re.compile(r"[^A-Za-z0-9._-]")


@dataclass
class ObjectStat:
    """对象元信息（对应 ``minio.S3Error``/``stat_object`` 的常用字段）。"""

    artifact_id: str
    size: int
    content_type: str
    etag: str


class ObjectStore:
    """极简对象存储接口。"""

    def put(self, artifact_id: str, data: bytes, *, content_type: str = "application/json") -> ObjectStat:
        raise NotImplementedError

    def get(self, artifact_id: str) -> bytes:
        raise NotImplementedError

    def stat(self, artifact_id: str) -> ObjectStat:
        raise NotImplementedError

    def delete(self, artifact_id: str) -> None:
        raise NotImplementedError

    def exists(self, artifact_id: str) -> bool:
        raise NotImplementedError


class LocalObjectStore(ObjectStore):
    """本地目录实现。

    ``root`` 下每个 artifact 是一个文件，另有一个 ``.meta.json`` 记 content_type。
    路径做了白名单清洗 —— 即使 ``artifact_id`` 被恶意构造（``../../etc/passwd``），
    也不可能写出 ``root`` 之外（与 §24 参数注入防护同一思路：**确定性兜底**，
    不依赖上游是否已经校验过）。
    """

    def __init__(self, root: str = "artifacts") -> None:
        self.root = os.path.abspath(root)
        os.makedirs(self.root, exist_ok=True)

    # ------------------------------------------------------------------
    def _path(self, artifact_id: str) -> str:
        safe = _SAFE.sub("_", artifact_id)
        if not safe:
            raise ValueError("artifact_id 不能为空")
        # 双保险：拼完之后再确认仍在 root 之内
        path = os.path.abspath(os.path.join(self.root, safe))
        if os.path.commonpath([self.root, path]) != self.root:
            raise ValueError(f"artifact_id 逃逸出 root: {artifact_id!r}")
        return path

    def _meta_path(self, artifact_id: str) -> str:
        return self._path(artifact_id) + ".meta.json"

    # ------------------------------------------------------------------
    def put(self, artifact_id: str, data: bytes, *, content_type: str = "application/json") -> ObjectStat:
        path = self._path(artifact_id)
        with open(path, "wb") as fh:
            fh.write(data)
        with open(self._meta_path(artifact_id), "w", encoding="utf-8") as fh:
            json.dump({"content_type": content_type}, fh)
        return ObjectStat(
            artifact_id=artifact_id,
            size=len(data),
            content_type=content_type,
            etag=hashlib.md5(data).hexdigest(),
        )

    def get(self, artifact_id: str) -> bytes:
        with open(self._path(artifact_id), "rb") as fh:
            return fh.read()

    def stat(self, artifact_id: str) -> ObjectStat:
        path = self._path(artifact_id)
        size = os.path.getsize(path)
        content_type = "application/octet-stream"
        meta_path = self._meta_path(artifact_id)
        if os.path.exists(meta_path):
            with open(meta_path, encoding="utf-8") as fh:
                content_type = json.load(fh).get("content_type", content_type)
        with open(path, "rb") as fh:
            digest = hashlib.md5(fh.read()).hexdigest()
        return ObjectStat(
            artifact_id=artifact_id, size=size, content_type=content_type, etag=digest
        )

    def delete(self, artifact_id: str) -> None:
        for path in (self._path(artifact_id), self._meta_path(artifact_id)):
            if os.path.exists(path):
                os.remove(path)

    def exists(self, artifact_id: str) -> bool:
        return os.path.exists(self._path(artifact_id))


class InMemoryObjectStore(ObjectStore):
    """纯内存实现 —— 演示/推演不想在磁盘留垃圾时用。"""

    def __init__(self) -> None:
        self._objects: dict[str, tuple[bytes, str]] = {}

    def put(self, artifact_id: str, data: bytes, *, content_type: str = "application/json") -> ObjectStat:
        self._objects[artifact_id] = (data, content_type)
        return ObjectStat(
            artifact_id=artifact_id,
            size=len(data),
            content_type=content_type,
            etag=hashlib.md5(data).hexdigest(),
        )

    def get(self, artifact_id: str) -> bytes:
        if artifact_id not in self._objects:
            raise KeyError(f"artifact 不存在: {artifact_id}")
        return self._objects[artifact_id][0]

    def stat(self, artifact_id: str) -> ObjectStat:
        data, content_type = self._objects[artifact_id]
        return ObjectStat(
            artifact_id=artifact_id,
            size=len(data),
            content_type=content_type,
            etag=hashlib.md5(data).hexdigest(),
        )

    def delete(self, artifact_id: str) -> None:
        self._objects.pop(artifact_id, None)

    def exists(self, artifact_id: str) -> bool:
        return artifact_id in self._objects
