"""Gray-release (canary) gate API.

Endpoints
---------
POST   /services/{name}/releases                       create/publish a release
GET    /services/{name}/releases                       list releases
GET    /services/{name}/route?user_id=...              resolve which version serves a user
POST   /services/{name}/releases/{id}/promote          advance rollout percentage
POST   /services/{name}/releases/{id}/pause            manual pause
POST   /services/{name}/releases/{id}/resume           resume a paused release
POST   /services/{name}/releases/{id}/rollback         canary -> current stable
POST   /services/{name}/rollback                       service rollback to previous stable
POST   /services/{name}/releases/{id}/telemetry        report one request result
POST   /services/{name}/telemetry                      batch report request results
GET    /services/{name}/status                         gate status + window error rate
GET    /services/{name}/audit                          audit trail
"""
from __future__ import annotations

import json
from typing import Optional

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from sqlite3 import Connection

from .database import get_db, init_db
from .logic import (
    get_progressing_or_paused,
    get_release,
    get_service,
    get_stable_release,
    now_iso,
    previous_complete,
    route_request,
    evaluate_gate,
    window_stats,
    write_audit,
)
from .models import PromoteRequest, ReleaseCreate, ServiceRegister, TelemetryBatch, TelemetryEvent

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="Gray Release Gate API",
    version="1.0.0",
    lifespan=lifespan,
    description="Register service versions, roll out by stable-hash cohorts, and "
                "auto-pause / roll back when sliding-window error rates breach a threshold.",
)


@app.get("/health")
def health():
    return {"status": "ok", "time": now_iso()}


@app.get("/")
def root():
    return {"service": "gray-release-gate", "docs": "/docs", "health": "/health"}


# --------------------------------------------------------------------------- #
# Serialization helpers
# --------------------------------------------------------------------------- #
def service_dict(row) -> dict:
    return {
        "name": row["name"],
        "description": row["description"],
        "default_rollout": json.loads(row["default_rollout"]),
        "created_at": row["created_at"],
    }


