"""把 pytest 的 JUnit 结果同步到禅道（P6 可选步骤）。

默认是 **dry-run**：只解析并打印「将会创建什么」，不发任何请求。
确认无误后加 `--apply` 才真正调用禅道 REST API v1 建 Bug。

环境变量（真提交时必需）：
    ZENTAO_URL       禅道根地址，如 http://localhost:8081 或 http://host/zentao
    ZENTAO_ACCOUNT   禅道账号
    ZENTAO_PASSWORD  禅道密码
    ZENTAO_PRODUCT   产品 ID（数字）
    ZENTAO_BUILD     影响版本，默认 trunk

用法：
    python ci/zentao_sync.py --junit junit-api.xml            # dry-run
    python ci/zentao_sync.py --junit junit-api.xml --apply    # 真提交

说明：走禅道 REST API v1（禅道 18+ 默认提供）。不同版本字段名可能略有差异，
若返回字段错误，按你版本的 `{ZENTAO_URL}/api.php/v1` 文档微调 _bug_payload 即可。
"""
from __future__ import annotations

import argparse
import os
import sys
import xml.etree.ElementTree as ET

try:
    import requests
except ImportError:  # pragma: no cover
    print("需要 requests：pip install requests", file=sys.stderr)
    raise


def parse_junit(path: str) -> dict:
    """解析 JUnit XML，返回汇总 + 失败用例列表。"""
    root = ET.parse(path).getroot()
    cases = list(root.iter("testcase"))

    total = len(cases)
    failed: list[dict] = []
    skipped = 0

    for tc in cases:
        fail = tc.find("failure")
        err = tc.find("error")
        if tc.find("skipped") is not None:
            skipped += 1
        node = fail if fail is not None else err
        if node is not None:
            failed.append(
                {
                    "classname": tc.get("classname", ""),
                    "name": tc.get("name", ""),
                    "message": (node.get("message") or "").strip(),
                    "detail": (node.text or "").strip()[:3000],
                }
            )

    return {
        "total": total,
        "passed": total - len(failed) - skipped,
        "failed": len(failed),
        "skipped": skipped,
        "failed_cases": failed,
    }


def _bug_payload(case: dict, build: str) -> dict:
    """禅道 Bug 字段。字段名如与你版本不符，在这里改。"""
    title = f"[自动化] {case['classname']}::{case['name']} 失败"
    steps = (
        f"<p><strong>失败信息：</strong>{case['message']}</p>"
        f"<p><strong>详细堆栈：</strong></p><pre>{case['detail'][:2000]}</pre>"
        f"<p>来源：Jenkins 自动化流水线 / pytest</p>"
    )
    return {
        "title": title[:255],
        "severity": 3,          # 3 = 一般
        "pri": 2,               # 2 = 中
        "type": "codeerror",
        "openedBuild": build,
        "steps": steps,
    }


def zentao_login(base: str, account: str, password: str) -> str:
    resp = requests.post(
        f"{base}/api.php/v1/tokens",
        json={"account": account, "password": password},
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    token = data.get("token")
    if not token:
        raise RuntimeError(f"登录成功但未返回 token：{data}")
    return token


def zentao_create_bug(base: str, token: str, product: str, payload: dict) -> dict:
    resp = requests.post(
        f"{base}/api.php/v1/products/{product}/bugs",
        json=payload,
        headers={"Token": token, "Content-Type": "application/json"},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


def main() -> int:
    ap = argparse.ArgumentParser(description="Sync pytest JUnit results to ZenTao")
    ap.add_argument("--junit", required=True, help="pytest --junitxml 生成的文件")
    ap.add_argument("--apply", action="store_true", help="真正提交到禅道（默认 dry-run）")
    ap.add_argument("--product", default=os.getenv("ZENTAO_PRODUCT", ""), help="禅道产品 ID")
    ap.add_argument("--build", default=os.getenv("ZENTAO_BUILD", "trunk"), help="影响版本")
    args = ap.parse_args()

    if not os.path.exists(args.junit):
        print(f"找不到 JUnit 文件：{args.junit}", file=sys.stderr)
        return 2

    summary = parse_junit(args.junit)
    print(
        f"JUnit 汇总：共 {summary['total']} 条，通过 {summary['passed']}，"
        f"失败 {summary['failed']}，跳过 {summary['skipped']}"
    )
    if not summary["failed_cases"]:
        print("没有失败用例，无需同步。")
        return 0

    print(f"\n失败用例（{len(summary['failed_cases'])} 条）：")
    for c in summary["failed_cases"]:
        print(f"  - {c['classname']}::{c['name']}  |  {c['message'][:80]}")

    if not args.apply:
        print("\n[dry-run] 未提交。确认后加 --apply 真正建 Bug。")
        return 0

    base = os.getenv("ZENTAO_URL", "").rstrip("/")
    account = os.getenv("ZENTAO_ACCOUNT", "")
    password = os.getenv("ZENTAO_PASSWORD", "")
    product = args.product

    missing = [k for k, v in {
        "ZENTAO_URL": base, "ZENTAO_ACCOUNT": account,
        "ZENTAO_PASSWORD": password, "ZENTAO_PRODUCT": product,
    }.items() if not v]
    if missing:
        print(f"缺少环境变量：{', '.join(missing)}", file=sys.stderr)
        return 2

    token = zentao_login(base, account, password)
    print(f"\n禅道登录成功，开始提交 {len(summary['failed_cases'])} 个 Bug ...")

    created = 0
    for c in summary["failed_cases"]:
        payload = _bug_payload(c, args.build)
        try:
            result = zentao_create_bug(base, token, product, payload)
            bug_id = (result.get("id") or result.get("bug", {}).get("id"))
            print(f"  ✓ {c['name']} -> bug id={bug_id}")
            created += 1
        except Exception as exc:  # noqa: BLE001
            print(f"  ✗ {c['name']} 提交失败：{exc}", file=sys.stderr)

    print(f"\n完成：成功提交 {created}/{len(summary['failed_cases'])} 个 Bug。")
    return 0 if created == len(summary["failed_cases"]) else 1


if __name__ == "__main__":
    sys.exit(main())
