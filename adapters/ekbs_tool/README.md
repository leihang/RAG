# ekbs_tool（Python）

企业知识库 Tool 客户端。LangChain / LangGraph 共用同一 `EkbsClient`。

```bash
# 在仓库根
export PYTHONPATH=adapters:$PYTHONPATH
export EKBS_API_BASE=http://127.0.0.1:8000/api/v1
export EKBS_INTERNAL_TOKEN=...          # ~/.ekbs/secrets.env
export EKBS_TOOL_USER_ID=364            # 真实用户，决定 ACL

python -c "from ekbs_tool import EkbsClient; print(EkbsClient.from_env().ask_text('年假有多少天'))"
```

可选依赖：

```bash
pip install httpx
pip install langchain-core   # LangChain / LangGraph ToolNode
```

LangChain：

```python
from ekbs_tool import build_langchain_tool
tool = build_langchain_tool()
```

LangGraph：

```python
from ekbs_tool import ekbs_ask_node
# 或 ToolNode([build_langchain_tool()])
```
