"""P3：安全专项用例。

对应方案里的安全清单：
1. 无 token                  → 401
2. 篡改 JWT（伪造提权）      → 401
3. 普通用户调 admin API      → 403（越权拦截）
4. 跨 realm 访问             → 被拒绝（401/403）

说明：
- 普通用户 token 用用例内临时创建的用户获取，不依赖手工建的 qa-user 密码，
  这样在 CI/换机时也能自洽运行。
- 跨 realm 用例用 student-qa 的 admin token 访问 master realm：令牌签发域与
  目标域不一致，实测 Keycloak 26.8.0 返回 403（无权访问该 realm），故严格断言 403。
"""
import base64
import json

import requests

from keycloak_client import KeycloakClient

PASSWORD = "Passw0rd!"


# ------------------------------------------------------------------ 工具函数
def _b64decode(segment: str) -> dict:
    segment += "=" * (-len(segment) % 4)
    return json.loads(base64.urlsafe_b64decode(segment))


def _b64encode(obj: dict) -> str:
    raw = json.dumps(obj, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _tamper(token: str, mutate) -> str:
    """解出 payload → 篡改 → 用原签名重新拼装（签名必然失配）。"""
    header, payload, signature = token.split(".")
    claims = _b64decode(payload)
    mutate(claims)
    return ".".join([header, _b64encode(claims), signature])


# --------------------------------------------------------------- 1. 无 token
def test_no_token_returns_401(kc: KeycloakClient):
    """完全不带 Authorization 头，admin API 应返回 401。"""
    resp = requests.get(kc.admin_url("/users"))
    assert resp.status_code == 401, resp.text


def test_garbage_token_returns_401(kc: KeycloakClient):
    """伪造一个乱码 token，应返回 401。"""
    resp = requests.get(
        kc.admin_url("/users"),
        headers={"Authorization": "Bearer not-a-real-token"},
    )
    assert resp.status_code == 401, resp.text


# ----------------------------------------------------------- 2. 篡改 JWT 提权
def test_tampered_jwt_returns_401(kc: KeycloakClient):
    """把 payload 里的 realm 角色改成 realm-admin，签名失效后应 401。"""
    token = kc.admin_token

    def escalate(claims: dict) -> None:
        roles = claims.setdefault("realm_access", {}).setdefault("roles", [])
        if "realm-admin" not in roles:
            roles.append("realm-admin")

    tampered = _tamper(token, escalate)
    assert tampered != token
    resp = requests.get(
        kc.admin_url("/users"),
        headers={"Authorization": f"Bearer {tampered}"},
    )
    assert resp.status_code == 401, resp.text


# --------------------------------------------------- 3. 普通用户越权调 admin
def test_normal_user_cannot_call_admin_api(kc: KeycloakClient, unique_name):
    """普通用户（无 realm-management 角色）调 admin API，应 403。"""
    user_id = kc.create_user(unique_name, password=PASSWORD)
    try:
        user_token = kc.get_token(unique_name, PASSWORD)
        resp = requests.get(
            kc.admin_url("/users"),
            headers={"Authorization": f"Bearer {user_token}"},
        )
        assert resp.status_code == 403, resp.text
    finally:
        kc.delete_user(user_id)


# --------------------------------------------------------- 4. 跨 realm 隔离
def test_cross_realm_access_denied(kc: KeycloakClient):
    """student-qa 的 admin token 访问 master realm 的 admin API，应被拒绝。"""
    resp = requests.get(
        f"{kc.base_url}/admin/realms/master/users",
        headers={"Authorization": f"Bearer {kc.admin_token}"},
    )
    assert resp.status_code == 403, resp.text
