"""Keycloak 管理台页面对象（P4）。

选择器来源：直接扒自 Keycloak 26.8.0 前端 bundle 的 data-testid 与表单 name，
不是猜的：
- 登录页：标准 Keycloak 登录主题 #username / #password / #kc-login
- 用户列表：data-testid="add-user"
- 建用户表单：input[name=username|email|firstName|lastName]，提交按钮 Create
- 用户角色映射：add-role-mapping-button / roles-role / table-search-input / assign
- 建 realm：currentRealm -> add-realm -> #realm-form input[name=realm] -> create

管理台是 hash 路由（createHashRouter），路由形如：
    /admin/master/console/#/{realm}/users
    /admin/master/console/#/{realm}/users/add-user
    /admin/master/console/#/{realm}/users/{id}/role-mapping
"""
from __future__ import annotations

from playwright.sync_api import Page


class AdminConsole:
    def __init__(self, page: Page, base_url: str):
        self.page = page
        self.base_url = base_url.rstrip("/")
        self.console_url = f"{self.base_url}/admin/master/console/"

    # ---------------------------------------------------------------- 登录
    def login(self, username: str, password: str, timeout: int = 30_000) -> None:
        """打开管理台并用指定账号登录，成功后等到顶部 realm 选择器出现。"""
        page = self.page
        page.goto(self.console_url, wait_until="domcontentloaded")
        # SPA 会跳转到 Keycloak 标准登录页
        page.wait_for_selector("#username", timeout=timeout)
        page.fill("#username", username)
        page.fill("#password", password)
        page.click("#kc-login")
        # 回到管理台（顶部 realm 选择器是登录成功的标志）
        page.wait_for_selector('[data-testid="currentRealm"]', timeout=timeout)

    # ------------------------------------------------------------ 路由跳转
    def goto(self, hash_path: str) -> None:
        """跳转到管理台内的 hash 路由，如 '/student-qa/users'。

        关键坑：只改 hash 不会切换应用内部的 realm 上下文（实测仍停在 master），
        必须再 reload 一次，让 SPA 按 URL 重新初始化 realm。
        """
        self.page.goto(f"{self.console_url}#{hash_path}", wait_until="domcontentloaded")
        self.page.reload(wait_until="domcontentloaded")

    # ------------------------------------------------------------ 建用户
    def create_user(
        self,
        realm: str,
        username: str,
        email: str,
        first_name: str = "UI",
        last_name: str = "Tester",
    ) -> None:
        """在指定 realm 下通过 UI 创建用户。

        注意：该 realm 的 user profile 把 email/firstName/lastName 设为必填，
        所以这四个字段都要填，否则创建后登录会报 "Account is not fully set up"。
        """
        page = self.page
        self.goto(f"/{realm}/users")
        page.get_by_test_id("add-user").click()
        page.wait_for_selector('input[name="username"]', timeout=15_000)
        page.fill('input[name="username"]', username)
        page.fill('input[name="email"]', email)
        page.fill('input[name="firstName"]', first_name)
        page.fill('input[name="lastName"]', last_name)
        page.get_by_role("button", name="Create", exact=True).first.click()

    # ---------------------------------------------------------- 配角色
    def assign_realm_role(self, realm: str, user_id: str, role_name: str) -> None:
        """给用户分配一个 realm 角色（走「Role mapping -> Assign role」流程）。

        面板不是 role=dialog，是一块内联区域；默认停在 Client roles，
        必须先点 roles-role 切到 Realm roles，再按角色名定位行勾选。
        """
        page = self.page
        self.goto(f"/{realm}/users/{user_id}/role-mapping")
        page.get_by_test_id("add-role-mapping-button").click()
        page.get_by_test_id("roles-role").click()

        # 面板内的搜索框（placeholder 区分于页面上的表格搜索）
        page.locator('input[placeholder="Search by role name"]').fill(role_name)
        page.wait_for_timeout(300)

        # 按角色名定位那一行，勾选行内 checkbox
        row = page.locator("tr", has_text=role_name).first
        row.locator('input[type="checkbox"]').check()
        page.get_by_test_id("assign").click()

    # ---------------------------------------------------------- 建 realm
    def create_realm(self, realm_name: str) -> None:
        """在「Manage realms」页创建新 realm。"""
        page = self.page
        self.goto("/master/realms")
        page.get_by_test_id("add-realm").click()
        page.wait_for_selector('input[name="realm"]', timeout=15_000)
        page.fill('input[name="realm"]', realm_name)
        # 弹窗内的 Create 按钮
        page.get_by_test_id("create").click()
