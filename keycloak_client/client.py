"""Keycloak Admin REST API 封装（P2 核心，P3 复用）。

覆盖能力：
- 认证：管理员账号 / 任意普通用户换取 access_token（Resource Owner Password 流程）
- 用户生命周期：创建 / 查询 / 更新 / 删除 / 列表
- 原始 admin 请求：供 P3 安全用例直接发请求（无 token / 篡改 token / 跨 realm）

被测系统：docker-compose 起的 Keycloak 26（start-dev，默认 8080 端口）。
"""
from __future__ import annotations

import json

import requests


class KeycloakError(Exception):
    """封装层自定义异常，便于用例断言时区分业务失败与网络失败。"""


class KeycloakClient:
    """对 Keycloak Admin REST API 的轻量封装。

    典型用法::

        kc = KeycloakClient("http://localhost:8080", "student-qa",
                            admin_user="qa-admin", admin_password="qa-admin-123")
        uid = kc.create_user("demo", password="Passw0rd!")
        assert kc.get_user(uid).status_code == 200
        kc.delete_user(uid)
    """

    def __init__(
        self,
        base_url: str,
        realm: str,
        client_id: str = "admin-cli",
        admin_user: str | None = None,
        admin_password: str | None = None,
        timeout: int = 10,
        verify: bool = False,
        token_realm: str | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.realm = realm
        self.client_id = client_id
        self.admin_user = admin_user
        self.admin_password = admin_password
        self.timeout = timeout
        self.verify = verify
        # 取 token 的 realm 与要操作的 realm 可以不同：
        # 多租户场景下用 master 管理员 token 去操作 qa-tenant-a，就靠这个参数。
        self.token_realm = token_realm or realm
        self._token: str | None = None

    # ------------------------------------------------------------------ 基础
    @property
    def admin_token(self) -> str:
        """管理员 token，懒加载并缓存。

        注意用 token_realm 而非 realm：多租户场景下管理员的凭据属于 master，
        但操作的目标是某个租户，两者不能混。
        """
        if self._token is None:
            self._token = self.get_token_response(
                self.admin_user, self.admin_password, realm=self.token_realm
            )["access_token"]
        return self._token

    def auth_headers(self, token: str | None = None) -> dict:
        """构造带 Bearer 的请求头；token 为空则使用缓存的 admin token。"""
        tok = token or self.admin_token
        return {"Authorization": f"Bearer {tok}"}

    def admin_url(self, suffix: str = "") -> str:
        """拼接当前 realm 下的 admin API 地址，例如 /users。"""
        return f"{self.base_url}/admin/realms/{self.realm}{suffix}"

    def admin_url_of(self, realm: str, suffix: str = "") -> str:
        """拼接任意 realm 的 admin API 地址（跨租户访问用例用）。"""
        return f"{self.base_url}/admin/realms/{realm}{suffix}"

    # ------------------------------------------------------------------ 认证
    def token_endpoint(self, realm: str | None = None) -> str:
        """token 端点。默认是当前操作的 realm（用户在哪个租户就去哪登录）。"""
        return f"{self.base_url}/realms/{realm or self.realm}/protocol/openid-connect/token"

    def get_token(self, username: str | None, password: str | None) -> str:
        """用用户名/密码换取 access_token。

        失败时抛 KeycloakError，消息里带上状态码与响应体，方便定位。
        """
        return self.get_token_response(username, password)["access_token"]

    def get_token_response(
        self,
        username: str | None,
        password: str | None,
        realm: str | None = None,
        scope: str | None = None,
    ) -> dict:
        """换取完整 token 响应（access_token / refresh_token / expires_in）。

        会话类用例要用 refresh_token，所以单独留一个返回全量的入口。
        realm 参数用于「租户 A 的用户去租户 B 登录」这类跨租户用例。
        scope 传 "openid" 时 token 才能调 userinfo（否则 403 insufficient_scope）。
        """
        data = {
            "client_id": self.client_id,
            "grant_type": "password",
            "username": username,
            "password": password,
        }
        if scope:
            data["scope"] = scope
        resp = requests.post(
            self.token_endpoint(realm),
            data=data,
            timeout=self.timeout,
            verify=self.verify,
        )
        if resp.status_code != 200:
            raise KeycloakError(
                f"获取 token 失败 [{resp.status_code}]: {resp.text}"
            )
        return resp.json()

    def refresh_token(self, refresh_token: str, realm: str | None = None) -> requests.Response:
        """用 refresh_token 换新 token，返回原始响应（用例自行断言）。"""
        return requests.post(
            self.token_endpoint(realm),
            data={
                "client_id": self.client_id,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            },
            timeout=self.timeout,
            verify=self.verify,
        )

    def logout(self, refresh_token: str, realm: str | None = None) -> requests.Response:
        """注销会话（revoke），使该会话的 refresh_token 失效。"""
        return requests.post(
            f"{self.base_url}/realms/{realm or self.realm}/protocol/openid-connect/logout",
            data={"client_id": self.client_id, "refresh_token": refresh_token},
            timeout=self.timeout,
            verify=self.verify,
        )

    def introspect(self, token: str, realm: str | None = None) -> dict:
        """校验 token 是否仍然有效（active 字段）。比猜 access_token 是否失效更可靠。"""
        resp = requests.post(
            f"{self.base_url}/realms/{realm or self.realm}/protocol/openid-connect/token/introspect",
            data={"client_id": self.client_id, "token": token},
            timeout=self.timeout,
            verify=self.verify,
        )
        return resp.json() if resp.status_code == 200 else {"active": False}

    # -------------------------------------------------------------- 用户管理
    def create_user(
        self,
        username: str,
        password: str | None = None,
        email: str | None = None,
        first_name: str | None = None,
        last_name: str | None = None,
        enabled: bool = True,
    ) -> str:
        """创建用户，返回新用户的 id。

        id 从响应头 Location 末尾截取（形如 .../users/{id}）。
        """
        # 该 realm 的 user profile 将 email/firstName/lastName 设为必填，
        # 缺任意一个登录时会报 "Account is not fully set up"，故这里给默认值兜底。
        payload = {
            "username": username,
            "enabled": enabled,
            "email": email or f"{username}@qa.local",
            "firstName": first_name or "QA",
            "lastName": last_name or "User",
        }
        if password:
            payload["credentials"] = [
                {"type": "password", "value": password, "temporary": False}
            ]
        resp = requests.post(
            self.admin_url("/users"),
            json=payload,
            headers=self.auth_headers(),
            timeout=self.timeout,
            verify=self.verify,
        )
        if resp.status_code != 201:
            raise KeycloakError(
                f"创建用户失败 [{resp.status_code}]: {resp.text}"
            )
        return resp.headers["Location"].rstrip("/").rsplit("/", 1)[-1]

    def get_user(self, user_id: str) -> requests.Response:
        """按 id 查询用户，返回原始 Response（用例自行断言状态码）。"""
        return requests.get(
            self.admin_url(f"/users/{user_id}"),
            headers=self.auth_headers(),
            timeout=self.timeout,
            verify=self.verify,
        )

    def find_user_by_username(self, username: str) -> str | None:
        """按用户名精确查找，返回 id 或 None。"""
        resp = requests.get(
            self.admin_url("/users"),
            params={"username": username, "exact": "true"},
            headers=self.auth_headers(),
            timeout=self.timeout,
            verify=self.verify,
        )
        resp.raise_for_status()
        users = resp.json()
        return users[0]["id"] if users else None

    def update_user(self, user_id: str, **fields) -> requests.Response:
        """局部更新用户字段（如 firstName / lastName / email / enabled）。"""
        return requests.put(
            self.admin_url(f"/users/{user_id}"),
            json=fields,
            headers=self.auth_headers(),
            timeout=self.timeout,
            verify=self.verify,
        )

    def delete_user(self, user_id: str) -> requests.Response:
        return requests.delete(
            self.admin_url(f"/users/{user_id}"),
            headers=self.auth_headers(),
            timeout=self.timeout,
            verify=self.verify,
        )

    def list_users(self) -> list[dict]:
        resp = requests.get(
            self.admin_url("/users"),
            headers=self.auth_headers(),
            timeout=self.timeout,
            verify=self.verify,
        )
        resp.raise_for_status()
        return resp.json()

    # -------------------------------------------------------------- 角色管理
    def get_realm_role(self, role_name: str) -> requests.Response:
        """按名称取 realm 角色；不存在返回 404。"""
        return requests.get(
            self.admin_url(f"/roles/{role_name}"),
            headers=self.auth_headers(),
            timeout=self.timeout,
            verify=self.verify,
        )

    def create_realm_role(self, name: str, description: str = "") -> requests.Response:
        """创建 realm 角色，成功 201，重名 409。"""
        return requests.post(
            self.admin_url("/roles"),
            json={"name": name, "description": description},
            headers=self.auth_headers(),
            timeout=self.timeout,
            verify=self.verify,
        )

    def get_user_realm_roles(self, user_id: str) -> list[dict]:
        resp = requests.get(
            self.admin_url(f"/users/{user_id}/role-mappings/realm"),
            headers=self.auth_headers(),
            timeout=self.timeout,
            verify=self.verify,
        )
        resp.raise_for_status()
        return resp.json()

    def assign_realm_roles(self, user_id: str, roles: list[dict]) -> requests.Response:
        """给用户分配 realm 角色（roles 是 role 表示的数组，不是字符串）。"""
        return requests.post(
            self.admin_url(f"/users/{user_id}/role-mappings/realm"),
            json=roles,
            headers=self.auth_headers(),
            timeout=self.timeout,
            verify=self.verify,
        )

    def remove_realm_roles(self, user_id: str, roles: list[dict]) -> requests.Response:
        return requests.delete(
            self.admin_url(f"/users/{user_id}/role-mappings/realm"),
            json=roles,
            headers=self.auth_headers(),
            timeout=self.timeout,
            verify=self.verify,
        )

    def get_client_by_client_id(self, client_id: str) -> dict | None:
        """按 clientId 取 client（如 realm-management / admin-cli）。"""
        resp = requests.get(
            self.admin_url("/clients"),
            params={"clientId": client_id},
            headers=self.auth_headers(),
            timeout=self.timeout,
            verify=self.verify,
        )
        resp.raise_for_status()
        clients = resp.json()
        return clients[0] if clients else None

    def get_client_role(self, client_uuid: str, role_name: str) -> dict:
        resp = requests.get(
            self.admin_url(f"/clients/{client_uuid}/roles/{role_name}"),
            headers=self.auth_headers(),
            timeout=self.timeout,
            verify=self.verify,
        )
        resp.raise_for_status()
        return resp.json()

    def assign_client_roles(
        self, user_id: str, client_uuid: str, roles: list[dict]
    ) -> requests.Response:
        return requests.post(
            self.admin_url(f"/users/{user_id}/role-mappings/clients/{client_uuid}"),
            json=roles,
            headers=self.auth_headers(),
            timeout=self.timeout,
            verify=self.verify,
        )

    def make_realm_admin(self, user_id: str) -> None:
        """把租户里的普通用户提升为「租户管理员」。

        租户管理员 = realm-management 客户端里的 realm-admin 角色，
        只能管自己 realm，不能建 realm（实测 403）——正是多租户要求的权限边界。
        """
        client = self.get_client_by_client_id("realm-management")
        if not client:
            raise KeycloakError("该 realm 缺少 realm-management 客户端")
        role = self.get_client_role(client["id"], "realm-admin")
        resp = self.assign_client_roles(user_id, client["id"], [role])
        if resp.status_code != 204:
            raise KeycloakError(
                f"提升租户管理员失败 [{resp.status_code}]：{resp.text[:200]}"
            )

    def revoke_realm_admin(self, user_id: str) -> requests.Response:
        """收回租户管理员权限（撤销 realm-management 的 realm-admin）。"""
        client = self.get_client_by_client_id("realm-management")
        role = self.get_client_role(client["id"], "realm-admin")
        return self.remove_client_roles(user_id, client["id"], [role])

    def assign_composites(self, role: dict, composites: list[dict]) -> requests.Response:
        """把 composites 并入 role，形成 composite 角色（继承关系）。"""
        return requests.post(
            self.admin_url(f"/roles/{role['name']}/composites"),
            json=composites,
            headers=self.auth_headers(),
            timeout=self.timeout,
            verify=self.verify,
        )

    def get_effective_realm_roles(self, user_id: str) -> list[dict]:
        """用户的「有效」realm 角色，已展开 composite 继承。

        为什么不用 token claims 断言：
        student-qa 的 user profile 被手工调过，access token 只含极简 claims
        （连 sub 都没有），roles scope 的 mapper 实际没写进 token。
        用这个接口验证角色生效更可靠，也不受 realm 配置漂移影响。
        """
        resp = requests.get(
            self.admin_url(f"/users/{user_id}/role-mappings/realm/composite"),
            headers=self.auth_headers(),
            timeout=self.timeout,
            verify=self.verify,
        )
        resp.raise_for_status()
        return resp.json()

    def get_role_mappings(self, user_id: str) -> dict:
        """一次拿到 realmMappings（realm 角色）与 clientMappings（client 角色）。"""
        resp = requests.get(
            self.admin_url(f"/users/{user_id}/role-mappings"),
            headers=self.auth_headers(),
            timeout=self.timeout,
            verify=self.verify,
        )
        resp.raise_for_status()
        return resp.json()

    def remove_client_roles(
        self, user_id: str, client_uuid: str, roles: list[dict]
    ) -> requests.Response:
        return requests.delete(
            self.admin_url(f"/users/{user_id}/role-mappings/clients/{client_uuid}"),
            json=roles,
            headers=self.auth_headers(),
            timeout=self.timeout,
            verify=self.verify,
        )

    def reset_password(
        self, user_id: str, password: str, temporary: bool = False
    ) -> requests.Response:
        """管理员重置用户密码（不经过旧密码）。"""
        return requests.put(
            self.admin_url(f"/users/{user_id}/reset-password"),
            json={"type": "password", "value": password, "temporary": temporary},
            headers=self.auth_headers(),
            timeout=self.timeout,
            verify=self.verify,
        )

    def delete_realm_role(self, role_name: str) -> requests.Response:
        return requests.delete(
            self.admin_url(f"/roles/{role_name}"),
            headers=self.auth_headers(),
            timeout=self.timeout,
            verify=self.verify,
        )

    def create_user_raw(
        self,
        payload: dict | None = None,
        raw_body: str | None = None,
        content_type: str = "application/json",
    ) -> requests.Response:
        """原样提交创建用户请求，返回原始 Response。

        输入契约用例要断言 400/415 这类状态码，而 create_user 只接受 201 否则抛异常，
        所以单独留一个不校验结果的入口。
        """
        body = raw_body if raw_body is not None else json.dumps(payload or {})
        return requests.post(
            self.admin_url("/users"),
            data=body,
            headers={**self.auth_headers(), "Content-Type": content_type},
            timeout=self.timeout,
            verify=self.verify,
        )
