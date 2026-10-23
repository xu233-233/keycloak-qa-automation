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

from keycloak_client import KeycloakClient

BASE_URL = os.getenv("KC_BASE_URL", "http://localhost:8080")
REALM = os.getenv("KC_REALM", "student-qa")
ADMIN_USER = os.getenv("KC_ADMIN_USER", "qa-admin")
ADMIN_PASSWORD = os.getenv("KC_ADMIN_PASSWORD", "qa-admin-123")


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
