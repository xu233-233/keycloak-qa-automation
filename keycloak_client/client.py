"""Keycloak Admin REST API 封装（P2 核心，P3 复用）。

覆盖能力：
- 认证：管理员账号 / 任意普通用户换取 access_token（Resource Owner Password 流程）
- 用户生命周期：创建 / 查询 / 更新 / 删除 / 列表
- 原始 admin 请求：供 P3 安全用例直接发请求（无 token / 篡改 token / 跨 realm）

被测系统：docker-compose 起的 Keycloak 26（start-dev，默认 8080 端口）。
"""
from __future__ import annotations

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
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.realm = realm
        self.client_id = client_id
        self.admin_user = admin_user
        self.admin_password = admin_password
        self.timeout = timeout
        self.verify = verify
        self._token: str | None = None

    # ------------------------------------------------------------------ 基础
    @property
    def admin_token(self) -> str:
        """管理员 token，懒加载并缓存。"""
        if self._token is None:
            self._token = self.get_token(self.admin_user, self.admin_password)
        return self._token

    def auth_headers(self, token: str | None = None) -> dict:
        """构造带 Bearer 的请求头；token 为空则使用缓存的 admin token。"""
        tok = token or self.admin_token
        return {"Authorization": f"Bearer {tok}"}

    def admin_url(self, suffix: str = "") -> str:
        """拼接当前 realm 下的 admin API 地址，例如 /users。"""
        return f"{self.base_url}/admin/realms/{self.realm}{suffix}"

    # ------------------------------------------------------------------ 认证
    def get_token(self, username: str | None, password: str | None) -> str:
        """用用户名/密码换取 access_token。

        失败时抛 KeycloakError，消息里带上状态码与响应体，方便定位。
        """
        url = f"{self.base_url}/realms/{self.realm}/protocol/openid-connect/token"
        data = {
            "client_id": self.client_id,
            "grant_type": "password",
            "username": username,
            "password": password,
        }
        resp = requests.post(url, data=data, timeout=self.timeout, verify=self.verify)
        if resp.status_code != 200:
            raise KeycloakError(
                f"获取 token 失败 [{resp.status_code}]: {resp.text}"
            )
        return resp.json()["access_token"]

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
