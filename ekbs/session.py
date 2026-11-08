"""多轮会话上下文与查询改写（`00` §2.1、`04` §5.5、`01` §5.5 的 ``ekbs_session_ctx``）。

两件事放在一起，因为它们共用同一份历史：

1. **会话上下文**：落 ``ekbs_session_ctx``（**替代 Redis Hash**），主键是
   ``(conversation_id, dingtalk_userid)`` —— 群聊的 ``conversation_id`` 是**共享**的，
   因此必须带用户维度，否则群里的 A 会读到 B 的上下文（`01` §5.5 的注释、
   ``session.group_share_context`` 默认 false）。TTL 由 ``context_ttl_seconds`` 控制，
   保留轮数由 ``context_max_turns`` 控制。
2. **查询改写**：`00` §2.1 把「查询改写」列为链路第一步，触发条件是**多轮对话**。
   策略可配（管理台的 Prompt 模板对应 ``llm`` 策略）：

   * ``off``：不改写；
   * ``heuristic``（默认）：当前问题含**整词**指代（它/这个/上述…）、追问展开用语
     （详细/继续/再说…），或过短且不是人名/编号时，把**上一轮问题**并入检索查询；
   * ``llm``：用云端 LLM 按模板改写成自包含查询（需要真实模型；mock 会返回固定串，
     因此默认不启用，避免把测试与离线环境搞坏）。

**为什么要改写而不是直接把历史塞给检索**：FAISS/FTS5 是对「一个查询串」召回的，
把历史对话整段拼进去会稀释查询向量（相似度被无关轮次拉平），实测通常反而降召回。
"""

from __future__ import annotations

import contextvars
import json
import re
from dataclasses import dataclass, field
from typing import Any, Sequence

from ekbs.config import Settings
from ekbs.db import Database
from ekbs.logging import get_logger

log = get_logger(component="session")

#: 同一次提问里，模型扩写出的问法。检索在改写之后才读变体，用任务内变量传过去，
#: 避免并发请求共用一个改写器字段。
_angle_cache: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "ekbs_query_angles", default=None
)
_ANGLE_MAX_CHARS = 80
_ANGLE_LINE = re.compile(r"^(?:\d+\s*[\.、\)]\s*|[-*•]\s*)")
_ANGLE_PROMPT = (
    "你是检索查询扩写助手。根据用户这一个问题，写2条不同角度、但信息需求相同的问法，"
    "用来提高检索召回。\n"
    "只输出2行，每行一条问法。不要编号，不要解释。\n"
    "不要添加问题里没有的人名、制度名、编号。\n"
    "\n"
    "【示例】\n"
    "用户问题：年假有多少天\n"
    "年假天数是如何规定的\n"
    "员工可休年假几天\n"
    "\n"
    "用户问题：{question}"
)

#: 改写 system 角色：规则 + 异形示例（禁止与线上热实体/高频追问同构）+ 输出约束。
#: 真题（历史/最新问题）只放 user，避免 few-shot 与本题抢注意力。
_REWRITE_SYSTEM_PROMPT = (
    "【通用规则】\n"
    "把用户的最新问题改写成一个不依赖上下文、可独立检索的查询词。\n"
    "保留主体（商户/表名/人名）、指标（用电量/用水量等）、年份与月份期间；"
    "不要汇总或计算；不要添加历史里没有的实体或指标；"
    "若最新问题本身已完整自包含，原样保留即可。\n"
    "\n"
    "【示例】\n"
    "【改写】\n"
    "历史一轮：\n"
    "user: 青禾店2024年12月用水量\n"
    "assistant: 108\n"
    "最新问题：2025年啦\n"
    "改写后的查询词：青禾店2025年用水量\n"
    "\n"
    "【扩写】\n"
    "历史一轮：\n"
    "user: （无）\n"
    "assistant: （无）\n"
    "最新问题：财务专用章保管\n"
    "改写后的查询词：财务专用章由谁保管\n"
    "\n"
    "【改扩写】\n"
    "历史一轮：\n"
    "user: 年假有多少天\n"
    "assistant: 5 天 [1]\n"
    "最新问题：那它需要提前几天申请\n"
    "改写后的查询词：年假申请需要提前几天\n"
    "\n"
    "【改写后的查询词】\n"
    "只输出改写后的查询词本身：单行、无解释、无引号、不多行。"
)

