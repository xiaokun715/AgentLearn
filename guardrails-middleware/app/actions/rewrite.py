"""内容改写器：Redactor（脱敏，§13）与 Sanitizer（中和注入指令，§22）。

按 Finding 里保存的命中原文（metadata["raw"] / metadata["phrase"]）做**精确替换**，
而不是重跑正则 —— 避免二次扫描误伤，也支持对 dict/list 里的字符串递归脱敏。
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..core.finding import SecurityFinding

# category -> 遮罩模板
DEFAULT_REDACT_TEMPLATES: dict[str, str] = {
    "PHONE": "<PHONE_REDACTED>",
    "EMAIL": "<EMAIL_REDACTED>",
    "ID_CARD": "<ID_CARD_REDACTED>",
    "BANK_CARD": "<BANK_CARD_REDACTED>",
    "SECRET": "<SECRET_REDACTED>",
    "TOKEN": "<SECRET_REDACTED>",
}

# Tool Result / 外部内容里「中和注入指令」的遮罩（§22）
INJECTION_MARK = "[injected-instruction-removed]"


def _apply_leaves(value: Any, fn) -> tuple[Any, bool]:
    """对 dict/list 里的字符串叶子（**含键名**）执行 fn，返回 (新值, 是否有改动)。

    Detector 扫描的是 json.dumps(content) 全文本（键也参与匹配），因此改写也必须
    落到键上——否则敏感信息作为 dict 键时会原样放行（review 修复）。
    """
    if isinstance(value, str):
        new = fn(value)
        return new, new != value
    if isinstance(value, list):
        changed = False
        out = []
        for item in value:
            new_item, c = _apply_leaves(item, fn)
            out.append(new_item)
            changed = changed or c
        return out, changed
    if isinstance(value, dict):
        changed = False
        out = {}
        for k, v in value.items():
            new_key = fn(k) if isinstance(k, str) else k
            new_val, c = _apply_leaves(v, fn)
            out[new_key] = new_val
            changed = changed or c or (new_key != k)
        return out, changed
    return value, False


class Redactor:
    """把命中的敏感原文替换为遮罩模板（PHONE -> <PHONE_REDACTED>）。"""

    def __init__(self, templates: dict[str, str] | None = None) -> None:
        self.templates = {**DEFAULT_REDACT_TEMPLATES, **(templates or {})}

    def template_for(self, category: str) -> str:
        return self.templates.get(category, "<REDACTED>")

    def redact(self, content: Any, findings: list["SecurityFinding"]) -> tuple[Any, bool]:
        # 长原文先替换，避免「短 token 是长 token 子串」时先被改坏
        ordered = sorted(
            (f for f in findings if f.raw),
            key=lambda f: len(f.raw or ""),
            reverse=True,
        )
        if not ordered:
            return content, False

        def _mask(text: str) -> str:
            for f in ordered:
                mask = self.template_for(f.category)
                if f.raw in text:
                    text = text.replace(f.raw, mask)
            return text

        return _apply_leaves(content, _mask)


class Sanitizer:
    """中和 PROMPT_INJECTION：移除外部内容里的注入指令（SANITIZE，§22）。

    被移除的指令用占位符标出，剩下的可信内容仍可进入 Context Assembly。
    """

    def sanitize(self, content: Any, findings: list["SecurityFinding"]) -> tuple[Any, bool]:
        import re

        tokens: set[str] = set()
        for f in findings:
            if f.category != "PROMPT_INJECTION":
                continue
            raw = f.raw or f.metadata.get("phrase")
            if raw:
                tokens.add(raw)
        if not tokens:
            return content, False

        def _neutralize(text: str) -> str:
            # 忽略大小写替换：检测时做了 lower()，原文可能是大写开头（Ignore ...）
            for token in sorted(tokens, key=len, reverse=True):
                text = re.sub(re.escape(token), INJECTION_MARK, text, flags=re.IGNORECASE)
            return text

        return _apply_leaves(content, _neutralize)


__all__ = ["Redactor", "Sanitizer", "DEFAULT_REDACT_TEMPLATES", "INJECTION_MARK"]
