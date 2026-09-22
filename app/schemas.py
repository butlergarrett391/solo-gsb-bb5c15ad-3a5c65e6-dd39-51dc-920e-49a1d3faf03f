"""Pydantic 请求模型。"""
from __future__ import annotations

from pydantic import BaseModel, Field


class ServiceCreate(BaseModel):
    name: str = Field(..., min_length=1, examples=["payments"])
    error_threshold_pct: float = Field(
        50.0, ge=0, le=100, description="滑动窗口错误率阈值（百分比），超过即触发动作"
    )
    window_seconds: int = Field(60, ge=1, description="滑动窗口大小（秒）")
    auto_rollback: bool = Field(False, description="超阈值时是否自动回滚；否则仅暂停")


class ReleaseCreate(BaseModel):
    version: str = Field(..., min_length=1, examples=["v2.0.0"])
    percentage: int = Field(10, ge=1, le=100, description="灰度百分比 1~99 灰度，100 直接全量")
    actor: str = Field("api", examples=["release-bot"])


class PromoteRequest(BaseModel):
    percentage: int = Field(..., ge=1, le=100, description="目标百分比；100 即全量转正")
    actor: str = Field("api")


class RollbackRequest(BaseModel):
    actor: str = Field("api")
    reason: str = Field("manual rollback")


class TelemetryRequest(BaseModel):
    user_id: str = Field(..., min_length=1)
    success: bool
    version: str | None = Field(
        None, description="可选，显式指定版本；缺省时记到当前 canary（无 canary 则 stable）"
    )
