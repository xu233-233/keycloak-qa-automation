"""P2：用户生命周期接口用例（增 → 查 → 改 → 删）。

验收目标：`pytest` 全绿。
每个用例用 unique_name 造唯一用户名，并在 finally 里清理，保证可重复执行。
"""
import requests

from keycloak_client import KeycloakClient

PASSWORD = "Passw0rd!"


def test_user_lifecycle(kc: KeycloakClient, unique_name):
    """完整生命周期：创建 → 查询 → 更新 → 删除。"""
    email = f"{unique_name}@qa.local"

    # 1) 创建
    user_id = kc.create_user(unique_name, password=PASSWORD, email=email)
    assert user_id, "创建用户应返回 id"

    # 2) 查询：字段与创建时一致
    resp = kc.get_user(user_id)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["username"] == unique_name
    assert body["email"] == email
    assert body["enabled"] is True

    # 3) 更新：改姓名后再查，确认已生效
    resp = kc.update_user(user_id, firstName="测试", lastName="更新")
    assert resp.status_code == 204, resp.text
    body = kc.get_user(user_id).json()
    assert body["firstName"] == "测试"
    assert body["lastName"] == "更新"

    # 4) 删除：删除后查询应 404
    assert kc.delete_user(user_id).status_code == 204
    assert kc.get_user(user_id).status_code == 404


def test_created_user_in_list(kc: KeycloakClient, unique_name):
    """创建的用户应出现在用户列表中。"""
    user_id = kc.create_user(unique_name, password=PASSWORD)
    try:
        usernames = [u["username"] for u in kc.list_users()]
        assert unique_name in usernames
    finally:
        kc.delete_user(user_id)


def test_duplicate_username_conflict(kc: KeycloakClient, unique_name):
    """重复用户名创建应返回 409 Conflict（负向用例）。"""
    user_id = kc.create_user(unique_name, password=PASSWORD)
    try:
        resp = requests.post(
            kc.admin_url("/users"),
            json={"username": unique_name, "enabled": True},
            headers=kc.auth_headers(),
        )
        assert resp.status_code == 409, resp.text
    finally:
        kc.delete_user(user_id)
