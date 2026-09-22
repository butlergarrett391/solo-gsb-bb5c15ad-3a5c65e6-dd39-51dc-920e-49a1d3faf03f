"""Pydantic request models."""
from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field, field_validator


def _valid_percentages(value: List[int]) -> List[int]:
    if not value or any(p < 0 or p > 100 for p in value):
        raise ValueError("percentages must be between 0 and 100")
    if value != sorted(value):
        raise ValueError("percentages must be strictly ascending")
    if len(set(value)) != len(value):
        raise ValueError("percentages must be unique")
    if value[-1] != 100:
        raise ValueError("the final rollout stage must be 100")
    return [int(p) for p in value]


class ServiceRegister(BaseModel):
    name: str = Field(min_length=1, max_length=128, examples=["checkout"])
    description: str = Field(default="", max_length=512)
    default_rollout: List[int] = Field(
        default_factory=lambda: [5, 10, 25, 50, 75, 100],
        description="Ascending canary percentages; the last stage must be 100.",
    )

    @field_validator("default_rollout")
    @classmethod
    def check_rollout(cls, v: List[int]) -> List[int]:
        return _valid_percentages(v)


class ReleaseCreate(BaseModel):
    version: str = Field(min_length=1, max_length=128, examples=["v2.1.0"])
    error_threshold: float = Field(
        default=0.1, ge=0, le=1, description="Auto-pause when error rate is strictly greater than this."
    )
    window_seconds: int = Field(default=300, ge=10, le=86400)
    min_requests: int = Field(default=10, ge=1, le=1_000_000,
                              description="Gate only evaluates after this many events in the window.")
    rollout: Optional[List[int]] = Field(
        default=None,
        description="Per-release ascending stages; defaults to the service's default_rollout.",
    )

    @field_validator("rollout")
    @classmethod
    def check_rollout(cls, v):
        return None if v is None else _valid_percentages(v)


class PromoteRequest(BaseModel):
    percentage: Optional[int] = Field(
        default=None, ge=0, le=100,
        description="Target percentage; must be greater than the current one. "
                    "Omit to advance to the next configured stage.",
    )


class TelemetryEvent(BaseModel):
    release_id: Optional[int] = Field(
        default=None,
        description="Target release. Required for the batch endpoint; ignored on the "
                    "per-release endpoint (its path id is used).",
    )
    success: bool
    latency_ms: Optional[float] = Field(default=None, ge=0)
    status_code: Optional[int] = Field(default=None, ge=100, lt=600)
    user_id: Optional[str] = Field(default=None, max_length=256)
    occurred_at: Optional[str] = Field(
        default=None,
        description="ISO-8601 UTC timestamp; server time is used when omitted.",
    )

    @field_validator("occurred_at")
    @classmethod
    def check_iso(cls, v):
        if v is None:
            return v
        from .logic import parse_iso
        return parse_iso(v)


class TelemetryBatch(BaseModel):
    events: List[TelemetryEvent] = Field(min_length=1, max_length=1000)