_REWRITE_USER_TEMPLATE = (
    "对话历史（最近一轮）：\n{history}\n\n"
    "最新问题：{question}"
)

#: 改写结果里允许出现、不必强制来自历史的计量/时间类词
_REWRITE_METRIC_OK = frozenset({
    "用电量", "用水量", "用气量", "电费", "水费", "气费", "热费", "用量", "金额",
    "合计", "总计", "多少", "几天", "申请", "提前", "需要", "保管", "由谁",
    "是多少", "是什么", "如何", "怎么",
})
_REWRITE_ENTITY_FLAGS = frozenset({"n", "nr", "ns", "nt", "nz", "nw", "eng", "nrt"})

#: 需要上下文的指代词。必须是分词后的整词，不能用子串
#: （「其他」里的「他」、「应该」里的「该」都不是指代）。
_ANAPHORA = frozenset({
    "它", "他", "她", "这个", "那个", "这些", "那些", "上述", "该", "此", "这项", "那条",
})
#: 整词以指代字开头、后面只跟这些尾巴，仍算指代（他们 / 此项 / 该条）。
_ANAPHORA_TAIL = frozenset({"们", "的", "项", "条"})
_ANAPHORA_HEAD = frozenset("它他她该此")
#: 短句已经自带问法时，人名不必再靠上一轮补主题。
_NAME_QUESTION_RE = re.compile(r"是谁|是什么|做什么|叫什么|哪个部门|负责什么")
#: 纯编号：字母数字、点斜杠、空格。没有汉字。
_CODE_RE = re.compile(r"^(?=.*[0-9A-Za-z])[0-9A-Za-z][0-9A-Za-z._/\- ]*$")
#: 文号：可选几个汉字 + 〔2024〕15号。
_DOCNO_RE = re.compile(r"^[\u4e00-\u9fff]{0,8}[〔\[（(]\d{4}[〕\]）)]\s*\d+\s*号?$")

#: 追问/展开类用语：语义上依赖上一轮主题，但未必带指代词
#: （用户实测：「崔超是谁」→「能介绍详细点吗？」启发式原先漏改写）
_FOLLOWUP = (
    "详细", "具体点", "具体一些", "具体说说", "展开", "补充", "继续",
    "再说", "再讲", "再介绍", "更多",
    "介绍一下", "介绍下", "介绍详细",
    "说说看", "讲讲", "还有呢", "然后呢",
)

DEFAULT_MAX_TURNS = 6
DEFAULT_TTL_SECONDS = 7200


#: 意图标签（②「理解意图」的第一步；当前是启发式，`mode` 仍允许调用方覆盖）
INTENT_FACTUAL = "factual"
INTENT_NUMERIC = "numeric"
INTENT_FIGURE = "figure"
INTENT_PROCEDURE = "procedure"
INTENT_COMPARE = "compare"

_NUMERIC_HINTS = (
    "多少", "多少钱", "是多少", "金额", "总价", "单价", "合计", "总计",
    "费用", "报价", "预算", "电价", "用量", "多少度", "多少吨", "多少元",
)
_FIGURE_HINTS = (
    "图片", "照片", "截图", "图纸", "图样", "现场图",
    "发图", "发图片", "把图", "看看图", "图呢", "有没有图",
    "二维码", "二维碼", "发码", "扫码下载",
)
_PROCEDURE_HINTS = ("怎么办", "怎么处理", "如何", "流程", "步骤", "哪些规定", "怎么交接")


@dataclass
class RewriteResult:
    """改写结果。**两类改写分开存放**（这是 2026-10-03 的设计调整）：

    * ① **上下文改写** → :attr:`rewritten`（把"它/这个/详细说说"补成自包含查询）
    * ② **意图理解** → :attr:`intent`
    * 变体**分两侧**：BM25 要**词面变体**（别名/编号/关键词），稠密要**语义变体**（自然语言改写）。
      把同一种变体塞给两侧是错的 —— 关键词碎片喂给嵌入通常更差（实测过）
      。:attr:`variants` 是历史字段，等价于 :attr:`sparse_variants`，保留以免破坏调用方。
    """

    original: str
    rewritten: str
    strategy: str
    used_history: bool = False
    #: 兼容字段：等价于 `sparse_variants`（旧调用方仍在用）
    variants: list[str] = field(default_factory=list)
    #: ② 意图（factual / numeric / figure / procedure）
    intent: str = INTENT_FACTUAL
    #: 词面变体（BM25 用）：含 rewritten，长度由 `retrieval.multi_query` 控制
    sparse_variants: list[str] = field(default_factory=list)
    #: 语义变体（稠密用）：自然语言改写，长度由 `retrieval.dense_multi_query` 控制
    dense_variants: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return self.rewritten.strip() != self.original.strip()


