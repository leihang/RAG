"""前端「契约归一化」的接线守卫（源码级 lint，不做渲染）。

## 为什么需要这个文件

后端返回的是 snake_case 原始报文，前端各页的 `*Data.ts` 负责归一化成 camelCase，
再由展示组件消费。一旦有人**跳过归一化、用类型断言把不匹配压下去**，编译期一切正常、
运行时才炸。用户实测到的正是这一条：

```
检索调试台 → TypeError: undefined is not an object (evaluating 's.perShard.reduce')
```

`Playground.tsx` 把 `result.stages` 直接用 `as never` 塞给了 `StagesPanel`，
而面板读的是归一化后的 `dense.perShard`，后端给的是 `dense.per_shard` ——
`perShard` 是 `undefined`，`.reduce` 当场抛。归一化函数一直都在
（`playgroundData.parseStages`，且有单测），只是没被调用。

这类断言没有任何正当用途（它把"类型不匹配"这条唯一的编译期信号也关掉了），
所以这里全局禁掉；顺带钉住那条具体的接线。
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
WEB_SRC = REPO_ROOT / "ekbs-admin-web" / "src"


def _frontend_sources() -> list[Path]:
    if not WEB_SRC.is_dir():  # pragma: no cover - 前端源码始终在仓库里
        pytest.skip(f"前端源码不存在：{WEB_SRC}")
    return sorted(
        p
        for p in WEB_SRC.rglob("*")
        if p.is_file() and p.suffix in {".ts", ".tsx"} and ".test." not in p.name
    )


def test_no_type_assertion_silences_contract_mismatch() -> None:
    """禁止 `as never`：用断言替代归一化 = 把编译期错误推迟成线上崩溃。"""
    offenders = []
    for path in _frontend_sources():
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if "as never" in line:
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{number}: {line.strip()}")
    assert not offenders, (
        "发现用类型断言压制契约不匹配的写法（应改为调用对应的归一化函数）：\n  "
        + "\n  ".join(offenders)
    )


def test_playground_normalizes_stages_before_rendering() -> None:
    """`StagesPanel` 只接受归一化后的 `DebugStages`；接线必须过 `parseStages`。"""
    source = (WEB_SRC / "pages" / "rag" / "Playground.tsx").read_text(encoding="utf-8")
    assert "parseStages" in source, "Playground 没有引入 parseStages"
    assert "fetchDiscoveryCatalog" in source, "D22：Playground 应接入发现层 catalog"
    assert "ekbs-discovery-chips" in source, "D22：Playground 应有空态 chips 容器"
    assert "stages={parseStages(" in source.replace(" ", ""), (
        "StagesPanel 拿到的不是归一化结果 —— 面板读 perShard，后端给 per_shard，"
        "直接传原始报文会在 dense.perShard.reduce 抛 TypeError"
    )


def test_stages_panel_has_no_other_callers() -> None:
    """如果哪天多了别的调用方，这条用例会提醒你它也必须过归一化。"""
    callers = set()
    for path in _frontend_sources():
        text = path.read_text(encoding="utf-8")
        if "StagesPanel" in text and path.name != "StagesPanel.tsx":
            callers.add(path.relative_to(REPO_ROOT).as_posix())
    assert callers == {"ekbs-admin-web/src/pages/rag/Playground.tsx"}, (
        f"StagesPanel 的调用方变了：{sorted(callers)}；新调用方同样必须传 parseStages(...)"
    )


# ---------------------------------------------------------------------------
# 钉钉免登接线（04 §3.4）：登录页必须真的走 authCode → /auth/dingtalk-login
# ---------------------------------------------------------------------------


def test_login_page_does_not_auto_dingtalk_sso() -> None:
    """管理台登录页不走钉钉免登。钉钉同事不能从这里进入。"""
    source = (WEB_SRC / "pages" / "login" / "index.tsx").read_text(encoding="utf-8")
    assert "requestAuthCode(" not in source
    assert "loginWithDingTalk(" not in source
    assert "钉钉同事不能" in source


def test_auth_api_posts_authcode_to_backend() -> None:
    """authCode 必须交给**服务端**换身份：前端不得自己拼身份或信任本地声明。"""
    source = (WEB_SRC / "api" / "auth.ts").read_text(encoding="utf-8")
    assert "'/auth/dingtalk-login'" in source or '"/auth/dingtalk-login"' in source
    assert "auth_code" in source, "请求体字段名必须是 auth_code（后端契约）"


def test_no_corp_id_hardcoded_in_frontend() -> None:
    """corpId 等服务端配置不得硬编码进前端产物（换企业/换应用不该要求重新构建）。"""
    offenders = []
    for path in _frontend_sources():
        text = path.read_text(encoding="utf-8")
        if "ding" in text.lower() and "corpId:" in text and "corpId: string" not in text:
            offenders.append(path.relative_to(REPO_ROOT).as_posix())
    assert not offenders, f"疑似硬编码 corpId：{offenders}"


def test_login_page_wires_local_password_login() -> None:
    """管理台第二种登录方式：本机账号密码（仅服务器本机可用）。

    必须真的有表单 + 真的调后端；失败原因（密码错/被锁/仅限本机）要显示出来。
    """
    source = (WEB_SRC / "pages" / "login" / "index.tsx").read_text(encoding="utf-8")
    assert "loginWithPassword(" in source, "登录页没有接本机账号登录"
    assert "Input.Password" in source, "登录页缺少口令输入框"
    assert "localError" in source, "本机登录失败必须把原因显示出来"
    assert "管理台账号" in source, "登录页要说明账号在管理台账号里新建"
    assert "新建用户" in source, "缺少「没有账号时去管理台账号新建」的说明"


def test_local_login_api_targets_backend_endpoint() -> None:
    source = (WEB_SRC / "api" / "auth.ts").read_text(encoding="utf-8")
    assert "'/auth/local-login'" in source or '"/auth/local-login"' in source
    hook = (WEB_SRC / "hooks" / "useAuth.ts").read_text(encoding="utf-8")
    assert "loginWithPassword" in hook, "useAuth 未暴露本机登录"
