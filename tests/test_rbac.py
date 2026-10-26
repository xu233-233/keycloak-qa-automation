"""P8 扩展：RBAC 角色与权限矩阵用例。

验证四件事：
1. **composite 角色继承**：分配父角色，用户的有效角色里应出现子角色
2. **权限变更是否实时生效**：撤销 realm-admin 后，已签发的旧 token 还能不能管 admin API
3. **realm role 与 client role 的归属**：两者落在 role-mappings 的不同位置
4. **改密码后旧 token 是否失效**：这是很多系统的真实漏洞点

为什么不解析 token claims：
student-qa 这个 realm 的 user profile 被手工调过，access token 只剩极简 claims
（连 sub 都没有），roles scope 的 mapper 实际没写进 token。所以改用
role-mappings 接口 + 实际请求权限来验证——既不依赖 realm 配置漂移，也更接近黑盒效果。
"""
import uuid

import requests

from keycloak_client import KeycloakClient

PASSWORD = "Passw0rd!"


def _suffix() -> str:
    return uuid.uuid4().hex[:6]


class TestCompositeRole:
    def test_composite_role_is_inherited(self, kc, unique_name):
        """分配父角色后，用户的有效角色应同时包含父与子。"""
        suf = _suffix()
        parent_name, child_name = f"qa-parent-{suf}", f"qa-child-{suf}"
        kc.create_realm_role(parent_name)
        kc.create_realm_role(child_name)
        uid = kc.create_user(unique_name, password=PASSWORD)
        try:
            child = kc.get_realm_role(child_name).json()
            parent = kc.get_realm_role(parent_name).json()
            assert kc.assign_composites(parent, [child]).status_code == 204

            kc.assign_realm_roles(uid, [parent])
            names = [r["name"] for r in kc.get_effective_realm_roles(uid)]
            assert parent_name in names, "直接分配的角色必须在有效角色里"
            assert child_name in names, "父角色应把子角色一起继承过来"
        finally:
            kc.delete_user(uid)
            for name in (parent_name, child_name):
                kc.delete_realm_role(name)


class TestPermissionRevocation:
    def test_revoked_admin_role_takes_effect_immediately(self, kc, unique_name):
        """撤销 realm-admin 后，已签发的旧 token 立刻失去 admin 权限（403）。

        实测 Keycloak 会按当前授权状态校验，不是「JWT 签发即固定」。
        这条用例守的是「权限收回要即时生效」——如果哪天变成旧 token 仍能操作，
        就是安全回归。
        """
        uid = kc.create_user(unique_name, password=PASSWORD)
        try:
            kc.make_realm_admin(uid)
            token = kc.get_token(unique_name, PASSWORD)
            before = requests.get(
                kc.admin_url("/users"),
                headers={"Authorization": f"Bearer {token}"},
                timeout=10,
            )
            assert before.status_code == 200, before.text

            assert kc.revoke_realm_admin(uid).status_code == 204

            after = requests.get(
                kc.admin_url("/users"),
                headers={"Authorization": f"Bearer {token}"},
                timeout=10,
            )
            assert after.status_code == 403, "撤销后旧 token 不应再能管 admin API"
        finally:
            kc.delete_user(uid)

    def test_new_user_without_role_cannot_call_admin_api(self, kc, unique_name):
        """对照组：从没授过权的用户直接调 admin API → 403。"""
        uid = kc.create_user(unique_name, password=PASSWORD)
        try:
            token = kc.get_token(unique_name, PASSWORD)
            resp = requests.get(
                kc.admin_url("/users"),
                headers={"Authorization": f"Bearer {token}"},
                timeout=10,
            )
            assert resp.status_code == 403, resp.text
        finally:
            kc.delete_user(uid)


class TestRoleOwnership:
    def test_realm_role_and_client_role_land_in_different_places(self, kc, unique_name):
        """realm 角色进 realmMappings，client 角色进 clientMappings。"""
        suf = _suffix()
        realm_role_name = f"qa-realmrole-{suf}"
        kc.create_realm_role(realm_role_name)
        uid = kc.create_user(unique_name, password=PASSWORD)
        try:
            realm_role = kc.get_realm_role(realm_role_name).json()
            kc.assign_realm_roles(uid, [realm_role])
            kc.make_realm_admin(uid)  # 顺带分配一个 client 角色 realm-admin

            mappings = kc.get_role_mappings(uid)
            realm_names = [r["name"] for r in mappings.get("realmMappings", [])]
            assert realm_role_name in realm_names

            client_mappings = mappings.get("clientMappings", {})
            assert "realm-management" in client_mappings
            mgmt_roles = [r["name"] for r in client_mappings["realm-management"]["mappings"]]
            assert "realm-admin" in mgmt_roles
            # 两种角色不能串位：client 角色不该出现在 realmMappings 里
            assert "realm-admin" not in realm_names
        finally:
            kc.delete_user(uid)
            kc.delete_realm_role(realm_role_name)


class TestPasswordChange:
    def test_password_change_invalidates_old_password_but_not_token(self, kc, unique_name):
        """改密码后：旧密码失效、新密码可用，但**已签发的旧 token 仍然有效**。

        最后一点是常见认知误区。要真正做到「改密即踢下线」，需要额外撤销会话
        （admin logout 或开启 revoke refresh token），默认行为做不到。
        """
        old_pwd, new_pwd = "OldPass123!", "NewPass123!"
        uid = kc.create_user(unique_name, password=old_pwd)
        try:
            token = kc.get_token(unique_name, old_pwd)
            assert kc.reset_password(uid, new_pwd).status_code == 204

            # 旧密码不能用了
            resp = requests.post(
                kc.token_endpoint(),
                data={
                    "client_id": kc.client_id,
                    "grant_type": "password",
                    "username": unique_name,
                    "password": old_pwd,
                },
                timeout=10,
            )
            assert resp.status_code == 400, "旧密码必须失效"

            # 新密码可以登录
            assert kc.get_token(unique_name, new_pwd)

            # 旧 token 仍是「有效身份」（403 = 已认证但无权限，401 才是失效）
            still = requests.get(
                kc.admin_url("/users"),
                headers={"Authorization": f"Bearer {token}"},
                timeout=10,
            )
            assert still.status_code == 403, "默认配置下改密码不会撤销已签发的 access token"
        finally:
            kc.delete_user(uid)
