"""托管管理台前端构建产物（`ekbs-admin-web/dist`）。

方案 `05` §6.9 的口径是 **Caddy 托管静态资源**；当前部署还没有 Caddy，
所以这里先由 `ekbs-api` 以 `/admin` 前缀把同一份 `dist/` 挂出去 —— 两者都是
「同源静态资源」，`VITE_API_BASE=/api/v1` 一个字都不用改就能平滑切到 Caddy。

三件事必须做对，否则表现为「页面白屏但接口全绿」：

1. **SPA fallback**：`/admin/docs/list` 这类前端路由在服务端没有对应文件，
   必须回落到 `index.html`，否则刷新即 404；
2. **带哈希的静态资源可以长缓存，`index.html` 绝对不能缓存**（§6.9）——
   否则发版后浏览器拿着旧 index 去要已被删除的旧哈希资源，白屏；
3. **资源目录与 SPA 回落分开注册**：`/admin/assets/*` 由静态挂载处理，
   不能被通配路由吞掉。

产物查找顺序（先到先用）：

1. 环境变量 ``EKBS_ADMIN_WEB_DIR``（部署时指到 `/opt/ekbs/app/web`）；
2. ``<data_dir>/web``（与 `/data/ekbs` 同级的部署布局）；
3. 仓库内 ``ekbs-admin-web/dist``（开发机）。

都找不到就**保持旧的单文件管理台**（`/admin` 仍可用），并打一条警告 ——
不能因为前端没构建就让整个管理入口消失。
"""

from __future__ import annotations

import contextlib
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

from ekbs.logging import get_logger

log = get_logger(component="api.admin_web")

#: 生产部署把构建产物放这里（方案 P6：dev/test/prod 共用同一份产物）
PROD_WEB_DIR = Path("/opt/ekbs/app/web")

#: 开发机产物位置（提成常量是为了测试能打桩，不必去动 __file__ 推导）
REPO_WEB_DIR = Path(__file__).resolve().parents[2] / "ekbs-admin-web" / "dist"

#: index.html 禁缓存；带内容哈希的资源可以长缓存（方案 §6.9）
NO_CACHE = {"Cache-Control": "no-cache, no-store, must-revalidate"}
LONG_CACHE = {"Cache-Control": "public, max-age=31536000, immutable"}


def find_web_dir(settings) -> Path | None:
    """按优先级找出可用的前端产物目录；都不可用返回 None。"""
    candidates: list[Path] = []
    env_dir = os.environ.get("EKBS_ADMIN_WEB_DIR")
    if env_dir:
        candidates.append(Path(env_dir))
    with contextlib.suppress(Exception):  # pragma: no cover - 配置异常时跳过
        candidates.append(Path(settings.data_dir).parent / "web")
    candidates.append(PROD_WEB_DIR)
    candidates.append(REPO_WEB_DIR)

    for path in candidates:
        if (path / "index.html").is_file():
            return path
    return None


def install_admin_web(app: FastAPI, settings) -> Path | None:
    """挂载 `/admin` 下的 SPA；返回实际使用的产物目录（None = 用旧的单文件页）。"""
    dist = find_web_dir(settings)
    if dist is None:
        log.warning(
            "admin_web_dist_missing",
            hint="未找到 ekbs-admin-web/dist，/admin 继续用旧的免构建单页；"
                 "在 ekbs-admin-web 下执行 `npm run build` 即可启用新管理台",
        )
        return None

    assets = dist / "assets"

    # 刻意**不用** StaticFiles 挂载：挂载会绕过这里设置缓存头，
    # 结果带内容哈希的资源拿不到 `immutable` 长缓存（实测如此）。
    # 统一走下面这条通配路由，缓存策略与 SPA 回落都在一处可控。
    @app.get("/admin", include_in_schema=False)
    @app.get("/admin/{spa_path:path}", include_in_schema=False)
    async def admin_spa(spa_path: str = "") -> FileResponse:
        # 产物目录里的真实文件（assets/*、favicon.ico 等）直出
        if spa_path:
            candidate = (dist / spa_path).resolve()
            try:
                candidate.relative_to(dist.resolve())
            except ValueError:
                # 路径穿越：不回落到 index，直接 404
                raise HTTPException(status_code=404, detail="not found") from None
            if candidate.is_file():
                headers = LONG_CACHE if spa_path.startswith("assets/") else NO_CACHE
                return FileResponse(candidate, headers=headers)
            if spa_path.startswith("assets/"):
                # 带哈希资源缺失（发版后旧 index 引用已删除文件）必须**明确 404**：
                # 回落成 HTML 会让浏览器把 HTML 当 JS 解析，报一个与真因无关的语法错误。
                raise HTTPException(status_code=404, detail="asset not found")
        # 其余一律回落到 index.html（SPA 路由），且禁缓存
        index = dist / "index.html"
        if not index.is_file():  # pragma: no cover - find_web_dir 已保证
            raise HTTPException(status_code=503, detail="管理台产物缺失")
        return FileResponse(index, headers=NO_CACHE)

    log.info("admin_web_mounted", dist=str(dist), has_assets=assets.is_dir())
    return dist
