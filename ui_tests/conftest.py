"""P4：Playwright UI 用例的 fixture 与配置。

运行方式（与 P2/P3 的接口用例分开，避免默认 pytest 需要浏览器）：
    pytest ui_tests -v            # 无头
    pytest ui_tests -v --headed   # 带界面，便于观察

前置（手动，只需一次）：
    pip install -r requirements.txt
    playwright install chromium
"""
import os

import pytest

from keycloak_client import KeycloakClient

BASE_URL = os.getenv("KC_BASE_URL", "http://localhost:8080")
REALM = os.getenv("KC_REALM", "student-qa")
# master 管理员：建 realm 等全局操作需要它（docker-compose 里的 admin/admin）
MASTER_ADMIN_USER = os.getenv("KC_MASTER_USER", "admin")
MASTER_ADMIN_PASSWORD = os.getenv("KC_MASTER_PASSWORD", "admin")


@pytest.fixture(scope="session")
def browser_context_args(browser_context_args):
    """强制英文 locale（避免控制台中文文案让定位失效）+ 固定视口。"""
    return {
        **browser_context_args,
        "locale": "en-US",
        "viewport": {"width": 1440, "height": 900},
    }


@pytest.fixture(scope="session")
def base_url() -> str:
    return BASE_URL


@pytest.fixture(scope="session")
def realm() -> str:
    return REALM


@pytest.fixture(scope="session")
def master_credentials() -> tuple[str, str]:
    return MASTER_ADMIN_USER, MASTER_ADMIN_PASSWORD


@pytest.fixture(scope="session")
def kc_master() -> KeycloakClient:
    """master realm 的管理客户端，用于建/删 realm、校验。"""
    return KeycloakClient(
        BASE_URL,
        "master",
        admin_user=MASTER_ADMIN_USER,
        admin_password=MASTER_ADMIN_PASSWORD,
    )
