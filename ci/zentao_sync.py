"""把 pytest 的 JUnit 结果同步到禅道（P6 流程的一环）。

默认是 **dry-run**：只解析并打印「将会创建什么」，不发任何请求。
确认无误后加 `--apply` 才真正调用禅道 REST API v1 建 Bug。

配置来源（优先级：已有环境变量 > 项目根目录 .env 文件）：
    ZENTAO_URL       禅道根地址，云禅道形如 https://xxxx.chandao.net
    ZENTAO_ACCOUNT   禅道账号
    ZENTAO_PASSWORD  禅道密码
    ZENTAO_PRODUCT   产品 ID（数字，取自产品页 URL 末尾）
    ZENTAO_BUILD     影响版本，默认 trunk

用法：
    python ci/zentao_sync.py --ping                            # 只验证登录，不建单
    python ci/zentao_sync.py --junit junit-api.xml             # dry-run，看会建什么单
    python ci/zentao_sync.py --junit junit-api.xml --apply     # 真提交

说明：走禅道 REST API v1。云禅道已实测 /api.php/v1/tokens 与
/api.php/v1/products/{id}/bugs 两个端点可用；若你的实例字段名有差异，
按返回的错误信息微调 _bug_payload 即可。
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


def load_dotenv(path: str | None = None) -> None:
    """读取项目根目录的 .env（已存在的环境变量优先，不覆盖）。

    这样凭据不必写在命令行里，也就不会留在 PowerShell 历史中。
    """
    if path is None:
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
    if not os.path.exists(path):
        return
    # utf-8-sig：Windows 记事本 / PowerShell 5.1 写出的 UTF-8 常带 BOM，
    # 用 utf-8 读会让第一个键变成 "\ufeffZENTAO_URL" 从而匹配不上
    with open(path, encoding="utf-8-sig") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


def _check_config(cfg: dict) -> bool:
    """检查四项配置是否齐全，缺失时打印 .env 模板并返回 False。"""
    mapping = {
        "ZENTAO_URL": cfg["base"],
        "ZENTAO_ACCOUNT": cfg["account"],
        "ZENTAO_PASSWORD": cfg["password"],
        "ZENTAO_PRODUCT": cfg["product"],
    }
    missing = [k for k, v in mapping.items() if not v]
    if not missing:
        return True
    print(f"缺少配置：{', '.join(missing)}", file=sys.stderr)
    print(
        "请在项目根目录建 .env 文件（已被 .gitignore 忽略），内容示例：\n"
        "  ZENTAO_URL=https://xxxx.chandao.net\n"
        "  ZENTAO_ACCOUNT=你的账号\n"
        "  ZENTAO_PASSWORD=你的密码\n"
        "  ZENTAO_PRODUCT=1",
        file=sys.stderr,
    )
    return False


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
    # 不先 raise_for_status：禅道 400 时 body 里通常带具体原因，先解析再决定
    try:
        data = resp.json()
    except ValueError:
        raise RuntimeError(f"返回的不是 JSON（HTTP {resp.status_code}）：{resp.text[:200]}")

    # 禅道把「账号密码错」也返回 200 + error 字段，需显式判断
    if data.get("error"):
        raise RuntimeError(f"禅道返回错误：{data['error']}（HTTP {resp.status_code}）")

    token = data.get("token")
    if not token:
        raise RuntimeError(f"HTTP {resp.status_code}，未返回 token：{resp.text[:200]}")
    return token


def zentao_create_bug(base: str, token: str, product: str, payload: dict) -> dict:
    resp = requests.post(
        f"{base}/api.php/v1/products/{product}/bugs",
        json=payload,
        headers={"Token": token, "Content-Type": "application/json"},
        timeout=10,
    )
    if resp.status_code >= 400:
        raise RuntimeError(f"HTTP {resp.status_code}：{resp.text[:300]}")
    return resp.json()


def main() -> int:
    load_dotenv()

    ap = argparse.ArgumentParser(description="Sync pytest JUnit results to ZenTao")
    ap.add_argument("--junit", help="pytest --junitxml 生成的文件（--ping 时可不填）")
    ap.add_argument("--ping", action="store_true", help="只验证禅道登录与配置，不解析用例、不建单")
    ap.add_argument("--apply", action="store_true", help="真正提交到禅道（默认 dry-run）")
    ap.add_argument("--product", default=os.getenv("ZENTAO_PRODUCT", ""), help="禅道产品 ID")
    ap.add_argument("--build", default=os.getenv("ZENTAO_BUILD", "trunk"), help="影响版本")
    args = ap.parse_args()

    cfg = {
        "base": os.getenv("ZENTAO_URL", "").rstrip("/"),
        "account": os.getenv("ZENTAO_ACCOUNT", ""),
        "password": os.getenv("ZENTAO_PASSWORD", ""),
        "product": args.product or os.getenv("ZENTAO_PRODUCT", ""),
    }

    # --- 模式一：只验证登录（不建单、不污染产品） ---
    if args.ping:
        if not _check_config(cfg):
            return 2
        print(f"禅道地址：{cfg['base']}")
        print(f"产品 ID ：{cfg['product']}")
        try:
            token = zentao_login(cfg["base"], cfg["account"], cfg["password"])
        except Exception as exc:  # noqa: BLE001
            print(f"[FAIL] 登录失败：{exc}", file=sys.stderr)
            return 1
        print(f"[OK] 登录成功，token 前 8 位：{token[:8]}…")
        return 0

    if not args.junit:
        print("需要 --junit 参数，或用 --ping 只验证登录。", file=sys.stderr)
        return 2

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
        sample = _bug_payload(summary["failed_cases"][0], args.build)
        print("\n[dry-run] 首条 Bug 将提交的字段：")
        for k, v in sample.items():
            print(f"  {k} = {str(v).replace(chr(10), ' ')[:90]}")
        print("\n[dry-run] 未提交。确认后加 --apply 真正建 Bug。")
        return 0

    if not _check_config(cfg):
        return 2

    try:
        token = zentao_login(cfg["base"], cfg["account"], cfg["password"])
    except Exception as exc:  # noqa: BLE001
        print(f"[FAIL] 登录失败：{exc}", file=sys.stderr)
        return 1
    print(f"\n禅道登录成功，开始提交 {len(summary['failed_cases'])} 个 Bug ...")

    created = 0
    for c in summary["failed_cases"]:
        payload = _bug_payload(c, args.build)
        try:
            result = zentao_create_bug(cfg["base"], token, cfg["product"], payload)
            bug_id = (result.get("id") or result.get("bug", {}).get("id"))
            print(f"  ✓ {c['name']} -> bug id={bug_id}")
            created += 1
        except Exception as exc:  # noqa: BLE001
            print(f"  ✗ {c['name']} 提交失败：{exc}", file=sys.stderr)

    print(f"\n完成：成功提交 {created}/{len(summary['failed_cases'])} 个 Bug。")
    return 0 if created == len(summary["failed_cases"]) else 1


if __name__ == "__main__":
    sys.exit(main())
