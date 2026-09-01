"""实验 8：伪造 Webhook Body / Timestamp -> HMAC Signature + Replay Protection。

覆盖设计说明书 §13~§15：
- 签名 = HMAC(secret, f"{timestamp}.{body}")，前缀 v1=
- 时间窗检查（§14 Replay Attack）
- Constant-Time Comparison（§15）
"""
from __future__ import annotations

import time

import pytest

from app.security.signature import (
    SignatureError,
    generate_signature,
    verify_signature,
)
from app.webhook.signer import Signer

SECRET = "whsec_test_123"
BODY = b'{"id": "evt_001", "type": "agent.job.completed"}'


def test_valid_signature_passes():
    ts = int(time.time())
    sig = generate_signature(SECRET, ts, BODY)
    assert sig.startswith("v1=")
    verify_signature(SECRET, ts, BODY, sig)  # 不应抛异常


def test_tampered_body_rejected():
    """§13：伪造 body 会导致 HMAC 不匹配。"""
    ts = int(time.time())
    sig = generate_signature(SECRET, ts, BODY)
    forged_body = BODY.replace(b"completed", b"failed")
    with pytest.raises(SignatureError, match="不匹配"):
        verify_signature(SECRET, ts, forged_body, sig)


def test_wrong_secret_rejected():
    ts = int(time.time())
    sig = generate_signature(SECRET, ts, BODY)
    with pytest.raises(SignatureError, match="不匹配"):
        verify_signature("whsec_wrong", ts, BODY, sig)


def test_replay_attack_rejected():
    """§14：攻击者截获请求几小时后重放，时间窗过期 -> 拒绝。"""
    old_ts = int(time.time()) - 600  # 10 分钟前
    sig = generate_signature(SECRET, old_ts, BODY)
    with pytest.raises(SignatureError, match="时间戳"):
        verify_signature(SECRET, old_ts, BODY, sig, tolerance=300)


def test_old_but_within_window_accepted():
    ts = int(time.time()) - 60  # 1 分钟内
    sig = generate_signature(SECRET, ts, BODY)
    verify_signature(SECRET, ts, BODY, sig, tolerance=300)


def test_bad_signature_format_rejected():
    ts = int(time.time())
    with pytest.raises(SignatureError, match="格式"):
        verify_signature(SECRET, ts, BODY, "md5=deadbeef")


def test_signer_builds_standard_headers():
    """§12：请求头必须包含 Event-ID / Webhook-ID / Timestamp / Signature。"""
    signer = Signer()
    headers = signer.build_headers(
        secret=SECRET,
        event_id="evt_001",
        event_type="agent.job.completed",
        delivery_id="del_001",
        body=BODY,
    )
    assert headers["X-Event-ID"] == "evt_001"
    assert headers["X-Event-Type"] == "agent.job.completed"
    assert headers["X-Webhook-ID"] == "del_001"
    assert headers["X-Webhook-Timestamp"].isdigit()
    assert headers["X-Webhook-Signature"].startswith("v1=")


def test_signer_verify_request_roundtrip():
    signer = Signer(tolerance=300)
    ts = str(int(time.time()))
    sig = generate_signature(SECRET, int(ts), BODY)
    signer.verify_request(secret=SECRET, timestamp=ts, signature=sig, body=BODY)


def test_tampered_timestamp_rejected():
    """§14：即使签名是用合法时间戳算的，重放时换了时间戳也会失配。"""
    ts = int(time.time())
    sig = generate_signature(SECRET, ts, BODY)
    with pytest.raises(SignatureError, match="不匹配"):
        verify_signature(SECRET, ts + 1, BODY, sig)
