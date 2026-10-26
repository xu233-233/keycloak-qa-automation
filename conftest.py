"""pytest 全局配置与 fixture。

被测环境默认值对齐 docker-compose.yml：
- Keycloak: http://localhost:8080
- Realm:    student-qa
- 管理员:   qa-admin / qa-admin-123（P1 手工创建）

如果你的密码不是默认值，用环境变量覆盖即可，例如（Git Bash）：
    export KC_ADMIN_PASSWORD='你的密码'
    pytest
"""
import os
import uuid

import pytest

from keycloak_client import KeycloakClient, PlatformAdmin

BASE_URL = os.getenv("KC_BASE_URL", "http://localhost:8080")
REALM = os.getenv("KC_REALM", "student-qa")
ADMIN_USER = os.getenv("KC_ADMIN_USER", "qa-admin")
ADMIN_PASSWORD = os.getenv("KC_ADMIN_PASSWORD", "qa-admin-123")
# 平台管理员：master realm 的 admin，建/停/删租户要用它
MASTER_ADMIN_USER = os.getenv("KC_MASTER_USER", "admin")
MASTER_ADMIN_PASSWORD = os.getenv("KC_MASTER_PASSWORD", "admin")

# 两个测试租户（realm 即租户边界）
TENANT_A = "qa-tenant-a"
TENANT_B = "qa-tenant-b"


@pytest.fixture(scope="session")
def kc() -> KeycloakClient:
    """会话级 Keycloak 客户端（复用同一个 admin token）。"""
    return KeycloakClient(
        base_url=BASE_URL,
        realm=REALM,
        admin_user=ADMIN_USER,
        admin_password=ADMIN_PASSWORD,
    )


@pytest.fixture()
def unique_name() -> str:
    """每个用例一个唯一用户名，避免脏数据互相干扰。"""
    return f"qa-{uuid.uuid4().hex[:8]}"


# ---------------------------------------------------------------- 多租户支持
@pytest.fixture(scope="session")
def platform() -> PlatformAdmin:
    """平台管理员（master），只用来开/停/删租户。"""
    return PlatformAdmin(BASE_URL, MASTER_ADMIN_USER, MASTER_ADMIN_PASSWORD)


@pytest.fixture(scope="session")
def tenants(platform):
    """建好两个租户，会话结束统一销户。

    幂等：ensure_realm 会先删残留再重建，避免上一轮失败留下的脏租户影响断言。
    只有用到租户的用例才会触发，不影响原有的单租户用例。
    """
    names = [TENANT_A, TENANT_B]
    for name in names:
        platform.ensure_realm(name)
    yield {"a": TENANT_A, "b": TENANT_B}
    for name in names:
        platform.delete_realm(name)


@pytest.fixture(scope="session")
def kc_of(tenants):
    """按租户名取客户端：token 取自 master 管理员，但操作的是目标租户。

    这样用例可以「以平台身份」在各租户里造数据，不必每个租户单独建管理员。
    """
    cache: dict[str, KeycloakClient] = {}

    def _get(realm: str) -> KeycloakClient:
        if realm not in cache:
            cache[realm] = KeycloakClient(
                base_url=BASE_URL,
                realm=realm,
                token_realm="master",
                admin_user=MASTER_ADMIN_USER,
                admin_password=MASTER_ADMIN_PASSWORD,
            )
        return cache[realm]

    return _get
