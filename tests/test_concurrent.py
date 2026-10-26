"""P8 扩展：并发与幂等用例。

为什么值得单独成组：
串行用例永远触发不了竞态——「先查再建」在单线程下永远正确，
只有并发时才会暴露「唯一约束没兜住」「重复扣款」这类问题。

实测结论（Keycloak 26，student-qa，8 线程）：
- 并发建同名用户 → 恰好 1 个 201 + 7 个 409（唯一约束生效）
- 重复删除 → 204 然后 404（幂等，不是 500）
- 并发用同一个 refresh_token → 全部 200（默认未开启 Refresh Token Rotation）
"""
import uuid
from concurrent.futures import ThreadPoolExecutor

PASSWORD = "Passw0rd!"
WORKERS = 8


class TestConcurrentUserCreation:
    def test_concurrent_create_same_username_only_one_wins(self, kc):
        """8 个线程同时创建同名用户：只能成功一个，其余全部 409。

        无论谁先到，最终库里必须只有一个用户——这是「唯一约束 + 并发防护」的底线。
        """
        name = f"qa-{uuid.uuid4().hex[:8]}"

        def attempt(_):
            return kc.create_user_raw(
                {
                    "username": name,
                    "enabled": True,
                    "email": f"{name}@qa.local",
                    "firstName": "Q",
                    "lastName": "U",
                }
            ).status_code

        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            codes = list(pool.map(attempt, range(WORKERS)))

        try:
            assert codes.count(201) == 1, f"应恰好一个成功，实际 {sorted(codes)}"
            assert codes.count(409) == WORKERS - 1, f"其余应全部冲突，实际 {sorted(codes)}"
            assert not any(code >= 500 for code in codes), f"不应出现 5xx：{sorted(codes)}"
        finally:
            uid = kc.find_user_by_username(name)
            if uid:
                kc.delete_user(uid)


class TestIdempotency:
    def test_delete_user_is_idempotent(self, kc, unique_name):
        """重复删除：第一次 204，第二次 404，不能 500。"""
        uid = kc.create_user(unique_name, password=PASSWORD)
        assert kc.delete_user(uid).status_code == 204
        second = kc.delete_user(uid)
        assert second.status_code == 404, f"重复删除应 404，实际 {second.status_code}"

    def test_concurrent_refresh_does_not_break_session(self, kc, unique_name):
        """同一个 refresh_token 并发去换 token，服务端不应 5xx。

        默认未开启 Refresh Token Rotation 时全部会成功；开启后应当只有一个成功、
        其余 invalid_grant。所以这里只断言「不崩」，把具体策略留给配置决定。
        """
        uid = kc.create_user(unique_name, password=PASSWORD)
        try:
            rt = kc.get_token_response(unique_name, PASSWORD)["refresh_token"]

            def hit(_):
                return kc.refresh_token(rt).status_code

            with ThreadPoolExecutor(max_workers=5) as pool:
                codes = list(pool.map(hit, range(5)))

            assert not any(code >= 500 for code in codes), f"不应出现 5xx：{sorted(codes)}"
            assert 200 in codes, f"至少应有一次续期成功，实际 {sorted(codes)}"
        finally:
            kc.delete_user(uid)
