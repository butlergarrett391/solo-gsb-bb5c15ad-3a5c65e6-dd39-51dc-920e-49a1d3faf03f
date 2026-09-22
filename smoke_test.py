"""用桩模块在无 fastapi/pydantic 的环境下冒烟测试核心业务逻辑。"""
import importlib
import os
import sys
import tempfile
import types

# ---- fastapi 桩 ----
fastapi = types.ModuleType("fastapi")


class HTTPException(Exception):
    def __init__(self, status_code, detail):
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


def Depends(fn):
    return ("dep", fn)


class QueryDefault:
    def __init__(self, default=..., **kw):
        self.default = default
        self.kw = kw


def Query(default=..., **kw):
    return QueryDefault(default, **kw)


fastapi.HTTPException = HTTPException
fastapi.Depends = Depends
fastapi.Query = Query
sys.modules["fastapi"] = fastapi

# ---- pydantic 桩（仅转字段，跳过校验；真实环境由 pydantic 负责）----
pydantic = types.ModuleType("pydantic")


class BaseModel:
    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)

    def model_dump(self):
        return {k: v for k, v in self.__dict__.items() if not k.startswith("_")}


class _Field:
    def __init__(self, default=..., **kw):
        self.default = default


def Field(default=..., **kw):
    return _Field(default, **kw)


pydantic.BaseModel = BaseModel
pydantic.Field = Field
sys.modules["pydantic"] = pydantic

# ---- 用临时 DB ----
tmp = tempfile.mkdtemp()
os.environ["GATE_DB_PATH"] = os.path.join(tmp, "gate.db")

from app import gate  # noqa: E402
from app import db  # noqa: E402

db.init_db()
conn = db.get_conn()

# 1. 登记服务：窗口 300s，阈值 50%，先不自动回滚
conn.execute("BEGIN IMMEDIATE")
cur = conn.execute(
    "INSERT INTO services (name,error_threshold_pct,window_seconds,auto_rollback,created_at)"
    " VALUES ('payments',50,300,0,?)",
    (gate.utcnow(),),
)
gate.add_audit(conn, cur.lastrowid, None, "service.create", "test", {"name": "payments"})
svc = gate.get_service(conn, "payments")

# 2. 首版本 v1 直接 stable
r1 = gate.create_release_locked(conn, svc, "v1", 10, "test")
assert r1["status"] == "stable", r1
conn.commit()
print("1) v1 ->", r1["status"])

# 3. 发 v2 灰度 10%
conn.execute("BEGIN IMMEDIATE")
svc = gate.get_service(conn, "payments")
r2 = gate.create_release_locked(conn, svc, "v2", 10, "test")
assert r2["status"] == "canary"
conn.commit()
print("2) v2 ->", r2["status"], "percentage", r2["percentage"])

# 4. 稳定哈希：同用户恒定，且百分比单调扩量
routed = {}
buckets = []
for i in range(2000):
    uid = f"user-{i}"
    res = gate.route_user(conn, "payments", uid)
    b = int(res["cohort"].split(":")[1]) if res["cohort"].startswith("canary") else None
    if b is not None:
        buckets.append(b)
    routed[uid] = res["version"]
    assert res["version"] in ("v1", "v2")
canary_users = [u for u, v in routed.items() if v == "v2"]
ratio = len(canary_users) / 2000
assert 0.07 < ratio < 0.13, ratio
# 再次路由结果一致
assert all(gate.route_user(conn, "payments", u)["version"] == routed[u] for u in list(routed)[:200])
# 桶都在 [0,10)
assert all(0 <= b < 10 for b in buckets)
print(f"3) 10% 灰度实际比例 {ratio:.3f}，哈希稳定 ✓")

# 5. 提到 50%：原来在 v2 的用户必须还在 v2（单调）
conn.execute("BEGIN IMMEDIATE")
svc = gate.get_service(conn, "payments")
gate.set_percentage_locked(conn, svc, gate.get_release(conn, svc["id"], "v2"), 50, "test")
conn.commit()
old_canary = set(canary_users)
now_v2 = {u for u in routed if gate.route_user(conn, "payments", u)["version"] == "v2"}
assert old_canary <= now_v2, "扩量后原灰度用户被移出！"
ratio50 = len(now_v2) / 2000
assert 0.47 < ratio50 < 0.53, ratio50
print(f"4) 提到 50% 实际比例 {ratio50:.3f}，原灰度用户全部保留 ✓")

# 6. 遥测：前 4 次失败（样本不足 5 不触发），第 5 次失败后错误率 100% > 50% 自动暂停
probe = sorted(now_v2)[:5]
for u in probe[:4]:
    conn.execute("BEGIN IMMEDIATE")
    svc = gate.get_service(conn, "payments")
    res = gate.record_telemetry_locked(conn, svc, None, u, False)
    conn.commit()
    assert res["action"] == "none", res
