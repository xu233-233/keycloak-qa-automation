"""平台侧管理（多租户扩展）：realm 的开工 / 停用 / 销户。

为什么单独一个类：
    建 realm 是 **master 级** 操作，只有 master 管理员（或持有 create-realm 角色的用户）
    才能做。实测 student-qa 的 qa-admin 调 POST /admin/realms 返回 403 —— 这正好对应
    多租户 SaaS 里「平台方开租户、租户管理员只管自己那亩地」的权限模型。

被测系统：docker-compose 起的 Keycloak 26（start-dev，默认 8080 端口）。
"""
from __future__ import annotations

import requests


class PlatformAdmin:
    """master realm 平台管理员，负责租户（realm）生命周期。

    典型用法::

        p = PlatformAdmin("http://localhost:8080", "admin", "admin")
        p.create_realm("qa-tenant-a")
        p.set_realm_enabled("qa-tenant-a", False)   # 停用租户
        p.delete_realm("qa-tenant-a")
    """

    def __init__(
        self,
        base_url: str,
        admin_user: str = "admin",
        admin_password: str = "admin",
        client_id: str = "admin-cli",
        timeout: int = 20,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.admin_user = admin_user
        self.admin_password = admin_password
        self.client_id = client_id
        self.timeout = timeout
        self._token: str | None = None

    # ------------------------------------------------------------------ 基础
    @property
    def token(self) -> str:
        """master 管理员 token，懒加载并缓存。"""
        if self._token is None:
            resp = requests.post(
                f"{self.base_url}/realms/master/protocol/openid-connect/token",
                data={
                    "client_id": self.client_id,
                    "grant_type": "password",
                    "username": self.admin_user,
                    "password": self.admin_password,
                },
                timeout=self.timeout,
            )
            if resp.status_code != 200:
                raise RuntimeError(
                    f"master 管理员登录失败 [{resp.status_code}]：{resp.text[:200]}"
                )
            self._token = resp.json()["access_token"]
        return self._token

    @property
    def headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}"}

    def reset_token(self) -> None:
        """丢弃缓存 token（用例里模拟登录态变化时用）。"""
        self._token = None

    # -------------------------------------------------------------- 租户管理
    def list_realms(self) -> list[dict]:
        resp = requests.get(
            f"{self.base_url}/admin/realms", headers=self.headers, timeout=self.timeout
        )
        resp.raise_for_status()
        return resp.json()

    def get_realm(self, realm: str) -> requests.Response:
        return requests.get(
            f"{self.base_url}/admin/realms/{realm}",
            headers=self.headers,
            timeout=self.timeout,
        )

    def create_realm(self, realm: str, enabled: bool = True) -> requests.Response:
        """开户。成功 201，重名 409（用例自行断言状态码）。"""
        return requests.post(
            f"{self.base_url}/admin/realms",
            json={"realm": realm, "enabled": enabled},
            headers=self.headers,
            timeout=self.timeout,
        )

    def set_realm_enabled(self, realm: str, enabled: bool) -> requests.Response:
        """停用 / 恢复租户。停用后该租户的用户无法换 token。"""
        return self.update_realm(realm, enabled=enabled)

    def update_realm(self, realm: str, **fields) -> requests.Response:
        """局部更新 realm 配置（PUT 是 merge 语义，只改传入的字段）。

        常用于时间窗口类用例：把 accessTokenLifespan 压到几秒，
        或设 notBefore 撤销该 realm 已签发的令牌。
        改动会立刻生效且无需重启，所以实验请放在临时 realm 里做，
        不要动 student-qa —— 否则其它用例的 token 会提前过期。
        """
        return requests.put(
            f"{self.base_url}/admin/realms/{realm}",
            json=fields,
            headers=self.headers,
            timeout=self.timeout,
        )

    def delete_realm(self, realm: str) -> requests.Response:
        """销户。会连同该租户下的用户、角色、会话一起删除。"""
        return requests.delete(
            f"{self.base_url}/admin/realms/{realm}",
            headers=self.headers,
            timeout=self.timeout,
        )

    def ensure_realm(self, realm: str) -> None:
        """幂等开户：已存在就先删掉重建，避免上一轮残留影响断言。"""
        if self.get_realm(realm).status_code == 200:
            self.delete_realm(realm)
        resp = self.create_realm(realm)
        if resp.status_code != 201:
            raise RuntimeError(f"创建租户 {realm} 失败 [{resp.status_code}]：{resp.text[:200]}")
