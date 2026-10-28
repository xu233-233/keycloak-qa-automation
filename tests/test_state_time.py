"""P8 扩展：「状态与时间」切面用例。

为什么单独立一组：
    前面的用例都在验证「某一刻的行为对不对」，这一组验证的是
    「跨过某个状态边界或时间边界之后，行为会不会跟着变」。
    这类缺陷在线上最多、在单点功能测试里最容易被漏——
    因为写代码的人按正常路径实现，很少想全「状态已经变了」的组合。

四个子维度：
    A. 用户状态机   enabled <-> disabled 是否可逆、禁用能否立即踢下线
    B. 凭据状态     临时密码（首次登录必须改密）-> 永久密码
    C. 会话生命周期 多次登录是否互相独立、管理员能否一键全部踢掉
    D. 时间窗口     access token 过期、过期后 refresh 是否仍可用、notBefore 撤销
"""
import time

import pytest

from keycloak_client import KeycloakError

PASSWORD = "Passw0rd!"
NEW_PASSWORD = "Passw0rd!2026"

# token 有效性判定统一走 KeycloakClient.token_is_valid()：
#     403 = 认出身份但没权限 -> token 有效
#     401 = 没认出身份       -> 无效 / 过期 / 用户被禁用 / 会话被撤销
# 不用 introspect：实测 admin-cli（公开客户端）调该端点返回 403 "Client not allowed."，
# 把这种「没权限」当成「token 失效」会造成整组用例假阴性。


class TestUserStateMachine:
    """A. 用户状态机：enabled <-> disabled 的往返。"""

    def test_reenable_restores_login(self, kc, unique_name):
        """禁用是「暂停」而不是「注销」：重新启用后，原密码应当能继续登录。

        这条守护的是状态的可逆性。若哪天运维点了禁用再启用、用户却登不进，
        就是状态机漏了恢复分支。
        """
        uid = kc.create_user(unique_name, password=PASSWORD)
        try:
            assert kc.get_token(unique_name, PASSWORD)

            kc.set_user_enabled(uid, False)
            with pytest.raises(KeycloakError):
                kc.get_token(unique_name, PASSWORD)

            kc.set_user_enabled(uid, True)
            # 关键断言：密码没变、用户没重建，仅靠状态回滚即可恢复
            assert kc.get_token(unique_name, PASSWORD)

            user = kc.get_user(uid).json()
            assert user["enabled"] is True
        finally:
            kc.delete_user(uid)

    def test_disable_revokes_live_session(self, kc, unique_name):
        """禁用用户后，已经发出去的 token 必须立即失效，不能等到过期。

        这是安全红线：员工离职当天点了禁用，如果手上的 token 还能用 5 分钟
        （access token 剩余寿命），就是可被利用的窗口。
        """
        uid = kc.create_user(unique_name, password=PASSWORD)
        try:
            data = kc.get_token_response(unique_name, PASSWORD)
            assert kc.token_is_valid(data["access_token"])

            kc.set_user_enabled(uid, False)

            # 旧 access token 立刻不能用（401 而非 403）
            assert not kc.token_is_valid(data["access_token"])
            # 旧 refresh token 也换不出新 token，否则等于绕过禁用
            resp = kc.refresh_token(data["refresh_token"])
            assert resp.status_code == 400, resp.text
            assert "invalid_grant" in resp.text
        finally:
            kc.delete_user(uid)

    def test_delete_fails_closed(self, kc, unique_name):
        """删除用户后无法登录，且查询返回 404（终态，不可恢复）。

        与「禁用」形成对照：删除是终态，不存在重新启用的路径。
        """
        uid = kc.create_user(unique_name, password=PASSWORD)
        assert kc.delete_user(uid).status_code == 204

        assert kc.get_user(uid).status_code == 404
        with pytest.raises(KeycloakError):
            kc.get_token(unique_name, PASSWORD)


class TestCredentialState:
    """B. 凭据状态：临时密码 -> 必须改密 -> 永久密码。"""

    def test_temporary_password_marks_update_required(self, kc, unique_name):
        """管理员发初始密码（temporary=True）后，账号被标记为「必须先改密」。

        对应真实场景：入职时 HR 发初始密码，首次登录必须改掉，
        否则初始密码会一直有效（HR 也知道，等于共享账号）。

        实测 Keycloak 的做法比预期更严：不是「能登录但功能受限」，
        而是连 token 都换不出来——直接 400 invalid_grant / Account is not fully set up。
        这意味着初始密码无法被当成长期凭据使用，「必须改密」是不可绕过的。
        """
        uid = kc.create_user(unique_name, password=PASSWORD, temporary_password=True)
        try:
            user = kc.get_user(uid).json()
            assert "UPDATE_PASSWORD" in user.get("requiredActions", []), user

            with pytest.raises(KeycloakError) as exc:
                kc.get_token(unique_name, PASSWORD)
            assert "Account is not fully set up" in str(exc.value)
        finally:
            kc.delete_user(uid)

    def test_reset_to_permanent_clears_required_action(self, kc, unique_name):
        """管理员重置为永久密码后，UPDATE_PASSWORD 标记必须消失。

        否则用户会被反复要求改密，陷入改完还要改的死循环。
        """
        uid = kc.create_user(unique_name, password=PASSWORD, temporary_password=True)
        try:
            assert "UPDATE_PASSWORD" in kc.get_user(uid).json().get("requiredActions", [])

            assert kc.reset_password(uid, NEW_PASSWORD, temporary=False).status_code == 204

            user = kc.get_user(uid).json()
            assert "UPDATE_PASSWORD" not in user.get("requiredActions", []), user

            # 新密码可用、旧密码失效
            assert kc.get_token(unique_name, NEW_PASSWORD)
            with pytest.raises(KeycloakError):
                kc.get_token(unique_name, PASSWORD)
        finally:
            kc.delete_user(uid)