conn.execute("BEGIN IMMEDIATE")
svc = gate.get_service(conn, "payments")
res = gate.record_telemetry_locked(conn, svc, None, probe[4], False)
conn.commit()
assert res["action"] == "paused", res
assert res["release"]["status"] == "paused"
assert res["window"]["failures"] == 5 and res["window"]["error_pct"] == 100.0
print("5) 第5个失败样本触发自动暂停，窗口:", res["window"])

# 7. paused 后流量全部回到 v1
assert all(
    gate.route_user(conn, "payments", u)["version"] == "v1" for u in sorted(routed)[:300]
)
print("6) 暂停后流量全部回退 stable v1 ✓")

# 8. resume 后恢复灰度
conn.execute("BEGIN IMMEDIATE")
svc = gate.get_service(conn, "payments")
gate.resume_release_locked(conn, svc, gate.get_release(conn, svc["id"], "v2"), "test")
conn.commit()
assert gate.route_user(conn, "payments", probe[0])["version"] == "v2"
print("7) resume 后灰度恢复 ✓")

# 9. 手动回滚
conn.execute("BEGIN IMMEDIATE")
svc = gate.get_service(conn, "payments")
rb = gate.rollback_release_locked(conn, svc, gate.get_release(conn, svc["id"], "v2"), "test", "manual")
conn.commit()
assert rb["status"] == "rolled_back"
assert gate.route_user(conn, "payments", probe[0])["version"] == "v1"
print("8) 回滚 v2 -> rolled_back，路由回到 v1 ✓")

# 10. auto_rollback 路径 + promote 全量转正
conn.execute("UPDATE services SET auto_rollback=1 WHERE name='payments'")
conn.commit()
conn.execute("BEGIN IMMEDIATE")
svc = gate.get_service(conn, "payments")
r3 = gate.create_release_locked(conn, svc, "v3", 20, "test")
conn.commit()
assert r3["status"] == "canary"
conn.execute("BEGIN IMMEDIATE")
svc = gate.get_service(conn, "payments")
v3_users = [u for u in list(routed)[:100]
            if gate.route_user(conn, "payments", u)["version"] == "v3"][:5]
assert len(v3_users) == 5
for u in v3_users:
    conn_again = conn
    svc = gate.get_service(conn_again, "payments")
    res = gate.record_telemetry_locked(conn_again, svc, None, u, False)
conn.commit()
assert res["action"] == "rolled_back", res
print("9) auto_rollback=true 时超阈值自动回滚 ✓")

# v4 走 promote 100
conn.execute("BEGIN IMMEDIATE")
svc = gate.get_service(conn, "payments")
gate.create_release_locked(conn, svc, "v4", 30, "test")
svc = gate.get_service(conn, "payments")
r4 = gate.set_percentage_locked(conn, svc, gate.get_release(conn, svc["id"], "v4"), 100, "test")
conn.commit()
assert r4["status"] == "stable"
# v1 应已 superseded
old_v1 = gate.get_release(conn, svc["id"], "v1")
assert old_v1["status"] == "superseded", old_v1["status"]
assert gate.route_user(conn, "payments", "anyone")["version"] == "v4"
print("10) promote 到 100 全量转正，v1 superseded ✓")

# 11. 状态与审计
st = gate.get_status(conn, "payments")
assert st["stable_version"] == "v4"
assert len(st["releases"]) == 4
for rel in st["releases"]:
    assert "window" in rel and "total" in rel
entries = gate.list_audit(conn, "payments", None, 100, 0)
actions = {e["action"] for e in entries}
assert {"release.create", "release.pause", "release.resume", "release.rollback",
        "release.stable", "release.set_percentage", "service.create"} <= actions, actions
print(f"11) 状态聚合 + {len(entries)} 条审计日志 ✓")

# 12. 错误处理
def expect_4xx(fn, code):
    try:
        fn()
    except HTTPException as e:
        assert e.status_code == code, (e.status_code, code)
        return
    raise AssertionError("应当抛 HTTPException")

expect_4xx(lambda: gate.get_service(conn, "nope"), 404)
conn.execute("BEGIN IMMEDIATE")
expect_4xx(lambda: gate.create_release_locked(conn, svc, "v4", 10, "t"), 409)
expect_4xx(lambda: gate.create_release_locked(conn, svc, "v5", 0, "t"), 422)
conn.rollback()
print("12) 404/409/422 错误路径 ✓")

conn.close()
print("\n全部冒烟用例通过 ✅")
