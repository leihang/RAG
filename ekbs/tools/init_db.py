"""``python -m ekbs.tools.init_db`` —— 初始化 SQLite（`01` §5.7 的开发机等价物）。

生产环境这一步由 ``deploy/scripts/install.sh`` 完成（``sqlite3 $DB < sql/001_init.sql``）；
本工具的作用是让开发机可以一条命令把库准备好，并按 `01` §5.7 的口径自检：

* 表数 = **29**（含 ``ekbs_chunk_fts`` 虚拟表，排除 FTS5 的 5 张影子表，否则会得到 34）；
* 审计触发器 = **12**；
* ``ekbs_policy_version.version = 1``（`01` §5.3 的种子数据）；
* ``journal_mode = wal``、``PRAGMA integrity_check = ok``。
"""

from __future__ import annotations

import argparse
import sys

from ekbs.config import load_settings
from ekbs.db import EXPECTED_TABLES, EXPECTED_TRIGGERS, Database
from ekbs.logging import configure_logging, get_logger

log = get_logger(component="tools.init_db")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="初始化 EKBS SQLite（幂等）")
    parser.add_argument("--force", action="store_true", help="即便已初始化也重跑建表脚本")
    args = parser.parse_args(argv)

    settings = load_settings()
    configure_logging(settings, "tools")
    settings.ensure_dirs()
    database = Database(settings)

    if args.force:
        report = database.apply_schema(force=True)
        print("已重跑 sql/001_init.sql（幂等）")
    else:
        report, created = database.ensure_schema()
        print("已建库" if created else "库已存在，跳过建表")

    print(f"  路径            {report.path}")
    print(f"  表数            {report.tables}（期望 {EXPECTED_TABLES}）")
    print(f"  审计触发器      {report.triggers}（期望 {EXPECTED_TRIGGERS}）")
    print(f"  policy_version  {report.policy_version}")
    print(f"  journal_mode    {report.journal_mode}")
    print(f"  integrity_check {report.integrity}")

    if not report.ok:
        print("自检未通过：", "；".join(report.problems()), file=sys.stderr)
        return 1
    print("自检通过。")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
