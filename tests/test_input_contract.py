"""P8 扩展：输入契约用例（创建用户接口）。

思路：把「字段级校验」当成独立切面，逐条验证服务端会不会拒绝脏数据。
每条断言不仅看状态码，还看错误标识（errorMessage / field），
否则「400」可能是别的原因造成的，属于假绿。

实测结论（Keycloak 26，student-qa）：
| 输入                    | 状态码 | 错误标识                              |
|-------------------------|--------|---------------------------------------|
| 空 body                 | 500    | unknown_error  ← **期望 400，已上报**  |
| 非法 JSON               | 400    | Cannot parse the JSON                 |
| Content-Type: text/plain| 415    | content-type header did not match     |
| 空 / 缺 username        | 400    | User name is missing                  |
| 非法 email              | 400    | error-invalid-email                   |
| username 长度 256       | 400    | error-invalid-length [3, 255]         |
| username 含非法字符     | 400    | error-username-invalid-character      |
"""
import uuid

import pytest

PASSWORD = "Passw0rd!"


def _payload(**overrides) -> dict:
    base = {
        "username": f"qa-{uuid.uuid4().hex[:8]}",
        "enabled": True,
        "email": "qa@qa.local",
        "firstName": "Q",
        "lastName": "U",
    }
    base.update(overrides)
    return base


def _cleanup(kc, resp):
    """创建成功时把用户删掉，避免脏数据。"""
    if resp.status_code == 201:
        uid = resp.headers["Location"].rstrip("/").rsplit("/", 1)[-1]
        kc.delete_user(uid)


class TestUserCreationContract:
    def test_missing_username_rejected(self, kc):
        """缺 username → 400，且报错指向 username。"""
        resp = kc.create_user_raw({"enabled": True, "email": "qa@qa.local"})
        assert resp.status_code == 400, resp.text
        assert "User name is missing" in resp.text

    def test_empty_username_rejected(self, kc):
        """空字符串 username → 400（不是静默创建出空名用户）。"""
        resp = kc.create_user_raw(_payload(username=""))
        assert resp.status_code == 400, resp.text
        assert "User name is missing" in resp.text

    def test_invalid_email_rejected(self, kc):
        """email 格式非法 → 400，错误码 error-invalid-email。"""
        resp = kc.create_user_raw(_payload(email="not-an-email"))
        assert resp.status_code == 400, resp.text
        assert "error-invalid-email" in resp.text

    def test_username_too_long_rejected(self, kc):
        """username 256 字符 → 400，服务端声明范围 [3, 255]。"""
        resp = kc.create_user_raw(_payload(username="a" * 256))
        assert resp.status_code == 400, resp.text
        assert "error-invalid-length" in resp.text

    def test_username_invalid_character_rejected(self, kc):
        """username 含 <>&" 等字符 → 400，错误码 error-username-invalid-character。"""
        resp = kc.create_user_raw(_payload(username=f"qa <{uuid.uuid4().hex[:4]}>&\"x"))
        assert resp.status_code == 400, resp.text
        assert "error-username-invalid-character" in resp.text

    def test_malformed_json_rejected(self, kc):
        """请求体不是合法 JSON → 400，且明确提示无法解析。"""
        resp = kc.create_user_raw(raw_body="{not json")
        assert resp.status_code == 400, resp.text
        assert "Cannot parse the JSON" in resp.text

    def test_wrong_content_type_rejected(self, kc):
        """Content-Type 用 text/plain → 415（不支持的媒体类型，不是 400）。"""
        resp = kc.create_user_raw(
            raw_body='{"username":"qa-ct","enabled":true}', content_type="text/plain"
        )
        assert resp.status_code == 415, resp.text
        _cleanup(kc, resp)

    @pytest.mark.xfail(
        strict=True,
        reason="已上报：空 body 返回 500 unknown_error，期望 400。修复后本用例会 XPASS，届时去掉 xfail 标记",
    )
    def test_empty_body_should_return_400(self, kc):
        """空 body 属于客户端参数错误，应返回 400，而不是 500。

        这条是**缺陷用例**：期望 400，实际 500。用 xfail(strict=True) 标记，
        一旦上游修复，它会变成 XPASS 提醒我们回来改断言。
        """
        resp = kc.create_user_raw(raw_body="")
        assert resp.status_code == 400, f"实际 {resp.status_code}：{resp.text[:150]}"


class TestUsernameLengthBoundary:
    """边界值：服务端声明范围是 [3, 255]，两侧各取一个点。"""

    @pytest.mark.parametrize(
        "length,expected",
        [(2, 400), (3, 201), (255, 201), (256, 400)],
        ids=["len2-below", "len3-min", "len255-max", "len256-above"],
    )
    def test_username_length_boundary(self, kc, length, expected):
        suffix = uuid.uuid4().hex
        name = suffix[:length] if length <= len(suffix) else "a" * (length - 8) + suffix[:8]
        resp = kc.create_user_raw(_payload(username=name))
        try:
            assert resp.status_code == expected, f"长度 {length}：{resp.status_code} {resp.text[:120]}"
        finally:
            _cleanup(kc, resp)
