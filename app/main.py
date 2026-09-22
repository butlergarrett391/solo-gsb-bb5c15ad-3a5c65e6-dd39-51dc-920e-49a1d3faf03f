"""灰度发布闸门 API。"""
from __future__ import annotations

from typing import Annotated

from fastapi import Depends, FastAPI, Query
from sqlite3 import Connection

from . import gate
from .db import get_conn, init_db
from .schemas import PromoteRequest, ReleaseCreate, RollbackRequest, ServiceCreate, TelemetryRequest

app = FastAPI(
    title="灰度发布闸门 API (Canary Release Gate)",
    version="1.0.0",
    description=(
        "登记服务版本与灰度百分比，按用户稳定哈希分配 cohort；"
        "接收成功/失败遥测，滑动窗口错误率超阈值自动暂停（可选自动回滚）。"
    ),
)


@app.on_event("startup")
def _startup() -> None:
    init_db()


Db = Annotated[Connection, Depends(get_conn)]


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


# ---------------- 服务登记

@app.post("/api/services", status_code=201)
def create_service(body: ServiceCreate, conn: Db) -> dict:
    try:
        conn.execute("BEGIN IMMEDIATE")
        now = gate.utcnow()
        cur = conn.execute(
            "INSERT INTO services (name, error_threshold_pct, window_seconds,"
            " auto_rollback, created_at) VALUES (?,?,?,?,?)",
            (
                body.name,
                body.error_threshold_pct,
                body.window_seconds,
                1 if body.auto_rollback else 0,
                now,
            ),
        )
        gate.add_audit(conn, cur.lastrowid, None, "service.create", "api", body.model_dump())
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    row = conn.execute("SELECT * FROM services WHERE name=?", (body.name,)).fetchone()
    return {
        "name": row["name"],
        "error_threshold_pct": row["error_threshold_pct"],
        "window_seconds": row["window_seconds"],
        "auto_rollback": bool(row["auto_rollback"]),
        "created_at": row["created_at"],
    }


@app.get("/api/services")
def services(conn: Db) -> list[dict]:
    return gate.list_services(conn)


@app.get("/api/services/{name}")
def service_status(name: str, conn: Db) -> dict:
    return gate.get_status(conn, name)


@app.get("/api/services/{name}/releases")
def service_releases(name: str, conn: Db) -> list[dict]:
    return gate.list_releases(conn, name)


# ---------------- 灰度路由

@app.get("/api/services/{name}/route")
def route(name: str, user_id: Annotated[str, Query(min_length=1)], conn: Db) -> dict:
    return gate.route_user(conn, name, user_id)

# ---------------- 发布 / 提升 / 暂停 / 恢复 / 回滚

@app.post("/api/services/{name}/releases", status_code=201)
def create_release(name: str, body: ReleaseCreate, conn: Db) -> dict:
    try:
        conn.execute("BEGIN IMMEDIATE")
        service = gate.get_service(conn, name)
        result = gate.create_release_locked(
            conn, service, body.version, body.percentage, body.actor
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return result


@app.post("/api/services/{name}/releases/{version}/promote")
def promote(name: str, version: str, body: PromoteRequest, conn: Db) -> dict:
    try:
        conn.execute("BEGIN IMMEDIATE")
        service = gate.get_service(conn, name)
        release = gate.get_release(conn, service["id"], version)
        result = gate.set_percentage_locked(
            conn, service, release, body.percentage, body.actor
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return result


@app.post("/api/services/{name}/releases/{version}/pause")
def pause(name: str, version: str, body: RollbackRequest, conn: Db) -> dict:
    try:
        conn.execute("BEGIN IMMEDIATE")
        service = gate.get_service(conn, name)
        release = gate.get_release(conn, service["id"], version)
        gate.pause_release_locked(conn, service, release, body.actor, body.reason)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return gate.serialize_release(gate.get_release(conn, service["id"], version))


@app.post("/api/services/{name}/releases/{version}/resume")
def resume(name: str, version: str, body: RollbackRequest, conn: Db) -> dict:
    try:
        conn.execute("BEGIN IMMEDIATE")
        service = gate.get_service(conn, name)
        release = gate.get_release(conn, service["id"], version)
        result = gate.resume_release_locked(conn, service, release, body.actor)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return result


@app.post("/api/services/{name}/releases/{version}/rollback")
def rollback(name: str, version: str, body: RollbackRequest, conn: Db) -> dict:
    try:
        conn.execute("BEGIN IMMEDIATE")
        service = gate.get_service(conn, name)
        release = gate.get_release(conn, service["id"], version)
        result = gate.rollback_release_locked(
            conn, service, release, body.actor, body.reason
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return result


# ---------------- 遥测

@app.post("/api/services/{name}/telemetry")
def telemetry(name: str, body: TelemetryRequest, conn: Db) -> dict:
    try:
        conn.execute("BEGIN IMMEDIATE")
        service = gate.get_service(conn, name)
        result = gate.record_telemetry_locked(
            conn, service, body.version, body.user_id, body.success
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return result


# ---------------- 审计

@app.get("/api/audit")
def audit(
    conn: Db,
    service: str | None = Query(None, description="按服务名过滤"),
    action: str | None = Query(None, description="按动作过滤，如 release.pause"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> list[dict]:
    return gate.list_audit(conn, service, action, limit, offset)
