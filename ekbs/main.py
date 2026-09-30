"""``ekbs.main:app`` —— uvicorn 的入口（`01` §8.1 的 plist 用 ``ekbs.main:app``）。

启动方式（开发机）：

.. code-block:: bash

    EKBS_CONFIG=config/application.dev.yaml .venv/bin/uvicorn ekbs.main:app \\
        --host 127.0.0.1 --port 8000 --workers 1

``--workers 1`` 不是性能取舍而是**正确性要求**：权限缓存的进程内 L1 靠同进程同步失效，
多 worker 会出现 ≤300s 的旧权限窗口（`04` §1.5 / §7.2）。
"""

from __future__ import annotations

from ekbs.api.app import create_app

app = create_app()

__all__ = ["app"]
