"""Deployment API（设计说明书 §14 / §18 / §30）。

  POST  /v1/deployments                发布（publish）
  POST  /v1/deployments/{id}/rollout   灰度（rollout，调整流量）
  POST  /v1/deployments/{id}/rollback  回滚（rollback，不删除版本）
  GET   /v1/deployments                列出所有环境绑定
  GET   /v1/deployments/{id}           查看单个（含 canary 进度）
"""
from __future__ import annotations

from fastapi import APIRouter, Request

from ..router.canary import canary_report
from .schemas import DeployRequest, RollbackRequest, RolloutRequest

router = APIRouter(prefix="/v1/deployments", tags=["deployments"])


@router.post("", status_code=201)
async def deploy(body: DeployRequest, request: Request):
    rt = request.app.state.runtime
    dep = await rt.publisher.publish(
        body.agent,
        body.environment,
        body.version,
        traffic_percent=body.traffic_percent,
        experiment=body.experiment,
        created_by=body.created_by,
        reason=body.reason,
    )
    return dep.to_dict()


@router.post("/{deployment_id}/rollout")
async def rollout(deployment_id: str, body: RolloutRequest, request: Request):
    rt = request.app.state.runtime
    dep = await rt.publisher.rollout(
        deployment_id,
        body.version,
        body.traffic_percent,
        created_by=body.created_by,
        reason=body.reason,
    )
    return dep.to_dict()


@router.post("/{deployment_id}/rollback")
async def rollback(deployment_id: str, body: RollbackRequest, request: Request):
    rt = request.app.state.runtime
    dep = await rt.rollback_service.rollback(
        deployment_id,
        target_version=body.version,
        created_by=body.created_by,
        reason=body.reason,
    )
    return dep.to_dict()


@router.get("")
async def list_deployments(request: Request):
    rt = request.app.state.runtime
    deps = await rt.repo.list_deployments()
    return [d.to_dict() for d in deps]


@router.get("/{deployment_id}")
async def get_deployment(deployment_id: str, request: Request):
    rt = request.app.state.runtime
    dep = await rt.repo.get_deployment_by_id(deployment_id)
    if dep is None:
        from ..domain.exceptions import NotFoundError

        raise NotFoundError(f"Deployment {deployment_id} 不存在")
    data = dep.to_dict()
    data["canary"] = canary_report(dep)
    return data
