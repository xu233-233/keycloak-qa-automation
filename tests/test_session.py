"""P8 扩展：会话与令牌生命周期用例。

补的是「登录之后」这一段链路：refresh 续期、logout 撤销、禁用用户立即失效。
原 P2/P3 只验证了「拿到 token 后的权限边界」，没验证「token 本身的生命周期」，
而实际线上问题很多出在这里（登出不彻底、禁用账号仍能操作）。
"""
import pytest
import requests

from keycloak_client import KeycloakError

PASSWORD = "Passw0rd!"


class TestTokenLifecycle:
    def test_refresh_token_issues_new_access_token(self, kc, unique_name):
        """refresh_token 能换出新 access_token，且新 token 确实可用。"""
        uid = kc.create_user(unique_name, password=PASSWORD)
        try:
            # 带 openid scope，贴近真实前端登录（浏览器端一定会带 openid）
            first = kc.get_token_response(unique_name, PASSWORD, scope="openid")
            resp = kc.refresh_token(first["refresh_token"])
            assert resp.status_code == 200, resp.text
            second = resp.json()
            assert second["access_token"] != first["access_token"]
            assert second["expires_in"] > 0

            # 用新 token 打 admin API：403 = token 有效但没权限；401 才是 token 失效。
            # （不用 userinfo：admin-cli 换来的 token 打 userinfo 恒为 401，是被测系统对该
            #   client 的限制，与续期是否成功无关，用它断言会误判）
            me = requests.get(
                kc.admin_url("/users"),
                headers={"Authorization": f"Bearer {second['access_token']}"},
                timeout=10,
            )
            assert me.status_code == 403, me.text
        finally:
            kc.delete_user(uid)

    def test_logout_revokes_refresh_token(self, kc, unique_name):
        """注销后原 refresh_token 失效，不能继续续期（400 invalid_grant）。"""
        uid = kc.create_user(unique_name, password=PASSWORD)
        try:
            data = kc.get_token_response(unique_name, PASSWORD)
            rt = data["refresh_token"]
            assert kc.logout(rt).status_code == 204
            resp = kc.refresh_token(rt)
            assert resp.status_code == 400, resp.text
            assert "invalid_grant" in resp.text
        finally:
            kc.delete_user(uid)

    def test_wrong_password_rejected(self, kc, unique_name):
        """密码错误无法换 token。"""
        uid = kc.create_user(unique_name, password=PASSWORD)
        try:
            with pytest.raises(KeycloakError):
                kc.get_token(unique_name, "wrong-password")
        finally:
            kc.delete_user(uid)

    def test_disabled_user_cannot_login(self, kc, unique_name):
        """禁用用户后立即无法登录，即使密码正确。"""
        uid = kc.create_user(unique_name, password=PASSWORD)
        try:
            assert kc.get_token(unique_name, PASSWORD)
            kc.update_user(uid, enabled=False)
            with pytest.raises(KeycloakError):
                kc.get_token(unique_name, PASSWORD)
        finally:
            kc.delete_user(uid)