class SessionStore:
    """``ekbs_session_ctx`` 的读写（键含用户维度，防群聊越权）。"""

    def __init__(self, db: Database, settings: Settings) -> None:
        self.db = db
        self.settings = settings

    @property
    def max_turns(self) -> int:
        return int(self.settings.session.get("context_max_turns", DEFAULT_MAX_TURNS) or 0)

    @property
    def ttl_seconds(self) -> int:
        return int(self.settings.session.get("context_ttl_seconds", DEFAULT_TTL_SECONDS) or 0)

    # ------------------------------------------------------------------

    def load(self, conversation_id: str, dingtalk_userid: str) -> list[dict[str, str]]:
        """取该用户在该会话里的历史消息（已按 TTL 与轮数裁剪）。"""
        if not conversation_id or not dingtalk_userid:
            return []
        with self.db.sync_connect() as conn:
            row = conn.execute(
                "SELECT payload, updated_at, "
                "       (strftime('%s','now') - strftime('%s', updated_at)) AS age "
                "FROM ekbs_session_ctx WHERE conversation_id = ? AND dingtalk_userid = ?",
                (conversation_id, dingtalk_userid),
            ).fetchone()
        if row is None:
            return []
        if self.ttl_seconds and int(row["age"] or 0) > self.ttl_seconds:
            log.info(
                "session_context_expired",
                conversation_id=conversation_id, age_s=int(row["age"] or 0),
            )
            return []
        try:
            payload = json.loads(row["payload"] or "[]")
        except ValueError:
            return []
        messages = [
            {"role": str(item.get("role", "")), "content": str(item.get("content", ""))}
            for item in payload
            if isinstance(item, dict)
        ]
        # 只保留最近 N 轮（一轮 = 用户 + 助手两条）
        limit = self.max_turns * 2 if self.max_turns else len(messages)
        return messages[-limit:] if limit else messages

    def append(
        self,
        conversation_id: str,
        dingtalk_userid: str,
        messages: Sequence[dict[str, str]],
    ) -> int:
        """把本轮问答追加进上下文并返回当前消息数（超限自动裁剪）。"""
        if not conversation_id or not dingtalk_userid or not messages:
            return 0
        history = self.load(conversation_id, dingtalk_userid)
        history.extend(dict(m) for m in messages)
        limit = self.max_turns * 2 if self.max_turns else len(history)
        if limit:
            history = history[-limit:]
        payload = json.dumps(history, ensure_ascii=False)
        turn = len(history)
        with self.db.sync_connect() as conn:
            conn.execute(
                "INSERT INTO ekbs_session_ctx(conversation_id, dingtalk_userid, turn, payload, "
                "updated_at) VALUES (?, ?, ?, ?, datetime('now')) "
                "ON CONFLICT(conversation_id, dingtalk_userid) DO UPDATE SET "
                "turn = excluded.turn, payload = excluded.payload, updated_at = excluded.updated_at",
                (conversation_id, dingtalk_userid, turn, payload),
            )
            conn.commit()
        return turn

    def clear(self, conversation_id: str, dingtalk_userid: str) -> int:
        with self.db.sync_connect() as conn:
            cursor = conn.execute(
                "DELETE FROM ekbs_session_ctx WHERE conversation_id = ? AND dingtalk_userid = ?",
                (conversation_id, dingtalk_userid),
            )
            conn.commit()
            return int(cursor.rowcount or 0)

    def cleanup_expired(self) -> int:
        """由 ``com.ekbs.scheduler`` 调用（`01` §5.5 的会话清理）。"""
        cutoff = self.ttl_seconds or DEFAULT_TTL_SECONDS
        with self.db.sync_connect() as conn:
            cursor = conn.execute(
                "DELETE FROM ekbs_session_ctx "
                "WHERE strftime('%s','now') - strftime('%s', updated_at) > ?",
                (cutoff,),
            )
            conn.commit()
            return int(cursor.rowcount or 0)


