# adapters — 把 EKBS 接到编排层

EKBS 只做**企业知识库**。LangChain / LangGraph / OpenClaw 在外层编排，通过同一 HTTP 语义调用：

`POST /api/v1/qa/chat`

```
adapters/
  ekbs_tool/          # Python 核心 + LangChain Tool + LangGraph 节点
  openclaw-ekbs/      # OpenClaw 插件（ekbs_ask）
```

## 你现在要测 OpenClaw

见 [`openclaw-ekbs/README.md`](./openclaw-ekbs/README.md)：

1. 起 EKBS API  
2. `node …/smoke.mjs` 验证 HTTP  
3. `openclaw plugins install` 挂工具  

## LangChain / LangGraph（同一客户端）

```bash
export PYTHONPATH=adapters:$PYTHONPATH
export EKBS_INTERNAL_TOKEN=...
export EKBS_TOOL_USER_ID=364

python -c "from ekbs_tool import EkbsClient; print(EkbsClient.from_env().ask_text('年假有多少天'))"
```

```python
from ekbs_tool import build_langchain_tool, ekbs_ask_node
tool = build_langchain_tool()          # Agent / ToolNode
# 或图节点：ekbs_ask_node({"question": "...", "user_id": 364})
```

## 约定

| 项 | 值 |
|---|---|
| 鉴权 | `EKBS_INTERNAL_TOKEN` + `EKBS_TOOL_USER_ID`，或 JWT |
| 拒答 | 文本以 `[EKBS 拒答` 开头 → 外层禁止编造 |
| 不要 | 在编排层再索引同一批企业文档 |
