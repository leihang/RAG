"""``python -m ekbs.tools.local_account`` —— 本机登录账号的运维入口。

管理台有两条登录路径：**钉钉免登**（日常）与**本机账号密码**（服务器本机运维/应急）。
本工具管后者 —— 账号不存在时无法登录，必须先在这里创建。

用法::

    # 创建：绑定到某个已有用户（默认绑定唯一的 super_admin）
    python -m ekbs.tools.local_account create --username admin
    python -m ekbs.tools.local_account create --username admin --user-id 5
    python -m ekbs.tools.local_account create --username admin --generate   # 自动生成强口令

    # 改密 / 启停 / 查看
    python -m ekbs.tools.local_account passwd --username admin --generate
    python -m ekbs.tools.local_account disable --username admin
    python -m ekbs.tools.local_account list

三条纪律：

1. **口令只以 scrypt 编码串入库**（`ekbs.local_auth`），本工具不回显既有口令；
2. ``--generate`` 生成的口令**只在屏幕上打印一次**，请立刻存入密码管理器；
3. 账号**只能绑定已有 ``ekbs_user``** —— 权限/密级/审计沿用既有链路，
   本机账号不会凭空多出权限。
"""

from __future__ import annotations

import argparse
import getpass
import sys

from ekbs.config import load_settings
from ekbs.db import Database
from ekbs.local_auth import MIN_PASSWORD_LENGTH, LocalAccountService
from ekbs.logging import configure_logging, get_logger

log = get_logger(component="tools.local_account")


def _resolve_super_admin(db: Database) -> int:
    """默认绑定唯一的 super_admin；找不到或有多个时要求显式 ``--user-id``。"""
    with db.sync_connect() as conn:
        rows = conn.execute(
            "SELECT u.user_id, u.nick FROM ekbs_user u "
            "JOIN ekbs_user_role ur ON ur.user_id = u.user_id "
            "JOIN ekbs_role r ON r.role_id = ur.role_id "
            "WHERE r.code = 'super_admin' AND u.deleted_at IS NULL"
        ).fetchall()
    if len(rows) == 1:
        return int(rows[0]["user_id"])
    if not rows:
        raise SystemExit("未找到 super_admin 用户，请用 --user-id 显式指定绑定对象")
    raise SystemExit(
        "存在多个 super_admin，请用 --user-id 指定绑定对象："
        + ", ".join(f"{r['user_id']}({r['nick']})" for r in rows)
    )


def _read_password(args: argparse.Namespace, service: LocalAccountService) -> str:
    if getattr(args, "generate", False):
        password = service.generate_password()
        print(f"[生成的口令] {password}")
        print("⚠️ 只显示这一次，请立即存入密码管理器；不要贴进聊天工具或工单。")
        return password
    if getattr(args, "password", None):
        return str(args.password)
    first = getpass.getpass(f"请输入口令（≥{MIN_PASSWORD_LENGTH} 位）：")
    second = getpass.getpass("请再输入一次：")
    if first != second:
        raise SystemExit("两次输入不一致")
    return first


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="本机登录账号管理（管理台第二种登录方式）")
    sub = parser.add_subparsers(dest="cmd", required=True)

    create = sub.add_parser("create", help="创建账号")
    create.add_argument("--username", required=True)
    create.add_argument("--user-id", type=int, help="绑定的 ekbs_user.user_id（默认 super_admin）")
    create.add_argument("--password", help="口令（不推荐：会进 shell 历史）")
    create.add_argument("--generate", action="store_true", help="自动生成强口令")
    create.add_argument("--must-change", action="store_true", help="首次登录须改密")

    passwd = sub.add_parser("passwd", help="修改口令")
    passwd.add_argument("--username", required=True)
    passwd.add_argument("--password")
    passwd.add_argument("--generate", action="store_true")

    for name in ("disable", "enable"):
        cmd = sub.add_parser(name, help=f"{name} 账号")
        cmd.add_argument("--username", required=True)

    sub.add_parser("list", help="列出现有账号")

    args = parser.parse_args(argv)
    settings = load_settings()
    configure_logging(settings, "tool")
    db = Database(settings)
    db.apply_schema()
    service = LocalAccountService(db, settings)

    if args.cmd == "create":
        user_id = int(args.user_id) if args.user_id else _resolve_super_admin(db)
        password = _read_password(args, service)
        service.create(
            username=args.username, user_id=user_id, password=password,
            must_change=bool(getattr(args, "must_change", False)),
        )
        print(f"✅ 已创建本机账号 {args.username} → user_id={user_id}")
        print("登录入口：管理台登录页「本机账号登录」（仅服务器本机可用）")
        return 0

    if args.cmd == "passwd":
        password = _read_password(args, service)
        changed = service.set_password(args.username, password)
        if not changed:
            raise SystemExit(f"账号不存在：{args.username}")
        print(f"✅ 已更新 {args.username} 的口令（失败计数与锁定已清零）")
        return 0

    if args.cmd in {"disable", "enable"}:
        status = "disabled" if args.cmd == "disable" else "active"
        changed = service.set_status(args.username, status)
        if not changed:
            raise SystemExit(f"账号不存在：{args.username}")
        print(f"✅ {args.username} → {status}")
        return 0

    rows = service.list_accounts()
    if not rows:
        print("(无本机账号；用 create 创建)")
        return 0
    print(f"{'username':<16}{'user_id':<9}{'status':<10}{'failed':<8}{'locked_until':<21}nick")
    for row in rows:
        print(
            f"{row['username']:<16}{row['user_id']:<9}{row['status']:<10}"
            f"{row['failed_attempts']:<8}{str(row['locked_until'] or '-'):<21}{row['nick']}"
        )
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