@dataclass
class QueryRewriter:
    """查询改写（策略可配，默认启发式）。"""

    settings: Settings
    llm: Any | None = None
    #: 快速模型，只负责把一条问法扩成两个角度。不参与回答，也不读对话历史。
    expand_llm: Any | None = None
    #: 兼容旧测试/调试：完整可见模板（system + user 占位）；真正调用走分角色消息。
    prompt_template: str = _REWRITE_SYSTEM_PROMPT + "\n\n" + _REWRITE_USER_TEMPLATE
    system_prompt: str = _REWRITE_SYSTEM_PROMPT
    user_template: str = _REWRITE_USER_TEMPLATE
    enabled: bool = True
    strategy_override: str | None = None
    _last: RewriteResult | None = field(default=None, init=False, repr=False)

    @property
    def strategy(self) -> str:
        if self.strategy_override:
            return self.strategy_override
        cfg = (self.settings.retrieval.model_extra or {}).get("query_rewrite") or {}
        return str(cfg.get("strategy", "heuristic"))

    async def rewrite(
        self, question: str, history: Sequence[dict[str, str]] = ()
    ) -> RewriteResult:
        _angle_cache.set(None)
        result = await self._rewrite_context(question, history)
        await self._maybe_replace_with_model_angles(result)
        return result

    async def _rewrite_context(
        self, question: str, history: Sequence[dict[str, str]] = ()
    ) -> RewriteResult:
        """多轮只决定要不要把上一句用户原话补进检索串。扩写角度不在这一步。"""
        if not self.enabled or not history:
            result = RewriteResult(question, question, "off", used_history=False)
            return self._fill_variants(result)

        previous_question = self._last_user_question(history)
        if not previous_question:
            result = RewriteResult(question, question, self.strategy, used_history=False)
            return self._fill_variants(result)

        strategy = self.strategy
        if strategy == "off":
            result = RewriteResult(question, question, "off", used_history=False)
            return self._fill_variants(result)
        # llm：只走模型改写；失败/mock/无模型 → **原文进检索**，绝不回退 heuristic。
        if strategy == "llm":
            if self.llm is None or _is_mock(self.llm):
                result = RewriteResult(
                    question, question, "llm_skipped", used_history=False
                )
                return self._fill_variants(result)
            rewritten = await self._rewrite_with_llm(question, history)
            if rewritten:
                allowed = _rewrite_allowed_text(question, history)
                if _rewrite_has_unknown_entity(rewritten, allowed):
                    log.warning(
                        "query_rewrite_guard_reject",
                        rewritten=rewritten[:80],
                        question=question[:80],
                    )
                    guarded = _merge_followup(
                        previous_question, question, _history_year(history)
                    )
                    if (
                        guarded != question
                        and not _rewrite_has_unknown_entity(guarded, allowed)
                    ):
                        result = RewriteResult(
                            question, guarded, "llm_guarded", used_history=True
                        )
                        return self._fill_variants(result)
                    result = RewriteResult(
                        question, question, "llm_rejected", used_history=False
                    )
                    return self._fill_variants(result)
                result = RewriteResult(question, rewritten, "llm", used_history=True)
                return self._fill_variants(result)
            result = RewriteResult(question, question, "llm_failed", used_history=False)
            return self._fill_variants(result)

        # 启发式：期间追问做槽位合并；其它上下文依赖才并入上一轮。
        merged = _merge_followup(previous_question, question, _history_year(history))
        if merged != question:
            result = RewriteResult(question, merged, "heuristic", used_history=True)
            return self._fill_variants(result)
        result = RewriteResult(question, question, "heuristic", used_history=False)
        return self._fill_variants(result)

    async def _maybe_replace_with_model_angles(self, result: RewriteResult) -> None:
        """用快速模型写 2 个角度。失败则保留规则变体。提示词里不放对话历史。"""
        llm = self.expand_llm
        if llm is None or _is_mock(llm):
            return
        cap = self._variant_limit("multi_query", None)
        if cap <= 1:
            return
        primary = (result.rewritten or result.original).strip()
        if not primary:
            return
        try:
            raw = await self._generate_angles(llm, primary)
        except Exception as exc:
            log.warning("query_expand_failed", error=str(exc)[:200])
            return
        angles = _accept_angles(raw, primary, limit=max(0, cap - 1))
        if not angles:
            log.info("query_expand_empty", query_len=len(primary))
            return
        shared = [primary, *angles][:cap]
        result.sparse_variants = shared
        result.dense_variants = list(shared)
        result.variants = list(shared)
        _angle_cache.set({"query": primary, "shared": shared})
        log.info("query_expand_ready", count=len(shared), model=getattr(getattr(llm, "ref", None), "model", ""))

    async def _generate_angles(self, llm: Any, question: str) -> str:
        prompt = _ANGLE_PROMPT.format(question=question)
        generated = await llm.generate([{"role": "user", "content": prompt}])
        return str(getattr(generated, "text", "") or "")

    # ------------------------------------------------------------------
    # 多轮改写用的两个助手
    #
    # ⚠️ 它们必须留在**类体内**。上一轮编辑把它们缩进到了模块级函数
    #    `_keyword_focus` 的 `return` 之后 —— 于是既成了不可达的死代码，
    #    又没挂到 `QueryRewriter` 上；任何**带历史**的多轮改写都会抛
    #    `AttributeError: 'QueryRewriter' object has no attribute '_last_user_question'`
    #    （12 个用例同时红，且线上多轮问答会直接失败）。
    #    以后再挪这两个方法，请确认缩进层级：4 空格 = 类方法，别落进别的函数体。
    # ------------------------------------------------------------------

    @staticmethod
    def _last_user_question(history: Sequence[dict[str, str]]) -> str:
        for message in reversed(list(history)):
            if str(message.get("role")) == "user" and str(message.get("content", "")).strip():
                return str(message["content"]).strip()
        return ""

    async def _rewrite_with_llm(self, question: str, history: Sequence[dict[str, str]]) -> str:
        # 只喂最近 1 轮（通常 user+assistant 共 2 条；不足则有多少用多少）
        rendered = "\n".join(f"{m.get('role')}: {m.get('content')}" for m in list(history)[-2:])
        user_content = self.user_template.format(history=rendered, question=question)
        messages = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": user_content},
        ]
        try:
            result = await self.llm.generate(messages)
        except Exception as exc:
            log.warning("query_rewrite_failed", error=str(exc))
            return ""
        text = (result.text or "").strip().splitlines()
        return text[0].strip() if text else ""

    def _variant_limit(self, key: str, explicit: int | None) -> int:
        """变体条数：**显式 limit 优先**，否则读配置。

        为什么必须支持显式 limit：检索调试台用 `RetrievalOverrides` 按请求调参，
        若这里再按**进程级配置**截断一次，override 就被悄悄吞掉（实测：台面上调成 3，
        实际只出 1 条变体）。
        """
        value = explicit if explicit is not None else getattr(self.settings.retrieval, key, 1)
        return max(1, min(int(value or 1), 5))

    def expand_dense_variants(
        self, primary: str, *, original: str | None = None, limit: int | None = None
    ) -> list[str]:
        """**语义变体**（稠密召回用）：自然语言改写，不是关键词碎片。

        为什么不复用 `expand_variants`（那是给 BM25 的）：`_keyword_focus` 之类的产物
        被嵌入模型编码后语义会被拉偏（实测过"关键词碎片喂嵌入更差"）。这里只做
        **语义等价的自然语言改写**（去口语外壳 / 补成完整问句 / 保留用户原始措辞）。
        """
        cap = self._variant_limit("dense_multi_query", limit)
        cached = _cached_angles(primary)
        if cached is not None:
            return cached[:cap] or [(primary or "").strip() or ""]
        variants: list[str] = []
        for candidate in (
            (primary or "").strip(),          # 改写后的自包含查询（首选）
            (original or "").strip(),         # 用户原始措辞（可能更贴文档用词）
            _strip_particles(primary),        # 去掉"请问/帮我查一下"这类外壳
            _as_question(primary),            # 补成完整问句
        ):
            if not candidate or candidate in variants:
                continue
            variants.append(candidate)
            if len(variants) >= cap:
                break
        return variants or [(primary or "").strip() or ""]

    @staticmethod
    def detect_intent(question: str) -> str:
        """启发式意图识别（② 的第一步；调用方仍可用 `mode` 覆盖）。

        只做**保守**判断：命中明确的取数/找图/流程词才给标签，否则一律 `factual` ——
        误判比不判更糟（取数路径有独立提示词与更严的校验）。

        比较/排名（「谁更高」「最高」）优先于 numeric：否则「9月用电量谁更高」会因
        「用量」命中取数路径，而取数铁律禁止计算，全局排序永远做不成。
        """
        from ekbs.retrieval.table_rank import is_compare_rank_intent

        text = (question or "").strip()
        if any(hint in text for hint in _FIGURE_HINTS):
            return INTENT_FIGURE
        if is_compare_rank_intent(text):
            return INTENT_COMPARE
        if any(hint in text for hint in _NUMERIC_HINTS):
            return INTENT_NUMERIC
        if any(hint in text for hint in _PROCEDURE_HINTS):
            return INTENT_PROCEDURE
        return INTENT_FACTUAL

    def _fill_variants(self, result: RewriteResult) -> RewriteResult:
        """把三类字段一次填好（保持 `variants` 与 `sparse_variants` 同步）。"""
        result.intent = self.detect_intent(result.rewritten or result.original)
        result.sparse_variants = self.expand_variants(
            result.rewritten, original=result.original
        )
        result.variants = list(result.sparse_variants)  # 兼容
        result.dense_variants = self.expand_dense_variants(
            result.rewritten, original=result.original
        )
        return result

    def expand_variants(
        self, primary: str, *, original: str | None = None, limit: int | None = None
    ) -> list[str]:
        """MultiQuery：在主查询外再扩若干语义近邻问句（稀疏召回用）。

        不调 LLM：用「去语气词 / 补问句尾 / 保留原问」等确定性变体，
        数量由 ``retrieval.multi_query`` 控制（1 = 只用主查询）。
        """
        cap = self._variant_limit("multi_query", limit)
        cached = _cached_angles(primary)
        if cached is not None:
            return cached[:cap] or [(primary or "").strip() or ""]
        variants: list[str] = []
        for candidate in (
            (primary or "").strip(),
            (original or "").strip(),
            _strip_particles(primary),
            _as_question(primary),
            _keyword_focus(primary),
        ):
            if not candidate or candidate in variants:
                continue
            variants.append(candidate)
            if len(variants) >= cap:
                break
        return variants or [(primary or "").strip() or ""]


