"""模型可用性自检 —— 出问题时先跑它，一眼区分四类原因。

## 为什么需要

云端报错常常是**一条笼统的 message**（如 `You exceeded your current quota`），但它可能是
四种完全不同的原因，处置方式截然不同：

| 现象 | 错误码/特征 | 处置 |
|---|---|---|
| 账号欠费 | `Arrearage` | 充值 |
| **模型未开通**（不在可用范围） | `ModelNotOpen` / `AccessDenied` / `Model.AccessDenied` | 百炼控制台「模型广场」开通该模型 |
| **限流 / 免费额度周期用尽** | `Throttling.*` / `AllocationQuota.*` / 笼统的 quota message | 退避重试、错峰；关掉"仅免费额度"或提配额 |
| key 无效 | `InvalidApiKey` / `AuthenticationError` | 换 key |

**关键**：同一个账号里"有的模型通、有的不通"才能区分"账号问题"与"模型未开通"，
所以本工具**逐个模型探活**，而不是只试一个。

用法：
    .venv/bin/python -m ekbs.tools.probe_models          # 探活所有已配置模型
    .venv/bin/python -m ekbs.tools.probe_models --json   # 机器可读
"""

from __future__ import annotations

import argparse
import json
import sys

import httpx

COMPATIBLE = "https://dashscope.aliyuncs.com/compatible-mode/v1"

#: 常见错误的归类（用于给出"该干什么"）
_VERDICT = (
    ("arrearage", "账号欠费 → 去阿里云充值"),
    ("modelnotopen", "模型未开通 → 百炼控制台「模型广场」开通该模型"),
    ("accessdenied", "模型未开通/无权限 → 百炼控制台开通，或给子账号授权"),
    ("throttling", "限流 → 退避重试/错峰；或关闭「仅免费额度」/提升配额"),
    ("allocationquota", "配额用尽（可能是免费额度或 TPM）→ 退避重试或提配额"),
    ("quota", "笼统的额度/限流报错 → 先退避重试；仍失败再看控制台套餐与「仅免费额度」开关"),
    ("invalidapikey", "key 无效 → 检查 DASHSCOPE_API_KEY"),
    ("authenticationerror", "认证失败 → 检查 key 是否属于当前账号/地域"),
)


def classify(message: str, code: str) -> str:
    blob = f"{code} {message}".lower()
    for key, verdict in _VERDICT:
        if key in blob:
            return verdict
    return "未识别的错误 → 把完整 message 发给服务方"


def probe(label: str, url: str, body: dict, headers: dict[str, str]) -> dict:
    try:
        resp = httpx.post(url, headers=headers, json=body, timeout=60)
    except Exception as exc:  # noqa: BLE001
        return {"label": label, "ok": False, "status": None, "code": "network",
                "message": str(exc)[:200], "verdict": "网络不通 → 检查代理/防火墙/白名单"}
    if resp.status_code == 200:
        payload = resp.json()
        usage = payload.get("usage") or {}
        return {"label": label, "ok": True, "status": 200, "code": "",
                "message": f"usage={usage}" if usage else "", "verdict": "可用"}
    try:
        error = (resp.json().get("error") or {})
    except Exception:  # noqa: BLE001
        error = {"message": resp.text[:200]}
    message = str(error.get("message") or "")
    code = str(error.get("code") or "")
    return {"label": label, "ok": False, "status": resp.status_code, "code": code,
            "message": message[:220], "verdict": classify(message, code)}