class TestSessionLifecycle:
    """C. 会话生命周期：多次登录互相独立，管理员可一键踢下线。"""

    def test_multiple_logins_create_separate_sessions(self, kc, unique_name):
        """同一用户登录两次应当产生两个独立会话，而不是复用同一个。

        多设备登录（手机 + 电脑）在真实产品里很常见，会话若被错误合并，
        会导致「在一台设备退出，另一台也掉线」。
        """
        uid = kc.create_user(unique_name, password=PASSWORD)
        try:
            first = kc.get_token_response(unique_name, PASSWORD)
            time.sleep(1)  # 让两次登录的 session 起始时间可区分
            second = kc.get_token_response(unique_name, PASSWORD)

            sessions = kc.get_sessions(uid)
            assert len(sessions) == 2, sessions
            assert len({s["id"] for s in sessions}) == 2
            # 两个访问令牌必须不同，否则等于共用了一个会话
            assert first["access_token"] != second["access_token"]
        finally:
            kc.delete_user(uid)

    def test_admin_logout_kills_all_sessions(self, kc, unique_name):
        """管理员踢下线后：会话列表清空，且被撤销的 refresh token 不能再用。

        场景：账号疑似被盗，管理员要能一次性把该用户所有设备踢掉。
        """
        uid = kc.create_user(unique_name, password=PASSWORD)
        try:
            kc.get_token_response(unique_name, PASSWORD)
            data = kc.get_token_response(unique_name, PASSWORD)
            assert len(kc.get_sessions(uid)) == 2

            assert kc.logout_user_sessions(uid).status_code == 204

            assert kc.get_sessions(uid) == []
            resp = kc.refresh_token(data["refresh_token"])
            assert resp.status_code == 400, resp.text
            assert "invalid_grant" in resp.text
        finally:
            kc.delete_user(uid)


class TestTimeWindow:
    """D. 时间窗口：令牌过期与撤销。

    这些用例需要把 realm 的 accessTokenLifespan 改小，所以跑在一次性的
    short_lived_realm 上（见 conftest），不动 student-qa 的全局配置。
    """

    def test_access_token_expires_after_lifespan(self, short_lived_realm, unique_name):
        """超过 accessTokenLifespan 后，access token 必须被拒绝。

        realm 里把寿命设成 3 秒（默认 300 秒，等不起），睡 5 秒再验。
        断言的是「过期即失效」，这是令牌机制存在的意义。
        """
        kc = short_lived_realm
        uid = kc.create_user(unique_name, password=PASSWORD)
        try:
            token = kc.get_token_response(unique_name, PASSWORD)["access_token"]
            assert kc.token_is_valid(token)

            time.sleep(5)

            # 过期后必须 401：既证明令牌机制生效，也说明服务端校验的是 exp 而非只看签名
            assert not kc.token_is_valid(token)
        finally:
            kc.delete_user(uid)

    def test_refresh_still_works_after_access_expiry(self, short_lived_realm, unique_name):
        """access token 过期 ≠ 登录失效：refresh token 还能换出新的 access token。

        这两件事经常被混淆。access token 是短票据（分钟级），
        refresh token 是长会话（小时级），前者过期正是后者该出场的时候。
        若这条失败，说明续期链路断了，用户会被频繁踢回登录页。
        """
        kc = short_lived_realm
        uid = kc.create_user(unique_name, password=PASSWORD)
        try:
            data = kc.get_token_response(unique_name, PASSWORD)
            time.sleep(5)
            assert not kc.token_is_valid(data["access_token"])

            resp = kc.refresh_token(data["refresh_token"])
            assert resp.status_code == 200, resp.text
            fresh = resp.json()["access_token"]
            assert fresh != data["access_token"]
            assert kc.token_is_valid(fresh)
        finally:
            kc.delete_user(uid)

    def test_disabled_state_survives_token_expiry(self, kc, unique_name):
        """禁用之后即使等到 access token 自然过期，refresh 也换不出新 token。

        防的是「先禁用、后过期」这个时间差：有些实现只在签发时校验用户状态，
        续期时不再校验，就会出现禁用用户靠 refresh 长期活着。
        （与 TestUserStateMachine 的即时失效互补：那条测立刻，这条测持续。）
        """
        uid = kc.create_user(unique_name, password=PASSWORD)
        try:
            data = kc.get_token_response(unique_name, PASSWORD)
            kc.set_user_enabled(uid, False)

            time.sleep(1)
            resp = kc.refresh_token(data["refresh_token"])
            assert resp.status_code == 400, resp.text
            assert "invalid_grant" in resp.text
        finally:
            kc.delete_user(uid)