def _cached_angles(primary: str) -> list[str] | None:
    cached = _angle_cache.get()
    if not cached:
        return None
    if str(cached.get("query") or "") != (primary or "").strip():
        return None
    shared = [str(item).strip() for item in cached.get("shared") or [] if str(item).strip()]
    return shared or None


def _accept_angles(raw: str, primary: str, *, limit: int) -> list[str]:
    """留下合格的扩写行：去编号、去空行、丢掉和原问相同或过长的行。"""
    primary_key = _norm_query(primary)
    accepted: list[str] = []
    for line in (raw or "").splitlines():
        cleaned = _ANGLE_LINE.sub("", line.strip()).strip("「」\"' ")
        if not cleaned or len(cleaned) > _ANGLE_MAX_CHARS:
            continue
        if _norm_query(cleaned) == primary_key or cleaned in accepted:
            continue
        accepted.append(cleaned)
        if len(accepted) >= limit:
            break
    return accepted


def _norm_query(text: str) -> str:
    return re.sub(r"\s+", "", (text or "").strip().lower())


def _strip_particles(text: str) -> str:
    cleaned = re.sub(
        r"(请问|麻烦问一下|想问一下|帮我查一下|帮我看看|能不能告诉我|是否可以)",
        "",
        text or "",
    )
    return cleaned.strip(" ，,。？?！!")