def probe_multimodal(model: str, key: str) -> dict:
    """多模态嵌入（图片/文本共用模型）—— 只能走原生 SDK，用文本输入探活。"""
    label = f"多模态嵌入 {model}"
    try:
        from dashscope import MultiModalEmbedding

        resp = MultiModalEmbedding.call(model=model, input=[{"text": "自检"}], api_key=key)
        status = int(getattr(resp, "status_code", 0) or 0)
        if status == 200:
            return {"label": label, "ok": True, "status": 200, "code": "",
                    "message": f"usage={getattr(resp, 'usage', None)}", "verdict": "可用"}
        code = str(getattr(resp, "code", "") or "")
        message = str(getattr(resp, "message", "") or "")
        return {"label": label, "ok": False, "status": status, "code": code,
                "message": message[:220], "verdict": classify(message, code)}
    except Exception as exc:  # noqa: BLE001
        return {"label": label, "ok": False, "status": None, "code": "sdk-error",
                "message": str(exc)[:200], "verdict": "SDK 调用异常 → 检查 dashscope 版本与网络"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="云端模型可用性自检")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    # 延迟导入：本工具要先能跑起来，再谈配置对不对
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[2]))
    from ekbs.config import Secrets, load_settings

    settings = load_settings()
    secrets = Secrets()
    key = secrets.get("DASHSCOPE_API_KEY")
    if not key:
        print("缺少 DASHSCOPE_API_KEY（看 ~/.ekbs/secrets.env）")
        return 1
    headers = {"Authorization": f"Bearer {key}", "content-type": "application/json"}
    models = settings.models

    targets: list[tuple[str, str, dict]] = [
        (f"嵌入 {models.embed.model}", f"{COMPATIBLE}/embeddings",
         {"model": models.embed.model, "input": ["自检"], "dimensions": int(models.embed.dim)}),
        (f"生成 {models.llm.model}", f"{COMPATIBLE}/chat/completions",
         {"model": models.llm.model, "messages": [{"role": "user", "content": "hi"}],
          "max_tokens": 5}),
    ]
    # 重排走**原生端点**（不是 OpenAI 兼容的 chat/completions），body 形状照 provider 实现
    rerank_cfg = settings.retrieval.rerank
    if getattr(rerank_cfg, "enabled", False) and getattr(rerank_cfg, "model", ""):
        targets.append(
            (f"重排 {rerank_cfg.model}",
             "https://dashscope.aliyuncs.com/api/v1/services/rerank/text-rerank/text-rerank",
             {"model": rerank_cfg.model,
              "input": {"query": "自检", "documents": ["甲", "乙"]},
              "parameters": {"top_n": 1}})
        )
    vision = getattr(models, "vision", None)
    if vision is not None and getattr(vision, "model", ""):
        targets.append(
            (f"视觉 {vision.model}", f"{COMPATIBLE}/chat/completions",
             {"model": vision.model, "messages": [{"role": "user", "content": "hi"}],
              "max_tokens": 5})
        )
    # 多模态嵌入没有 OpenAI 兼容端点，走 DashScope 原生 SDK —— 用一段文本探活即可
    # （这条路径曾出过事故：图向量索引与库不一致，见 README 发现 78，所以必须能自检）
    mm = getattr(models, "multimodal_embed", None)
    results = [probe(label, url, body, headers) for label, url, body in targets]
    if mm is not None and getattr(mm, "model", ""):
        results.append(probe_multimodal(str(mm.model), key))

    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        print(f"\n  账号 key：{key[:6]}…{key[-4:]}（同一把 key 逐个模型探活）\n")
        for item in results:
            mark = "✅" if item["ok"] else "❌"
            print(f"  {mark} {item['label']}")
            if not item["ok"]:
                print(f"       HTTP {item['status']}  code={item['code'] or '(空)'}")
                print(f"       message={item['message']}")
            print(f"       → {item['verdict']}")
        ok = sum(1 for i in results if i["ok"])
        print(f"\n  小结：{ok}/{len(results)} 可用")
        if ok == len(results):
            print("  → 模型都可用。若某次调用仍报 quota，那是**限流/免费额度周期**问题：")
            print("     错峰重试即可（本项目已配 max_retries=3 退避）；一次性大文档（如十几万 token 的 xlsx）"
                  "建议与其它文档分开入库。")
        elif ok:
            print("  → 部分模型不可用：属于**模型级**问题（开通/配额），不是账号整体故障。")
        else:
            print("  → 全部不可用：多半是账号级问题（欠费/key/实名/地域）。")
    return 0 if all(i["ok"] for i in results) else 2


if __name__ == "__main__":
    sys.exit(main())
