"""P8 扩展：多租户隔离用例。

设计思路——把 realm 当成一个租户（一家「租赁公司」）：
- 平台方（master admin）负责开户 / 停用 / 销户
- 租户管理员（realm-management 的 realm-admin）只能管自己那一家
- 租户之间数据完全隔离：同名用户可并存、用户 id 不互通、token 不通用

这组用例补上了原 P2 的盲区：原来只测「一个 realm 内的 CRUD 和安全」，
没测「多个租户之间会不会串」。而多租户系统最典型的线上事故恰恰是串租户。
"""
import uuid

import pytest
import requests

from keycloak_client import KeycloakError

PASSWORD = "Passw0rd!"


def _uid() -> str:
    return uuid.uuid4().hex[:8]


# ============================================================ A. 租户生命周期
class TestTenantLifecycle:
    """开户 / 重名 / 销户 / 停用 —— 平台方视角。"""

    def test_create_tenant_returns_201(self, platform):
        """开户成功返回 201，且立刻能查到。"""
        name = f"qa-tmp-{_uid()}"
        try:
            resp = platform.create_realm(name)
            assert resp.status_code == 201, resp.text
            assert platform.get_realm(name).status_code == 200
        finally:
            platform.delete_realm(name)

    def test_duplicate_tenant_name_conflict(self, platform, tenants):
        """开一个已存在的租户名 → 409，不能静默覆盖。"""
        resp = platform.create_realm(tenants["a"])
        assert resp.status_code == 409, resp.text

    def test_delete_tenant_makes_it_gone(self, platform):
        """销户后查不到（404），且数据不可再访问。"""
        name = f"qa-tmp-{_uid()}"
        platform.create_realm(name)
        assert platform.delete_realm(name).status_code == 204
        assert platform.get_realm(name).status_code == 404

    def test_disabled_tenant_rejects_login(self, platform, kc_of, tenants):
        """停用租户后，该租户里已存在的用户也换不到 token。"""
        realm = tenants["b"]
        c = kc_of(realm)
        name = f"qa-{_uid()}"
        uid = c.create_user(name, password=PASSWORD)
        try:
            assert c.get_token(name, PASSWORD)  # 停用前正常
            platform.set_realm_enabled(realm, False)
            with pytest.raises(KeycloakError):
                c.get_token(name, PASSWORD)
        finally:
            platform.set_realm_enabled(realm, True)
            c.delete_user(uid)


# ============================================================ B. 租户权限边界
class TestTenantPermissionBoundary:
    """租户管理员 vs 平台管理员：能不能越界。"""

    def test_tenant_admin_can_manage_own_tenant(self, tenants, kc_of):
        """对照组：租户管理员管自己租户 → 200。"""
        realm = tenants["a"]
        c = kc_of(realm)
        name = f"qa-{_uid()}"
        uid = c.create_user(name, password=PASSWORD)
        c.make_realm_admin(uid)
        try:
            token = c.get_token(name, PASSWORD)
            resp = requests.get(
                c.admin_url("/users"),
                headers={"Authorization": f"Bearer {token}"},
                timeout=10,
            )
            assert resp.status_code == 200, resp.text
        finally:
            c.delete_user(uid)

    def test_tenant_admin_cannot_manage_other_tenant(self, tenants, kc_of):
        """租户 A 的管理员调租户 B 的 admin API → 403（串租户红线）。"""
        realm_a, realm_b = tenants["a"], tenants["b"]
        c = kc_of(realm_a)
        name = f"qa-{_uid()}"
        uid = c.create_user(name, password=PASSWORD)
        c.make_realm_admin(uid)
        try:
            token = c.get_token(name, PASSWORD)
            resp = requests.get(
                c.admin_url_of(realm_b, "/users"),
                headers={"Authorization": f"Bearer {token}"},
                timeout=10,
            )
            assert resp.status_code == 403, resp.text
        finally:
            c.delete_user(uid)

    def test_tenant_admin_cannot_create_tenant(self, tenants, kc_of):
        """租户管理员擅自开户 → 403。开户是平台方特权。"""
        realm = tenants["a"]
        c = kc_of(realm)
        name = f"qa-{_uid()}"
        uid = c.create_user(name, password=PASSWORD)
        c.make_realm_admin(uid)
        evil = f"qa-evil-{_uid()}"
        try:
            token = c.get_token(name, PASSWORD)
            resp = requests.post(
                f"{c.base_url}/admin/realms",
                json={"realm": evil},
                headers={"Authorization": f"Bearer {token}"},
                timeout=15,
            )
            assert resp.status_code == 403, resp.text
        finally:
            c.delete_user(uid)


# ============================================================ C. 租户数据隔离
class TestTenantDataIsolation:
    """名字一样、id 一样，都不能串。"""

    def test_same_username_coexists_in_two_tenants(self, tenants, kc_of):
        """同一用户名可在两个租户各建一份，且 user id 不同。"""
        name = f"qa-{_uid()}"
        ca, cb = kc_of(tenants["a"]), kc_of(tenants["b"])
        ida = ca.create_user(name, password=PASSWORD)
        idb = cb.create_user(name, password=PASSWORD)
        try:
            assert ida != idb, "不同租户的同名用户必须是两份独立数据"
            assert ca.get_user(ida).json()["username"] == name
            assert cb.get_user(idb).json()["username"] == name
        finally:
            ca.delete_user(ida)
            cb.delete_user(idb)

    def test_user_id_not_visible_across_tenants(self, tenants, kc_of):
        """拿租户 A 的用户 id 去租户 B 查 → 404。"""
        name = f"qa-{_uid()}"
        ca, cb = kc_of(tenants["a"]), kc_of(tenants["b"])
        uid = ca.create_user(name, password=PASSWORD)
        try:
            assert ca.get_user(uid).status_code == 200
            assert cb.get_user(uid).status_code == 404
        finally:
            ca.delete_user(uid)

    def test_user_token_rejected_by_other_tenant(self, tenants, kc_of):
        """租户 A 的用户 token 打租户 B 的 userinfo → 401。"""
        name = f"qa-{_uid()}"
        ca, cb = kc_of(tenants["a"]), kc_of(tenants["b"])
        uid = ca.create_user(name, password=PASSWORD)
        try:
            token = ca.get_token(name, PASSWORD)
            resp = requests.get(
                f"{cb.base_url}/realms/{tenants['b']}/protocol/openid-connect/userinfo",
                headers={"Authorization": f"Bearer {token}"},
                timeout=10,
            )
            assert resp.status_code == 401, resp.text
        finally:
            ca.delete_user(uid)

    def test_delete_user_in_a_keeps_same_name_in_b(self, tenants, kc_of):
        """删掉租户 A 的用户，租户 B 的同名用户不受影响（不会级联误删）。"""
        name = f"qa-{_uid()}"
        ca, cb = kc_of(tenants["a"]), kc_of(tenants["b"])
        ida = ca.create_user(name, password=PASSWORD)
        idb = cb.create_user(name, password=PASSWORD)
        try:
            assert ca.delete_user(ida).status_code == 204
            assert ca.get_user(ida).status_code == 404
            assert cb.get_user(idb).status_code == 200
            assert cb.get_token(name, PASSWORD), "B 租户的用户必须还能登录"
        finally:
            cb.delete_user(idb)