def _as_question(text: str) -> str:
    base = (text or "").strip().rstrip("？?。！!")
    if not base:
        return ""
    if base.endswith(("吗", "呢", "吧")):
        return base + "？"
    return base + "是什么？"


def _keyword_focus(text: str) -> str:
    """去掉常见口语外壳，留下可能更贴 chunk 陈述的关键词串。"""
    cleaned = _strip_particles(text)
    cleaned = re.sub(r"(是多少|有多少|怎么办|怎么处理|如何规定|的规定)", "", cleaned)
    return cleaned.strip(" ，,。？?！!")


def _rewrite_allowed_text(question: str, history: Sequence[dict[str, str]]) -> str:
    """改写校验语料：最近一轮历史 ∪ 当前原话。"""
    parts = [str(m.get("content", "") or "") for m in list(history)[-2:]]
    parts.append(question or "")
    return "\n".join(parts)


def _rewrite_has_unknown_entity(rewritten: str, allowed: str) -> bool:
    """改写结果是否引入了历史∪原话里没有的主体词（防 few-shot 抄实体）。"""
    text = (rewritten or "").strip()
    if not text:
        return True
    haystack = allowed or ""
    for word, flag in _pos_tokens(text):
        token = (word or "").strip()
        if len(token) < 2:
            continue
        if token.isdigit() or re.fullmatch(r"20\d{2}年?", token):
            continue
        if token in _REWRITE_METRIC_OK:
            continue
        if _token_is_anaphora(token):
            continue
        if _PERIOD_FOLLOW_RE.fullmatch(token) or _YEAR_RE.fullmatch(token):
            continue
        is_entity = flag in _REWRITE_ENTITY_FLAGS or (
            flag.startswith("n") and len(token) >= 2
        )
        if not is_entity:
            continue
        if token not in haystack:
            return True
    return False


