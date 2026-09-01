"""Webhook 请求头常量（设计说明书 §12, §23~§24）。"""
from __future__ import annotations

HEADER_EVENT_ID = "X-Event-ID"
HEADER_EVENT_TYPE = "X-Event-Type"
HEADER_WEBHOOK_ID = "X-Webhook-ID"
HEADER_WEBHOOK_TIMESTAMP = "X-Webhook-Timestamp"
HEADER_WEBHOOK_SIGNATURE = "X-Webhook-Signature"
