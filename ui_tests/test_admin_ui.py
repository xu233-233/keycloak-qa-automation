"""P4：Keycloak 管理台 UI 自动化用例（Playwright）。

覆盖方案里的三条 UI 流程：
1. 后台登录
2. 建用户
3. 配角色（给用户分配 realm 角色）
4. 建 realm（额外覆盖，建完用 API 校验并清理）

设计原则：UI 负责「执行动作」，API 负责「校验结果 + 清理」，这样断言稳、可重复跑。
"""
import time
import uuid

import pytest
import requests
from playwright.sync_api import expect

from ui_tests.pages import AdminConsole

pytestmark = pytest.mark.ui


def _poll(fn, timeout: float = 10.0, interval: float = 0.3):
    """轮询直到 fn 返回真值（UI 动作落库有延迟，避免竞态）。"""
    end = time.time() + timeout
    while time.time() < end:
        try:
            value = fn()
        except Exception:
            value = None
        if value:
            return value
        time.sleep(interval)
    return fn()


def _login(page, base_url, credentials):
    console = AdminConsole(page, base_url)
    console.login(*credentials)
    return console


# --------------------------------------------------------------- 1. 登录
def test_admin_login(page, base_url, master_credentials):
    _login(page, base_url, master_credentials)
    expect(page.get_by_test_id("currentRealm")).to_be_visible()


# ------------------------------------------------------------- 2. 建用户
def test_create_user_ui(page, base_url, master_credentials, kc, realm, unique_name):
    _login(page, base_url, master_credentials)

    email = f"{unique_name}@qa.local"
    AdminConsole(page, base_url).create_user(realm, unique_name, email, "UI", "Tester")

    uid = _poll(lambda: kc.find_user_by_username(unique_name))
    try:
        assert uid, "UI 建用户后，API 查不到该用户"
        body = kc.get_user(uid).json()
        assert body["username"] == unique_name
        assert body["email"] == email
    finally:
        if uid:
            kc.delete_user(uid)


# ------------------------------------------------------------- 3. 配角色
def test_assign_realm_role_ui(page, base_url, master_credentials, kc, realm, unique_name):
    role = f"qa-viewer-{uuid.uuid4().hex[:6]}"

    # 预置：建 realm 角色 + 建用户（走 API，减少 UI 噪声，专注「配角色」这一步）
    requests.post(
        kc.admin_url("/roles"),
        json={"name": role},
        headers=kc.auth_headers(),
    ).raise_for_status()
    uid = kc.create_user(unique_name, password="Passw0rd!")

    try:
        _login(page, base_url, master_credentials)
        AdminConsole(page, base_url).assign_realm_role(realm, uid, role)

        # 校验：该用户确实拿到了这个 realm 角色（UI 落库有延迟，轮询）
        assigned = _poll(
            lambda: role
            in [
                r["name"]
                for r in requests.get(
                    kc.admin_url(f"/users/{uid}/role-mappings/realm"),
                    headers=kc.auth_headers(),
                ).json()
            ]
        )
        assert assigned, f"UI 分配后角色未生效"
    finally:
        kc.delete_user(uid)
        requests.delete(kc.admin_url(f"/roles/{role}"), headers=kc.auth_headers())


# ------------------------------------------------------------- 4. 建 realm
def test_create_realm_ui(page, base_url, master_credentials, kc_master):
    new_realm = f"qa-realm-{uuid.uuid4().hex[:6]}"

    _login(page, base_url, master_credentials)

    try:
        AdminConsole(page, base_url).create_realm(new_realm)

        created = _poll(
            lambda: requests.get(
                f"{base_url}/admin/realms/{new_realm}",
                headers=kc_master.auth_headers(),
            ).status_code
            == 200
        )
        assert created, f"UI 建 realm 后，API 查不到 {new_realm}"
    finally:
        requests.delete(
            f"{base_url}/admin/realms/{new_realm}",
            headers=kc_master.auth_headers(),
        )