def release_dict(row) -> dict:
    return {
        "id": row["id"],
        "service_name": row["service_name"],
        "version": row["version"],
        "percentage": row["percentage"],
        "stages": json.loads(row["stages"]),
        "stage_index": row["stage_index"],
        "status": row["status"],
        "error_threshold": row["error_threshold"],
        "window_seconds": row["window_seconds"],
        "min_requests": row["min_requests"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def require_service(db: Connection, service_name: str):
    svc = get_service(db, service_name)
    if svc is None:
        raise HTTPException(status_code=404, detail=f"service {service_name!r} not found")
    return svc


# --------------------------------------------------------------------------- #
# Service registration
# --------------------------------------------------------------------------- #
@app.post("/services", status_code=201)
def register_service(payload: ServiceRegister, db: Connection = Depends(get_db)):
    if get_service(db, payload.name) is not None:
        raise HTTPException(status_code=409, detail=f"service {payload.name!r} already exists")
    db.execute(
        "INSERT INTO services (name, description, default_rollout, created_at) VALUES (?, ?, ?, ?)",
        (payload.name, payload.description, json.dumps(payload.default_rollout), now_iso()),
    )
    write_audit(db, payload.name, "service_registered",
                {"description": payload.description, "default_rollout": payload.default_rollout})
    row = get_service(db, payload.name)
    return service_dict(row)


@app.get("/services")
def list_services(db: Connection = Depends(get_db)):
    rows = db.execute("SELECT * FROM services ORDER BY name").fetchall()
    return {"services": [service_dict(r) for r in rows]}


# --------------------------------------------------------------------------- #
# Releases (publish / list / route)
# --------------------------------------------------------------------------- #
@app.post("/services/{service_name}/releases", status_code=201)
def create_release(service_name: str, payload: ReleaseCreate, db: Connection = Depends(get_db)):
    svc = require_service(db, service_name)
    stages = payload.rollout if payload.rollout is not None else json.loads(svc["default_rollout"])

    duplicate = db.execute(
        "SELECT 1 FROM releases WHERE service_name = ? AND version = ?",
        (service_name, payload.version),
    ).fetchone()
    if duplicate is not None:
        raise HTTPException(status_code=409,
                            detail=f"version {payload.version!r} already exists for service {service_name!r}")

    active = get_progressing_or_paused(db, service_name)
    if active is not None:
        raise HTTPException(
            status_code=409,
            detail=f"release {active['version']!r} is still {active['status']} at "
                   f"{active['percentage']}%; promote it to 100%, pause or roll it back first",
        )

    ts = now_iso()
    first_release = get_stable_release(db, service_name) is None
    if first_release:
        # The first release is the known-good baseline and serves 100% immediately.
        db.execute(
            "INSERT INTO releases (service_name, version, percentage, stages, stage_index, "
            "error_threshold, window_seconds, min_requests, status, created_at, updated_at) "
            "VALUES (?, ?, 100, '[100]', 0, ?, ?, ?, 'complete', ?, ?)",
            (service_name, payload.version, payload.error_threshold, payload.window_seconds,
             payload.min_requests, ts, ts),
        )
        action, percentage = "release_baseline", 100
    else:
        db.execute(
            "INSERT INTO releases (service_name, version, percentage, stages, stage_index, "
            "error_threshold, window_seconds, min_requests, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 0, ?, ?, ?, 'progressing', ?, ?)",
            (service_name, payload.version, stages[0], json.dumps(stages),
             payload.error_threshold, payload.window_seconds, payload.min_requests, ts, ts),
        )
        action, percentage = "release_created", stages[0]

    row = db.execute(
        "SELECT * FROM releases WHERE service_name = ? AND version = ?",
        (service_name, payload.version),
    ).fetchone()
    write_audit(db, service_name, action,
                {"version": payload.version, "percentage": percentage, "stages": stages,
                 "error_threshold": payload.error_threshold, "window_seconds": payload.window_seconds,
                 "min_requests": payload.min_requests},
                version=payload.version)
    return release_dict(row)


@app.get("/services/{service_name}/releases")
def list_releases(service_name: str, db: Connection = Depends(get_db)):
    require_service(db, service_name)
    rows = db.execute(
        "SELECT * FROM releases WHERE service_name = ? ORDER BY id DESC", (service_name,)
    ).fetchall()
    return {"releases": [release_dict(r) for r in rows]}


@app.get("/services/{service_name}/route")
def resolve_route(
    service_name: str,
    user_id: str = Query(..., min_length=1, description="Stable user identifier (id, account, hash)."),
    db: Connection = Depends(get_db),
):
    require_service(db, service_name)
    result = route_request(db, service_name, user_id)
    if result["cohort"] == "none":
        return JSONResponse(
            status_code=503,
            content={"detail": "no fully rolled-out version available for this user", **result},
        )
    return result


# --------------------------------------------------------------------------- #
# Rollout controls
# --------------------------------------------------------------------------- #
@app.post("/services/{service_name}/releases/{release_id}/promote")
def promote_release(service_name: str, release_id: int, payload: PromoteRequest,
                    db: Connection = Depends(get_db)):
    require_service(db, service_name)
    release = get_release(db, release_id)
    if release is None or release["service_name"] != service_name:
        raise HTTPException(status_code=404, detail="release not found")
    if release["status"] != "progressing":
        raise HTTPException(status_code=409,
                            detail=f"release is {release['status']}, only progressing releases can be promoted")

    stages = json.loads(release["stages"])

    if payload.percentage is not None:
        target = payload.percentage
        if target <= release["percentage"]:
            raise HTTPException(status_code=400,
                               detail=f"target percentage {target} must be greater than current "
                                      f"{release['percentage']}")
        stage_index = release["stage_index"]
        for i, p in enumerate(stages):
            if p >= target:
                stage_index = i
                break
    else:
        next_index = release["stage_index"] + 1
        if next_index >= len(stages):
            raise HTTPException(status_code=409, detail="release already at the final stage")
        stage_index = next_index
        target = stages[stage_index]

    new_status = "complete" if target >= 100 else "progressing"
    db.execute("UPDATE releases SET percentage = ?, stage_index = ?, status = ?, updated_at = ? WHERE id = ?",
               (target, stage_index, new_status, now_iso(), release_id))
    write_audit(db, service_name,
                "release_completed" if new_status == "complete" else "release_promoted",
                {"version": release["version"], "from_percentage": release["percentage"],
                 "to_percentage": target, "stage_index": stage_index},
                version=release["version"])
    return release_dict(get_release(db, release_id))


@app.post("/services/{service_name}/releases/{release_id}/pause")
def pause_release(service_name: str, release_id: int,
                  reason: str = Query(default="manual pause"), db: Connection = Depends(get_db)):
    require_service(db, service_name)
    release = get_release(db, release_id)
    if release is None or release["service_name"] != service_name:
        raise HTTPException(status_code=404, detail="release not found")
    if release["status"] != "progressing":
        raise HTTPException(status_code=409, detail=f"release is {release['status']}, cannot pause")
    db.execute("UPDATE releases SET status = 'paused', updated_at = ? WHERE id = ?",
               (now_iso(), release_id))
    write_audit(db, service_name, "manual_paused",
                {"version": release["version"], "percentage": release["percentage"], "reason": reason},
                version=release["version"])
    return release_dict(get_release(db, release_id))


@app.post("/services/{service_name}/releases/{release_id}/resume")
def resume_release(service_name: str, release_id: int, db: Connection = Depends(get_db)):
    require_service(db, service_name)
    release = get_release(db, release_id)
    if release is None or release["service_name"] != service_name:
        raise HTTPException(status_code=404, detail="release not found")
    if release["status"] != "paused":
        raise HTTPException(status_code=409, detail=f"release is {release['status']}, cannot resume")

    stats = window_stats(db, release)
    if stats["breached"]:
        raise HTTPException(
            status_code=409,
            detail={"message": "error rate still above threshold; roll back or fix before resuming",
                    "window": stats},
        )

    db.execute("UPDATE releases SET status = 'progressing', updated_at = ? WHERE id = ?",
               (now_iso(), release_id))
    write_audit(db, service_name, "release_resumed",
                {"version": release["version"], "percentage": release["percentage"], "window": stats},
                version=release["version"])
    return release_dict(get_release(db, release_id))


@app.post("/services/{service_name}/releases/{release_id}/rollback")
def rollback_canary(service_name: str, release_id: int,
                    reason: str = Query(default="manual rollback"), db: Connection = Depends(get_db)):
    """Roll a canary back: it is stopped and all traffic returns to stable."""
    require_service(db, service_name)
    release = get_release(db, release_id)
    if release is None or release["service_name"] != service_name:
        raise HTTPException(status_code=404, detail="release not found")
    if release["status"] not in ("progressing", "paused"):
        raise HTTPException(status_code=409,
                           detail=f"release is {release['status']}; only an active canary can be rolled back")

    stats = window_stats(db, release)
    stable = get_stable_release(db, service_name)
    db.execute("UPDATE releases SET status = 'rolled_back', updated_at = ? WHERE id = ?",
               (now_iso(), release_id))
    write_audit(db, service_name, "canary_rolled_back",
                {"version": release["version"], "percentage": release["percentage"],
                 "restored_version": stable["version"] if stable else None,
                 "reason": reason, "window": stats},
                version=release["version"])
    return {
        "rolled_back": release_dict(get_release(db, release_id)),
        "restored_to": release_dict(stable) if stable else None,
        "message": f"traffic restored to {stable['version']}" if stable else "no stable version available",
    }


@app.post("/services/{service_name}/rollback")
def rollback_service(service_name: str,
                     reason: str = Query(default="service rollback"), db: Connection = Depends(get_db)):
    """Roll the whole service back: current stable is demoted, previous stable restored."""
    require_service(db, service_name)
    active = get_progressing_or_paused(db, service_name)
    if active is not None:
        raise HTTPException(
            status_code=409,
            detail=f"canary {active['version']!r} is still {active['status']}; "
                   f"roll it back before rolling back the stable version",
        )
    current = get_stable_release(db, service_name)
    if current is None:
        raise HTTPException(status_code=409, detail="no stable release to roll back")
    target = previous_complete(db, service_name, current["id"])
    if target is None:
        raise HTTPException(status_code=409, detail="no previous stable release exists")

    db.execute("UPDATE releases SET status = 'rolled_back', updated_at = ? WHERE id = ?",
               (now_iso(), current["id"]))
    # The target is already 'complete' at 100%, so routing automatically returns to it.
    write_audit(db, service_name, "service_rolled_back",
                {"from_version": current["version"], "restored_version": target["version"],
                 "reason": reason},
                version=target["version"])
    return {
        "rolled_back": release_dict(get_release(db, current["id"])),
        "restored_to": release_dict(target),
        "message": f"service restored to {target['version']}",
    }


# --------------------------------------------------------------------------- #
# Telemetry
# --------------------------------------------------------------------------- #
def _insert_event(db: Connection, service_name: str, release_id: int, event: TelemetryEvent) -> None:
    db.execute(
        "INSERT INTO telemetry (service_name, release_id, user_id, success, latency_ms, status_code, "
        "occurred_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (service_name, release_id, event.user_id, 1 if event.success else 0, event.latency_ms,
         event.status_code, event.occurred_at or now_iso()),
    )


def _evaluate_active_releases(db: Connection, service_name: str, release_ids: set) -> list:
    """Re-run the gate for every distinct active canary that received events."""
    results = []
    for release_id in release_ids:
        release = get_release(db, release_id)
        if release is not None and release["status"] in ("progressing", "paused"):
            results.append(evaluate_gate(db, release))
    return results


@app.post("/services/{service_name}/releases/{release_id}/telemetry")
def report_telemetry(service_name: str, release_id: int, event: TelemetryEvent,
                     db: Connection = Depends(get_db)):
    require_service(db, service_name)
    release = get_release(db, release_id)
    if release is None or release["service_name"] != service_name:
        raise HTTPException(status_code=404, detail="release not found")

    _insert_event(db, service_name, release_id, event)
    response = {"accepted": 1, "release_id": release_id}

    if release["status"] in ("progressing", "paused"):
        # The gate reads inside the same transaction, so the just-inserted event counts.
        refreshed = get_release(db, release_id)
        stats = evaluate_gate(db, refreshed)
        response["window"] = stats
        if stats["auto_paused"]:
            stable = get_stable_release(db, service_name)
            response["auto_paused"] = True
            response["message"] = (
                f"release {release['version']} auto-paused; traffic remains on "
                f"{stable['version'] if stable else 'nothing (no baseline)'}"
            )
    return response


@app.post("/services/{service_name}/telemetry")
def report_telemetry_batch(service_name: str, batch: TelemetryBatch,
                           db: Connection = Depends(get_db)):
    """Report many events in one call; every event must carry its release_id."""
    require_service(db, service_name)

    release_ids = set()
    seen = set()
    for idx, event in enumerate(batch.events):
        if event.release_id is None:
            raise HTTPException(status_code=400,
                               detail=f"events[{idx}].release_id is required for batch telemetry")
        if event.release_id not in seen:
            release = get_release(db, event.release_id)
            if release is None or release["service_name"] != service_name:
                raise HTTPException(status_code=404,
                                   detail=f"events[{idx}]: release {event.release_id} not found")
            seen.add(event.release_id)
        _insert_event(db, service_name, event.release_id, event)
        release_ids.add(event.release_id)

    gate_results = _evaluate_active_releases(db, service_name, release_ids)
    paused = [r for r in gate_results if r["auto_paused"]]
    return {
        "accepted": len(batch.events),
        "release_ids": sorted(release_ids),
        "windows": gate_results,
        "auto_paused": paused,
    }


# --------------------------------------------------------------------------- #
# Status & audit
# --------------------------------------------------------------------------- #
@app.get("/services/{service_name}/status")
def service_status(service_name: str, db: Connection = Depends(get_db)):
    """Gate overview: canary vs stable, latest window error rate, gate state.

    Reporting telemetry is the primary trigger for auto-pause; this endpoint is
    read-only but does re-evaluate the gate so dashboards see current state.
    """
    require_service(db, service_name)
    canary = get_progressing_or_paused(db, service_name)
    stable = get_stable_release(db, service_name)

    response = {
        "service": service_name,
        "time": now_iso(),
        "stable": release_dict(stable) if stable else None,
        "canary": None,
        "routing": {
            "canary_version": canary["version"] if canary else None,
            "canary_percentage": canary["percentage"] if canary else None,
            "stable_version": stable["version"] if stable else None,
            "paused": bool(canary and canary["status"] == "paused"),
        },
    }
    if canary is not None:
        stats = evaluate_gate(db, canary)
        response["canary"] = release_dict(get_release(db, canary["id"]))
        response["window"] = stats
    return response


@app.get("/services/{service_name}/audit")
def query_audit(
    service_name: str,
    action: Optional[str] = Query(default=None, description="Filter by action, e.g. auto_paused."),
    version: Optional[str] = Query(default=None),
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    db: Connection = Depends(get_db),
):
    require_service(db, service_name)
    sql = "SELECT * FROM audit_log WHERE service_name = ?"
    params: list = [service_name]
    if action:
        sql += " AND action = ?"
        params.append(action)
    if version:
        sql += " AND version = ?"
        params.append(version)
    sql += " ORDER BY id DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])
    rows = db.execute(sql, params).fetchall()
    total = db.execute("SELECT COUNT(*) AS c FROM audit_log WHERE service_name = ?",
                       (service_name,)).fetchone()["c"]
    return {
        "service": service_name,
        "total": total,
        "limit": limit,
        "offset": offset,
        "entries": [
            {
                "id": r["id"],
                "action": r["action"],
                "version": r["version"],
                "actor": r["actor"],
                "detail": json.loads(r["detail"]),
                "created_at": r["created_at"],
            }
            for r in rows
        ],
    }
