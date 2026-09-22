"""灰度发布闸门核心逻辑：稳定分桶、发布状态机、滑动窗口闸门、审计。

所有 *_locked 函数假定调用方已持有写事务（BEGIN IMMEDIATE），
以避免并发遥测请求重复触发自动暂停。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import HTTPException

# 窗口内样本不足该数量时不触发自动动作，避免冷启动误伤
MIN_TELEMETRY_SAMPLES = 5


# ---------------------------------------------------------------- 基础工具

def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _cutoff(window_seconds: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=window_seconds)).isoformat(
        timespec="seconds"
    )


def bucket(service: str, version: str, user_id: str) -> int:
    """稳定哈希分桶：同一 (服务, 版本, 用户) 永远落在同一桶 [0,99]。

    采用 SHA-256 截断取模，用户均匀分布；随百分比单调扩量时
    （10 -> 50 -> 100），已纳入灰度的用户不会被移出。
    """
    digest = hashlib.sha256(f"{service}|{version}|{user_id}".encode()).hexdigest()
    return int(digest[:16], 16) % 100


def add_audit(
    conn: sqlite3.Connection,
    service_id: int | None,
    release_id: int | None,
    action: str,
    actor: str,
    detail: dict[str, Any],
) -> None:
    conn.execute(
        "INSERT INTO audit_log (service_id, release_id, action, actor, detail, ts)"
        " VALUES (?,?,?,?,?,?)",
        (service_id, release_id, action, actor, json.dumps(detail, ensure_ascii=False), utcnow()),
    )


def get_service(conn: sqlite3.Connection, name: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM services WHERE name=?", (name,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"服务不存在: {name}")
    return row


def get_release(conn: sqlite3.Connection, service_id: int, version: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM releases WHERE service_id=? AND version=?", (service_id, version)
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"版本不存在: {version}")
    return row


def serialize_release(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "service_id": row["service_id"],
        "version": row["version"],
        "percentage": row["percentage"],
        "status": row["status"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _find_unfinished(conn: sqlite3.Connection, service_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM releases WHERE service_id=? AND status IN ('canary','paused')"
        " ORDER BY id DESC LIMIT 1",
        (service_id,),
    ).fetchone()


def _find_stable(conn: sqlite3.Connection, service_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM releases WHERE service_id=? AND status='stable'"
        " ORDER BY id DESC LIMIT 1",
        (service_id,),
    ).fetchone()


def _supersede_stable(conn: sqlite3.Connection, service_id: int) -> None:
    conn.execute(
        "UPDATE releases SET status='superseded', updated_at=? "
        "WHERE service_id=? AND status='stable'",
        (utcnow(), service_id),
    )


# ---------------------------------------------------------------- 发布生命周期

def create_release_locked(
    conn: sqlite3.Connection, service: sqlite3.Row, version: str, percentage: int, actor: str
) -> dict[str, Any]:
    if not 1 <= percentage <= 100:
        raise HTTPException(status_code=422, detail="percentage 必须在 1~100 之间")

    dup = conn.execute(
        "SELECT 1 FROM releases WHERE service_id=? AND version=?",
        (service["id"], version),
    ).fetchone()
    if dup:
        raise HTTPException(status_code=409, detail=f"版本已登记: {version}")

    if _find_unfinished(conn, service["id"]) is not None:
        raise HTTPException(status_code=409, detail="已有进行中的灰度版本，请先提升、暂停或回滚")

    has_stable = _find_stable(conn, service["id"]) is not None
    # 首个版本直接成为 stable；percentage=100 表示直接全量
    status = "canary" if (has_stable and percentage < 100) else "stable"
    now = utcnow()
    cur = conn.execute(
        "INSERT INTO releases (service_id, version, percentage, status, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?)",
        (service["id"], version, percentage, status, now, now),
    )
    release_id = cur.lastrowid

    superseded_version = None
    if status == "stable" and has_stable:
        old = _find_stable(conn, service["id"])
        superseded_version = old["version"]
        _supersede_stable(conn, service["id"])

    add_audit(
        conn, service["id"], release_id, "release.create", actor,
        {"version": version, "percentage": percentage, "status": status},
    )
    if superseded_version:
        add_audit(
            conn, service["id"], release_id, "release.stable", actor,
            {"version": version, "superseded": superseded_version},
        )

    row = conn.execute("SELECT * FROM releases WHERE id=?", (release_id,)).fetchone()
    return serialize_release(row)


def set_percentage_locked(
    conn: sqlite3.Connection,
    service: sqlite3.Row,
    release: sqlite3.Row,
    percentage: int,
    actor: str,
) -> dict[str, Any]:
    if release["status"] not in ("canary", "paused"):
        raise HTTPException(
            status_code=409,
            detail=f"仅进行中的灰度可以调整百分比，当前状态: {release['status']}",
        )
    if not 1 <= percentage <= 100:
        raise HTTPException(status_code=422, detail="percentage 必须在 1~100 之间")

    finalized = percentage == 100
    new_status = "stable" if finalized else release["status"]

    superseded_version = None
    if finalized:
        # 必须先把旧 stable 标记掉，再转正当前版本
        old = _find_stable(conn, service["id"])
        if old is not None:
            superseded_version = old["version"]
        _supersede_stable(conn, service["id"])

    conn.execute(
        "UPDATE releases SET percentage=?, status=?, updated_at=? WHERE id=?",
        (percentage, new_status, utcnow(), release["id"]),
    )
    add_audit(
        conn, service["id"], release["id"], "release.set_percentage", actor,
        {"version": release["version"], "percentage": percentage},
    )

    if finalized:
        add_audit(
            conn, service["id"], release["id"], "release.stable", actor,
            {"version": release["version"], "superseded": superseded_version},
        )

    row = conn.execute("SELECT * FROM releases WHERE id=?", (release["id"],)).fetchone()
    return serialize_release(row)


def pause_release_locked(
    conn: sqlite3.Connection,
    service: sqlite3.Row,
    release: sqlite3.Row,
    actor: str,
    reason: str,
) -> None:
    if release["status"] != "canary":
        raise HTTPException(status_code=409, detail=f"仅 canary 状态可暂停，当前: {release['status']}")
    conn.execute(
        "UPDATE releases SET status='paused', updated_at=? WHERE id=?",
        (utcnow(), release["id"]),
    )
    add_audit(
        conn, service["id"], release["id"], "release.pause", actor,
        {"version": release["version"], "reason": reason},
    )


def resume_release_locked(
    conn: sqlite3.Connection, service: sqlite3.Row, release: sqlite3.Row, actor: str
) -> dict[str, Any]:
    if release["status"] != "paused":
        raise HTTPException(status_code=409, detail=f"仅 paused 状态可恢复，当前: {release['status']}")
    conn.execute(
        "UPDATE releases SET status='canary', updated_at=? WHERE id=?",
        (utcnow(), release["id"]),
    )
    add_audit(
        conn, service["id"], release["id"], "release.resume", actor,
        {"version": release["version"], "percentage": release["percentage"]},
    )
    row = conn.execute("SELECT * FROM releases WHERE id=?", (release["id"],)).fetchone()
    return serialize_release(row)


def rollback_release_locked(
    conn: sqlite3.Connection,
    service: sqlite3.Row,
    release: sqlite3.Row,
    actor: str,
    reason: str,
) -> dict[str, Any]:
    """回滚灰度版本：状态置 rolled_back，流量自动回到当前 stable。"""
    if release["status"] not in ("canary", "paused"):
        raise HTTPException(
            status_code=409, detail=f"仅 canary/paused 可回滚，当前: {release['status']}"
        )
    stable = _find_stable(conn, service["id"])
    conn.execute(
        "UPDATE releases SET status='rolled_back', updated_at=? WHERE id=?",
        (utcnow(), release["id"]),
    )
    add_audit(
        conn, service["id"], release["id"], "release.rollback", actor,
        {
            "version": release["version"],
            "reason": reason,
            "rollback_to": stable["version"] if stable else None,
        },
    )
    row = conn.execute("SELECT * FROM releases WHERE id=?", (release["id"],)).fetchone()
    return serialize_release(row)


# ---------------------------------------------------------------- 遥测与闸门

def window_stats_locked(
    conn: sqlite3.Connection, release_id: int, window_seconds: int
) -> dict[str, int | float]:
    row = conn.execute(
        "SELECT COUNT(*) AS total,"
        " SUM(CASE WHEN success=0 THEN 1 ELSE 0 END) AS failures"
        " FROM telemetry WHERE release_id=? AND ts>=?",
        (release_id, _cutoff(window_seconds)),
    ).fetchone()
    total, failures = row["total"], row["failures"] or 0
    error_pct = round(failures * 100 / total, 2) if total else 0.0
    return {"total": total, "failures": failures, "error_pct": error_pct}


def _total_stats_locked(conn: sqlite3.Connection, release_id: int) -> dict[str, int]:
    row = conn.execute(
        "SELECT COUNT(*) AS total,"
        " SUM(CASE WHEN success=0 THEN 1 ELSE 0 END) AS failures"
        " FROM telemetry WHERE release_id=?",
        (release_id,),
    ).fetchone()
    return {"total": row["total"], "failures": row["failures"] or 0}


def record_telemetry_locked(
    conn: sqlite3.Connection,
    service: sqlite3.Row,
    version: str | None,
    user_id: str,
    success: bool,
) -> dict[str, Any]:
    """写入遥测，并在同事务内评估当前 canary 的滑动窗口错误率。"""
    canary = _find_unfinished(conn, service["id"])
    if version is not None:
        target = conn.execute(
            "SELECT * FROM releases WHERE service_id=? AND version=?",
            (service["id"], version),
        ).fetchone()
        if target is None:
            raise HTTPException(status_code=404, detail=f"版本不存在: {version}")
    elif canary is not None:
        target = canary
    else:
        stable = _find_stable(conn, service["id"])
        if stable is None:
            raise HTTPException(status_code=409, detail="该服务尚无任何已登记版本")
        target = stable

    now = utcnow()
    conn.execute(
        "INSERT INTO telemetry (service_id, release_id, user_id, success, ts)"
        " VALUES (?,?,?,?,?)",
        (service["id"], target["id"], user_id, 1 if success else 0, now),
    )

    result: dict[str, Any] = {
        "release": serialize_release(target),
        "window": None,
        "action": "none",
    }

    # 闸门只评估处于 canary 的版本（paused 后不再引流，不再重复触发）
    if target["status"] == "canary" and canary is not None and target["id"] == canary["id"]:
        stats = window_stats_locked(conn, target["id"], service["window_seconds"])
        result["window"] = stats
        threshold = service["error_threshold_pct"]
        if stats["total"] >= MIN_TELEMETRY_SAMPLES and stats["error_pct"] > threshold:
            if service["auto_rollback"]:
                rollback_release_locked(
                    conn, service, target, actor="gate",
                    reason=(
                        f"滑动窗口({service['window_seconds']}s)错误率 {stats['error_pct']}%"
                        f" 超过阈值 {threshold}%，自动回滚"
                    ),
                )
                result["action"] = "rolled_back"
            else:
                pause_release_locked(
                    conn, service, target, actor="gate",
                    reason=(
                        f"滑动窗口({service['window_seconds']}s)错误率 {stats['error_pct']}%"
                        f" 超过阈值 {threshold}%，自动暂停"
                    ),
                )
                result["action"] = "paused"
            row = conn.execute("SELECT * FROM releases WHERE id=?", (target["id"],)).fetchone()
            result["release"] = serialize_release(row)

    return result


# ---------------------------------------------------------------- 查询

def route_user(conn: sqlite3.Connection, service_name: str, user_id: str) -> dict[str, Any]:
    service = get_service(conn, service_name)
    stable = _find_stable(conn, service["id"])
    canary = _find_unfinished(conn, service["id"])  # 仅 canary/paused，但下面校验状态

    chosen, cohort = None, "stable"
    if canary is not None and canary["status"] == "canary":
        b = bucket(service_name, canary["version"], user_id)
        if b < canary["percentage"]:
            chosen, cohort = canary, f"canary:{b}"
    if chosen is None:
        chosen, cohort = stable, "stable"

    if chosen is None:
        raise HTTPException(status_code=409, detail="该服务尚无可用版本")

    return {
        "service": service_name,
        "user_id": user_id,
        "version": chosen["version"],
        "cohort": cohort,
        "release_status": chosen["status"],
    }


def get_status(conn: sqlite3.Connection, service_name: str) -> dict[str, Any]:
    service = get_service(conn, service_name)
    rows = conn.execute(
        "SELECT * FROM releases WHERE service_id=? ORDER BY id DESC", (service["id"],)
    ).fetchall()
    releases = []
    for r in rows:
        releases.append(
            {
                **serialize_release(r),
                "window": window_stats_locked(conn, r["id"], service["window_seconds"]),
                "total": _total_stats_locked(conn, r["id"]),
            }
        )
    stable = next((r["version"] for r in releases if r["status"] == "stable"), None)
    canary = next((r["version"] for r in releases if r["status"] in ("canary", "paused")), None)
    return {
        "service": service_name,
        "error_threshold_pct": service["error_threshold_pct"],
        "window_seconds": service["window_seconds"],
        "auto_rollback": bool(service["auto_rollback"]),
        "stable_version": stable,
        "active_canary": canary,
        "releases": releases,
    }


def list_services(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM services ORDER BY id").fetchall()
    return [
        {
            "name": r["name"],
            "error_threshold_pct": r["error_threshold_pct"],
            "window_seconds": r["window_seconds"],
            "auto_rollback": bool(r["auto_rollback"]),
            "created_at": r["created_at"],
        }
        for r in rows
    ]


def list_releases(conn: sqlite3.Connection, service_name: str) -> list[dict[str, Any]]:
    service = get_service(conn, service_name)
    rows = conn.execute(
        "SELECT * FROM releases WHERE service_id=? ORDER BY id DESC", (service["id"],)
    ).fetchall()
    return [serialize_release(r) for r in rows]


def list_audit(
    conn: sqlite3.Connection,
    service_name: str | None,
    action: str | None,
    limit: int,
    offset: int,
) -> list[dict[str, Any]]:
    sql = (
        "SELECT a.*, s.name AS service_name FROM audit_log a"
        " LEFT JOIN services s ON s.id = a.service_id WHERE 1=1"
    )
    params: list[Any] = []
    if service_name is not None:
        service = get_service(conn, service_name)
        sql += " AND a.service_id=?"
        params.append(service["id"])
    if action is not None:
        sql += " AND a.action=?"
        params.append(action)
    sql += " ORDER BY a.id DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])
    rows = conn.execute(sql, params).fetchall()
    return [
        {
            "id": r["id"],
            "service": r["service_name"],
            "release_id": r["release_id"],
            "action": r["action"],
            "actor": r["actor"],
            "detail": json.loads(r["detail"]),
            "ts": r["ts"],
        }
        for r in rows
    ]
