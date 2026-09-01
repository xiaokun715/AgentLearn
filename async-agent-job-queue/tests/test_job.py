"""Job 领域模型 + 状态机（设计说明书 §5-7）。"""
from __future__ import annotations

import pytest

from app.domain.exceptions import CancellationRequested, RetryableError
from app.domain.job import Job, new_job_id
from app.domain.state_machine import InvalidTransitionError, JobStateMachine
from app.domain.status import JobStatus


def test_new_job_id_format():
    jid = new_job_id()
    assert jid.startswith("job_")
    assert len(jid) > 4


def test_job_create_defaults():
    job = Job.create(agent_name="research_agent", input={"query": "q"}, tenant_id="t1")
    assert job.status == JobStatus.QUEUED
    assert job.retry_count == 0
    assert job.max_retries == 3
    assert job.queued_at is not None
    assert job.started_at is None
    assert job.id.startswith("job_")


def test_job_to_public_shape():
    job = Job.create(agent_name="research_agent", input={"query": "q"})
    job.status = JobStatus.RUNNING
    job.current_step = "search"
    job.progress = 40
    pub = job.to_public()
    assert pub["job_id"] == job.id
    assert pub["status"] == "running"
    assert pub["current_step"] == "search"
    assert pub["progress"] == 40
    assert pub["retry_count"] == 0
    assert "result" in pub and "error" in pub


def test_job_roundtrip_from_dict():
    job = Job.create(agent_name="chaos_agent", input={"query": "q"}, priority=5)
    d = job.to_dict()
    again = Job.from_dict(d)
    assert again.id == job.id
    assert again.status == JobStatus.QUEUED
    assert again.priority == 5


# ---- 状态机 -------------------------------------------------------------


def test_state_machine_valid_transitions():
    cases = [
        (JobStatus.QUEUED, JobStatus.RUNNING),
        (JobStatus.QUEUED, JobStatus.CANCELLED),
        (JobStatus.RUNNING, JobStatus.SUCCESS),
        (JobStatus.RUNNING, JobStatus.FAILED),
        (JobStatus.RUNNING, JobStatus.RETRYING),
        (JobStatus.RUNNING, JobStatus.CANCELLED),
        (JobStatus.RETRYING, JobStatus.RUNNING),
        (JobStatus.RETRYING, JobStatus.QUEUED),
        (JobStatus.FAILED, JobStatus.DEAD),
        (JobStatus.DEAD, JobStatus.QUEUED),
    ]
    for frm, to in cases:
        assert JobStateMachine.can_transition(frm, to), f"{frm} -> {to} should be valid"


def test_state_machine_invalid_transitions_raise():
    bad = [
        (JobStatus.SUCCESS, JobStatus.RUNNING),
        (JobStatus.QUEUED, JobStatus.DEAD),
        (JobStatus.CANCELLED, JobStatus.RUNNING),
        (JobStatus.SUCCESS, JobStatus.QUEUED),
        (JobStatus.DEAD, JobStatus.SUCCESS),
        (JobStatus.CANCELLED, JobStatus.QUEUED),
    ]
    for frm, to in bad:
        assert not JobStateMachine.can_transition(frm, to)
        with pytest.raises(InvalidTransitionError):
            JobStateMachine.assert_can_transition(frm, to)


def test_reaper_recovery_transition_is_allowed():
    """Lease 过期 -> Reaper 把 RUNNING/RETRYING 重新入队（§30-31）。"""
    assert JobStateMachine.can_transition(JobStatus.RUNNING, JobStatus.QUEUED)
    assert JobStateMachine.can_transition(JobStatus.RETRYING, JobStatus.QUEUED)


def test_dlq_requeue_transition_is_allowed():
    """DEAD -> QUEUED：人工从 DLQ 重新入队（§28）。"""
    assert JobStateMachine.can_transition(JobStatus.DEAD, JobStatus.QUEUED)


def test_terminal_statuses():
    # 严格定义：无任何出边才算终态。
    # SUCCESS / CANCELLED 真正终止；DEAD 可人工重投、FAILED 会自动转 DEAD，故非严格终态。
    assert JobStateMachine.is_terminal(JobStatus.SUCCESS)
    assert JobStateMachine.is_terminal(JobStatus.CANCELLED)
    assert not JobStateMachine.is_terminal(JobStatus.DEAD)
    assert not JobStateMachine.is_terminal(JobStatus.FAILED)
    assert not JobStateMachine.is_terminal(JobStatus.RUNNING)


def test_exception_classification():
    # §24：Retryable 应重试，NonRetryable 不应重试
    from app.retry.policy import RetryPolicy

    policy = RetryPolicy()
    assert policy.should_retry(RetryableError("timeout")) is True
    from app.domain.exceptions import NonRetryableError

    assert policy.should_retry(NonRetryableError("invalid prompt")) is False
    assert policy.should_retry(ValueError("boom")) is False
    assert policy.should_retry(CancellationRequested()) is False