def _pos_tokens(text: str) -> list[tuple[str, str]]:
    """jieba 词和词性。先加载用户词典，和检索分词用同一套词。"""
    import jieba.posseg as pseg

    from ekbs.index.fts import ensure_userdict

    ensure_userdict()
    return [(pair.word, pair.flag) for pair in pseg.cut(text or "") if pair.word.strip()]


def _token_is_anaphora(word: str) -> bool:
    if word in _ANAPHORA:
        return True
    return (
        len(word) >= 2
        and word[0] in _ANAPHORA_HEAD
        and word[1:] in _ANAPHORA_TAIL
    )


def _has_anaphora(text: str) -> bool:
    return any(_token_is_anaphora(word) for word, _flag in _pos_tokens(text))


def _is_standalone_lookup(text: str) -> bool:
    """人名或编号本身就能检索，不必把上一轮主题拼进来。"""
    stripped = (text or "").strip().strip("？?。！!，,、 ")
    if not stripped:
        return False
    if _CODE_RE.fullmatch(stripped) or _DOCNO_RE.fullmatch(stripped.replace(" ", "")):
        return True
    content = [
        (word, flag)
        for word, flag in _pos_tokens(stripped)
        if flag not in {"x", "w", "y"}
    ]
    if content and all(flag == "nr" for _word, flag in content):
        return True
    return any(flag == "nr" for _word, flag in content) and bool(
        _NAME_QUESTION_RE.search(stripped)
    )


_PERIOD_FOLLOW_RE = re.compile(
    r"(?:20\d{2}\s*年\s*)?(?:\d{1,2}\s*[-—–~～至到]\s*\d{1,2}\s*月|\d{1,2}\s*月|"
    r"Q[1-4]|第?[一二三四]季度|上半年|下半年|YTD|年初至今|全年)"
)
_QUESTION_TAIL_RE = re.compile(r"(是多少|多少|几|合计|总计|总共|多大|呢|啦|吗|？|\?)+$")
_YEAR_RE = re.compile(r"20\d{2}\s*年")
_METRIC_TAIL_RE = re.compile(
    r"(用电量|用水量|用气量|电费|水费|气费|热费|用量|金额)$"
)
_SOLICIT_HEAD_RE = re.compile(r"^(提供|给我|查一下|看看|发一下|再给|换成|换成)")


def _period_of(text: str) -> str:
    match = _PERIOD_FOLLOW_RE.search(text or "")
    return match.group(0).strip() if match else ""


def _history_year(history: Sequence[dict[str, str]]) -> str:
    for message in reversed(list(history)):
        match = _YEAR_RE.search(str(message.get("content", "") or ""))
        if match:
            return match.group(0)
    return ""


