"""Prompt Normalizer（设计说明书 §6 ~ §9）。

Normalizer 解决的是文本形式差异（空格 / 大小写 / 全半角 / 格式 / 标点），
语义差异由 Embedding 解决（§7）。二者不是一回事。

对 Chat Request 而言，Cache Key 不能只取 user.content（§8）：
  - System Prompt A + 问题  与  System Prompt B + 问题  绝不能互相命中；
  - 因此 canonical request 必须包含 model / temperature / 完整 messages，
    再对它做 SHA256 得到精确指纹（§9）。
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from typing import Any

from ..core.entry import ChatRequest, Message

_WS_RE = re.compile(r"\s+")


@dataclass(slots=True)
class NormalizedRequest:
    """Normalizer 的产物（§6.1 Canonical Prompt）。"""

    user_text: str  # 规范化后的用户问题 —— 用于 Embedding
    request_text: str  # 规范化后的完整请求（system + user）—— 用于展示/日志
    canonical: dict[str, Any]  # 规范化请求 —— 用于精确指纹
    fingerprint: str  # 精确缓存 key（§9）
    system_fingerprint: str  # system prompt 指纹 —— 语义命中的 Safety 二次校验


class PromptNormalizer:
    """把 Raw Prompt 变成 Canonical Prompt。

    归一化规则（§6.1 的扩展）：
      1. NFKC 统一全半角（全角字母/数字/标点转半角，如 ？→?）
      2. 去除首尾空白
      3. 折叠连续空白（空格 / 换行 / tab 视为一个空格）
      4. 英文大小写统一（TCP / tcp 视为相同）
    """

    def normalize_text(self, text: str) -> str:
        text = unicodedata.normalize("NFKC", text)
        text = text.strip()
        text = _WS_RE.sub(" ", text)
        return text.lower()

    def normalize_request(self, request: ChatRequest) -> NormalizedRequest:
        """对整个请求做归一化，并计算精确指纹与 system 指纹。"""
        messages = [Message(role=m.role, content=self.normalize_text(m.content)) for m in request.messages]

        user_text = "\n".join(m.content for m in messages if m.role != "system")
        system_text = "\n".join(m.content for m in messages if m.role == "system")
        request_text = "\n".join(f"{m.role}: {m.content}" for m in messages)

        # 参与精确指纹的字段（§8）：model / temperature / messages / 版本 / Agent scope
        canonical: dict[str, Any] = {
            "model": request.model,
            "temperature": request.temperature,
            "messages": [m.to_dict() for m in messages],
            "knowledge_version": request.knowledge_version,
            "agent_type": request.agent_type,
            "task_type": request.task_type,
            "context_version": request.context_version,
            "time_sensitive": request.time_sensitive,
        }

        return NormalizedRequest(
            user_text=user_text,
            request_text=request_text,
            canonical=canonical,
            fingerprint=self.fingerprint(canonical),
            system_fingerprint=self._hash_text(system_text),
        )

    @staticmethod
    def fingerprint(canonical: dict[str, Any]) -> str:
        """精确缓存 key（§9）：canonical request 的 SHA256。

        ``sort_keys=True`` 保证字段顺序不影响指纹；
        ``ensure_ascii=False`` 保证中文按 UTF-8 计算。
        """
        canonical_json = json.dumps(canonical, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()

    @staticmethod
    def _hash_text(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()