def _split_entity_metric(previous: str) -> tuple[str, str]:
    """上一问去掉年/期间/尾问后，拆成（主体, 指标）。"""
    base = _QUESTION_TAIL_RE.sub("", previous or "").strip()
    base = _YEAR_RE.sub("", base)
    base = _PERIOD_FOLLOW_RE.sub("", base)
    base = re.sub(r"\s+", "", base).strip(" ，,的")
    match = _METRIC_TAIL_RE.search(base)
    if match:
        return base[: match.start()].strip(), match.group(1)
    return base, ""


def _is_incomplete_period_ask(question: str) -> bool:
    """「提供一下2026年1-3月的」：有期间，但缺可检索主体/指标。"""
    text = (question or "").strip()
    if not text or not _period_of(text):
        return False
    if _SOLICIT_HEAD_RE.match(text):
        return True
    if text.endswith(("的", "呢", "吧")) and not _METRIC_TAIL_RE.search(
        _QUESTION_TAIL_RE.sub("", text)
    ):
        return True
    return False


def _compose_topic_period(
    entity: str, metric: str, year: str, period: str
) -> str:
    period_body = _YEAR_RE.sub("", period or "").strip()
    parts = [entity, year, period_body, metric]
    return "".join(p for p in parts if p) + "是多少"


def _merge_followup(previous: str, question: str, fallback_year: str = "") -> str:
    """把「那 1-5 月啦」这类期间追问合并成可独立检索的一句话。

    旧逻辑直接把上一问和追问用空格拼起来，产出
    ``2026年公司承担电费是多少 那1-5月啦``；检索/计划都不可用。
    """
    if not previous:
        return question
    current_period = _period_of(question)
    previous_year_match = _YEAR_RE.search(previous)
    previous_year_text = previous_year_match.group(0) if previous_year_match else fallback_year
    current_has_year = bool(_YEAR_RE.search(question))
    # 「提供一下2026年1-3月的」：带走上一问的主体+指标，换上当前年/期间
    if _is_incomplete_period_ask(question):
        entity, metric = _split_entity_metric(previous)
        year_m = _YEAR_RE.search(question)
        year_t = year_m.group(0) if year_m else previous_year_text
        if entity or metric:
            return _compose_topic_period(entity, metric, year_t, current_period)
    # 只补期间、没有主题：用上一问的主体 + 当前期间。
    if _needs_context(question) and current_period and not current_has_year:
        base = _QUESTION_TAIL_RE.sub("", previous).strip()
        if base:
            return f"{base}{current_period}是多少"
    # 自包含但漏了年份：只对带期间的追问补上一轮年份。
    if current_period and not current_has_year and previous_year_text:
        return f"{previous_year_text}{question}"
    # 「新东方的」：短主体追问，继承上一问的年/期间/指标
    if _needs_context(question) and not current_period:
        entity_now = _QUESTION_TAIL_RE.sub("", question).strip().rstrip("的")
        cjk_n = len(re.findall(r"[\u4e00-\u9fff]", entity_now))
        if 1 <= cjk_n <= 6:
            _prev_entity, metric = _split_entity_metric(previous)
            year_t = previous_year_text
            period = _period_of(previous)
            if metric or period:
                return _compose_topic_period(entity_now, metric, year_t, period)
    if _needs_context(question):
        return f"{previous} {question}"
    return question


def _needs_context(question: str) -> bool:
    """问题是否依赖上下文。

    触发条件（任一即可）：

    * 分词后的整词是指代（它/他/这个/上述…），「其他」「应该」不算；
    * 含追问展开用语（详细/继续/再说…）—— 「能介绍详细点吗？」这类不带指代；
    * 过短（CJK < 6），而且不是人名或编号。
    """
    text = (question or "").strip()
    if not text:
        return False
    if _has_anaphora(text):
        return True
    if any(marker in text for marker in _FOLLOWUP):
        return True
    cjk = re.findall(r"[\u4e00-\u9fff]", text)
    if len(cjk) >= 6:
        return False
    return not _is_standalone_lookup(text)


def _is_mock(provider: Any) -> bool:
    ref = getattr(provider, "ref", None)
    return bool(ref is not None and str(getattr(ref, "provider", "")).startswith("mock"))
