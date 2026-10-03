"""配置加载 —— `01` §7 的 ``application.yaml`` + ``~/.ekbs/secrets.env``。

三条纪律（都能在本模块里找到实现，改这里之前先读）：

1. **application.yaml 是非敏感配置**：本模块对它做**反向检查** —— 一旦发现密钥类键名
   即 :class:`~ekbs.errors.ConfigError` 报错退出，而不是忽略（`04` §2.6）。
2. **密钥只从环境变量来**：``~/.ekbs/secrets.env`` 由 launchd 的
   ``zsh -lc 'set -a; source …; set +a; exec …'`` 注入（`01` §8.1）。:class:`Secrets`
   只读 ``os.environ``，且 :meth:`Secrets.redacted` 保证日志/接口里只出现「是否已配置」。
3. **硬口径在代码里也拦一道**：``server.workers`` 必须为 1（`04` §7.2，多 worker 会出现
   ≤300s 的旧权限窗口）；``host`` 非回环在非 dev 环境直接拒绝启动。
"""

from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from ekbs.errors import ConfigError

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

#: 密钥类键名的反向检查规则。
#: 只匹配「整段等于」的密钥词，避免误伤 application.yaml 里的合法非敏感键：
#: ``token_cache_key``（缓存键模板）、``token_refresh_ahead_seconds``、
#: ``corpus_egress_ack_ticket``（合规确认编号，是编号不是密钥）。
_SECRET_KEY_RE = re.compile(
    r"(?i)(?:^|[._-])("
    r"api_?key|apikey|secret|app_?secret|client_?secret|password|passwd|"
    r"private_?key|jwt_?secret|access_?token|refresh_?token|internal_?token"
    r")(?:$|[._-])"
)

#: ``secrets.env`` 的键名清单（`01` §0.2 + `04` §2.6）。
SECRET_KEYS = (
    "DASHSCOPE_API_KEY",
    "DEEPSEEK_API_KEY",
    "DINGTALK_CLIENT_ID",
    "DINGTALK_CLIENT_SECRET",
    "DINGTALK_ROBOT_CODE",
    "DINGTALK_CORP_ID",
    "JWT_SECRET",
    "EKBS_INTERNAL_TOKEN",
)

#: 同一密钥在文档里出现过两个写法时，按顺序都接受。
#: ⚠️ 口径冲突：`01` §7 的示例写 ``JWT_SECRET=``，而 ``deploy/config/secrets.env.example``
#: 写 ``EKBS_JWT_SECRET=``。两者都认，避免「按文档填了却读不到」这类静默故障。
SECRET_ENV_ALIASES: dict[str, tuple[str, ...]] = {
    "JWT_SECRET": ("EKBS_JWT_SECRET", "JWT_SECRET"),
}

#: ``EkbsConfig`` 环境变量：指向配置文件本身。
CONFIG_ENV_VAR = "EKBS_CONFIG"


# ---------------------------------------------------------------------------
# 配置段
# ---------------------------------------------------------------------------


class _Section(BaseModel):
    """所有配置段的基类：允许多余键（下游文档可能先加键），但不允许密钥键。"""

    model_config = ConfigDict(extra="allow", protected_namespaces=())


class AppCfg(_Section):
    name: str = "ekbs"
    env: str = "pilot"
    timezone: str = "Asia/Shanghai"
    log_dir: str = "/opt/ekbs/log"
    data_dir: str = "/data/ekbs"


class ServerCfg(_Section):
    host: str = "127.0.0.1"
    port: int = 8000
    workers: int = 1

    @field_validator("workers")
    @classmethod
    def _workers_must_be_one(cls, v: int) -> int:
        # 04 §1.5 / §7.2：权限缓存的进程内 L1 靠同进程同步失效
        if v != 1:
            raise ValueError("server.workers 必须为 1（04 §1.5 / §7.2）")
        return v


class DatabaseCfg(_Section):
    path: str = "/data/ekbs/index/ekbs.db"
    wal: bool = True


class RerankCfg(_Section):
    enabled: bool = True
    provider: str = "dashscope"
    model: str = "gte-rerank-v2"
    #: 共享域名；需工作空间专属域名时覆盖为 https://{WorkspaceId}.cn-beijing.maas.aliyuncs.com
    #: ⚠️ gte-rerank-v2 官方公告 2026-05-30 下线，换 qwen3-rerank 时改 model 即可（端点相同）
    base_url: str = "https://dashscope.aliyuncs.com"
    top_n: int = 15  # 输入候选数
    keep: int = 5  # 输出条数
    min_score: float = 0.30  # 低于此分视为无有效命中 → 拒答


class RetrievalCacheCfg(_Section):
    backend: str = "memory+sqlite"
    l1_size: int = 2000
    query_embedding_ttl: int = 86400
    retrieve_ttl: int = 1800
    answer_ttl: int = 3600


class RetrievalCfg(_Section):
    dense_top_k: int = 30
    sparse_top_k: int = 30
    rrf_k: int = 60
    rerank: RerankCfg = Field(default_factory=RerankCfg)
    parent_expand: bool = True
    cache: RetrievalCacheCfg = Field(default_factory=RetrievalCacheCfg)
    #: 融合策略：``rrf``（默认，只用名次）| ``alpha``（α·向量 + (1-α)·稀疏，课程 18）
    fusion: str = "rrf"
    #: ``fusion=alpha`` 时的向量权重；0=纯 BM25，1=纯向量，0.5=平衡
    hybrid_alpha: float = 0.5
    #: 入库时为可检索块生成假设问句写入 FTS（确定性 Doc2Query，不写 FAISS）
    doc2query_enabled: bool = True
    #: 每个块最多写几条假设问句
    doc2query_max: int = 4
    #: MultiQuery：稀疏召回并行的改写问句数（1=关闭；建议 3）
    multi_query: int = 1
    #: **稠密侧**多查询数（1 = 关，保持原有单查询行为）。为什么与 `multi_query` 分开：
    #: 两侧需要的"变体"不是同一种东西 —— BM25 要词面变体（别名/编号/关键词），
    #: 稠密要语义变体（自然语言改写）。分开才能独立 A/B，且默认不改变生产行为。
    dense_multi_query: int = 1
    #: figure 图片进多模态旁路索引，检索时作为 RRF 第三路（课程 17）
    figure_multimodal: bool = True


class EmbedCfg(_Section):
    provider: str = "dashscope"
    model: str = "text-embedding-v4"
    base_url: str = "https://dashscope.aliyuncs.com"
    dim: int = 1024
    #: 00 §3.1 的模型标识是 ``<provider>/<model>@<version>#<dim>``，但配置文件里没有 version 键。
    #: 未显式给出时由 :func:`ekbs.provider.base.derive_version` 从 model 名推导（如 text-embedding-v4 → v4）。
    version: str | None = None
    batch_size: int = 16
    timeout_s: int = 30
    max_retries: int = 3
    #: 查询/文档**不对称编码**（DashScope `text_type`）：查询用 `query`、入库用 `document`。
    #: 实测该参数生效（`document` 与默认向量余弦 0.916）。**开启会改变全部 chunk 向量** ⇒
    #: 必须全量重灌；指纹会带上 `+asym`（见 registry），避免新旧向量混用（C3）。
    asymmetry: bool = False


class MultimodalEmbedCfg(_Section):
    """图片/查询共用的多模态嵌入（与 ``models.embed`` 维度不同，必须旁路 FAISS）。"""

    provider: str = "mock"
    model: str = "mock-mm-embed-v1"
    #: ``tongyi-embedding-vision-plus`` = 1152；mock 默认 64 减轻测试负担
    dim: int = 64
    base_url: str = "https://dashscope.aliyuncs.com"
    version: str | None = None
    timeout_s: float = 60.0
    #: 单张图向量化失败的重试次数。实测该端点会偶发连接错误/限流
    #: （`HTTPSConnectionPool(host='dashscope.aliyuncs.com')`）：旧实现一次失败就跳过 →
    #: 图片进库却没有向量（「存了但搜不到」），文档照样 published。
    max_retries: int = 3
    #: 重试退避基数（秒）：第 n 次重试等待 `retry_backoff_s * n`
    retry_backoff_s: float = 1.0


class LLMFallbackCfg(_Section):
    provider: str = ""
    model: str = ""
    #: 留空则按 provider 推导（deepseek → https://api.deepseek.com）
    base_url: str = ""


class LLMCfg(_Section):
    provider: str = "dashscope"
    model: str = "qwen-max"
    base_url: str = "https://dashscope.aliyuncs.com"
    version: str | None = None
    temperature: float = 0.1
    top_p: float = 0.8
    max_tokens: int = 2048
    #: **输入**（prompt）token 上限：0 = 不限制。为什么必须有它：资料块 + 多轮记忆 +
    #: 问题是一起塞给模型的，而模型窗口有限（qwen-max 最大输入 30,720）。实测最坏情况
    #: （5 个表格块 × 2048 + 10 轮 × (答 2048 + 问 2000)）≈ 40k+ ⇒ 不设上限会直接超窗。
    #: 取值按"本地 Qwen 计数 × 最坏偏差比 1.19 < 模型上限"来留余量（见 yaml 注释）。
    max_input_tokens: int = 0
    timeout_s: int = 120
    max_retries: int = 2
    fallback: LLMFallbackCfg = Field(default_factory=LLMFallbackCfg)


class ModelsCfg(_Section):
    embed: EmbedCfg = Field(default_factory=EmbedCfg)
    llm: LLMCfg = Field(default_factory=LLMCfg)
    #: **视觉模型**：给文档里的图片生成一句描述（`06` §4.11 的"图内文字/图意进索引"）。
    #: 独立于 `llm`：那是 qwen-max（纯文本），这是 qwen-vl-*（能看图）。
    #: ⚠️ 默认 ``model=""`` = **不启用**：它会计费且把图片发到云端，必须显式配置才生效。
    #: （不能直接复用 `LLMCfg()` 的默认值 —— 那个默认是 `qwen-max`，会让未配置的部署
    #: 一启动就去要密钥、并在开工时把图片发给一个纯文本模型。）
    vision: LLMCfg = Field(default_factory=lambda: LLMCfg(model=""))
    #: **多模态嵌入**（figure 旁路索引，课程 17）。与文本 embed 维度不同。
    multimodal_embed: MultimodalEmbedCfg = Field(default_factory=MultimodalEmbedCfg)


class ParseCfg(_Section):
    engine: str = "mineru"
    max_file_mb: int = 500
    timeout_s: int = 1800
    worker_concurrency: int = 1
    keep_tmp_days: int = 7
    #: 是否给文档里的图片生成描述（**会出网、会计费**，默认关）。
    #: 打开后：每张图调一次视觉模型，描述写进图片块的检索文本；
    #: 描述按**图片内容哈希**缓存，重复解析/重复上传不会二次计费。
    figure_caption: bool = False
    #: 图注视觉调用并发（仅未命中缓存的图；MinerU 仍串行）。默认 1。
    figure_caption_concurrency: int = 1


class UploadCfg(_Section):
    """上传行为（`方案 05` §6.5 的 `upload.*`）。

    这两个值都是**给前端用的默认值**，由 `/import/meta` 报给界面：
    ``chunk_size_mb`` 决定分片大小（界面按它切片），``concurrent`` 决定同时上传几个文件。
    放在服务端而不是写死在界面里，是为了"改配置即生效"，不用重新构建前端。
    """

    chunk_size_mb: int = 8               # 方案 §6.5：固定 8 MB，可由系统参数调整
    concurrent: int = 3                  # 方案 §6.5：默认并发 3
    max_retry: int = 3                   # 方案 §6.5：失败分片指数退避重试 3 次


class PermissionCfg(_Section):
    default_level: int = 2
    l4_enabled: bool = False
    l3_ingest_enabled: bool = False
    policy_cache_ttl: int = 300
    #: 合规闸（`00` §7）：云端嵌入 = 全量语料出网
    corpus_egress_ack: bool = False
    corpus_egress_ack_ticket: str = ""
    #: dept_code → 钉钉部门 ID 列表（O6-1，权威方案未定义存放位置，暂由配置承载）
    dept_code_map: dict[str, list[str]] = Field(default_factory=dict)


class AdapterCfg(_Section):
    host: str = "127.0.0.1"
    port: int = 8100
    graceful_shutdown_seconds: int = 30
    api_base: str = "http://127.0.0.1:8000"
    qa_stream_timeout_seconds: int = 180
    request_timeout_seconds: int = 30


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


class Settings(BaseSettings):
    """``application.yaml`` 的强类型视图。

    环境变量覆盖用双下划线表示层级：``EKBS_SERVER__PORT=9000``（开发/测试便利），
    配置文件路径由 ``EKBS_CONFIG`` 指定。
    """

    model_config = SettingsConfigDict(
        env_prefix="EKBS_",
        env_nested_delimiter="__",
        extra="ignore",
        protected_namespaces=(),
    )

    app: AppCfg = Field(default_factory=AppCfg)
    server: ServerCfg = Field(default_factory=ServerCfg)
    database: DatabaseCfg = Field(default_factory=DatabaseCfg)
    retrieval: RetrievalCfg = Field(default_factory=RetrievalCfg)
    models: ModelsCfg = Field(default_factory=ModelsCfg)
    parse: ParseCfg = Field(default_factory=ParseCfg)
    upload: UploadCfg = Field(default_factory=UploadCfg)
    permission: PermissionCfg = Field(default_factory=PermissionCfg)
    dingtalk: dict[str, Any] = Field(default_factory=dict)
    adapter: AdapterCfg = Field(default_factory=AdapterCfg)
    session: dict[str, Any] = Field(default_factory=dict)
    limits: dict[str, Any] = Field(default_factory=dict)
    security: dict[str, Any] = Field(default_factory=dict)

    #: 配置文件与仓库根目录（由 :func:`load_settings` 填），供相对路径解析与 SQL 定位
    config_path: Path = Field(default=Path("config/application.dev.yaml"), exclude=True)
    repo_root: Path = Field(default=Path("."), exclude=True)

    # ---- 路径 ----

    @property
    def data_dir(self) -> Path:
        return Path(self.app.data_dir)

    @property
    def log_dir(self) -> Path:
        return Path(self.app.log_dir)

    @property
    def db_path(self) -> Path:
        return Path(self.database.path)

    @property
    def schema_sql_path(self) -> Path:
        """权威 Schema 文件（`01` §5 / §5.7）。

        按候选顺序解析，**不能只推一个位置** —— 生产布局里三个目录是分开的：

        * ``/opt/ekbs/app/sql/001_init.sql``（`install.sh` 复制到 ``$APP_DIR/sql``，
          ``01`` §5.7 用的就是这个）；
        * ``/opt/ekbs/etc/sql/001_init.sql``（``install.sh`` 也复制到这里）；
        * 开发仓库：``<repo>/sql/001_init.sql``。

        取第一个存在的；都不存在时返回第一个候选（由调用方报出清晰错误）。
        """
        candidates = [
            self.repo_root / "sql" / "001_init.sql",
            self.repo_root / "app" / "sql" / "001_init.sql",     # 生产 $APP_DIR/sql
            self.config_path.parent / "sql" / "001_init.sql",    # 生产 $ETC/sql
        ]
        for candidate in candidates:
            if candidate.is_file():
                return candidate
        return candidates[0]

    def data_paths(self) -> dict[str, Path]:
        """``data_dir`` 下的运行目录：``raw/parse/index/backup/tmp``，
        以及软删原件暂存的 ``trash``（保留期见 ``TRASH_RETENTION_DAYS``）。"""
        base = self.data_dir
        return {
            name: base / name
            for name in ("raw", "parse", "index", "backup", "tmp", "trash")
        }

    def ensure_dirs(self) -> None:
        """创建运行所需目录（幂等）。"""
        for path in (self.log_dir, *self.data_paths().values()):
            path.mkdir(parents=True, exist_ok=True)

    # ---- 闸门 ----

    @property
    def is_dev(self) -> bool:
        return self.app.env in {"dev", "local", "test"}

    def assert_compliance_gate(self, *, real_corpus: bool) -> None:
        """合规闸（`00` §7 硬闸门）：未取得书面确认前，禁止对**真实**语料做云端嵌入。

        ``real_corpus=False``（合成/公开样本）不受限；本地嵌入 provider 也不受本闸限制，
        因为不出网 —— 但当前 provider 集合里只有云端与 mock，故一并要求 ack。
        """
        if not real_corpus:
            return
        if self.models.embed.provider in {"mock", "local"}:
            return
        if not self.permission.corpus_egress_ack:
            raise ConfigError(
                "合规闸未开：permission.corpus_egress_ack=false 时禁止对真实语料做云端嵌入"
                "（入库即全量语料出网，00 §7）。请先取得合规书面确认，并在 application.yaml 中"
                "填写 corpus_egress_ack=true 与 corpus_egress_ack_ticket。"
            )

    def assert_l3_ingest_allowed(self, level: int) -> None:
        """L3 入库闸（`00` C5）：C5 未落地前 ``l3_ingest_enabled`` 必须为 true 才放行。"""
        if level >= 3 and not self.permission.l3_ingest_enabled:
            raise ConfigError(
                f"L{level} 入库被拒：permission.l3_ingest_enabled=false（00 C5 —— "
                "进 LLM 前文档 ACL 闸门未验收前，真实 L3 语料不得入库）"
            )


# ---------------------------------------------------------------------------
# 密钥
# ---------------------------------------------------------------------------


class Secrets:
    """``~/.ekbs/secrets.env`` 的读取视图（只读 ``os.environ``）。

    绝不把值写进日志或接口响应；用 :meth:`redacted` 输出「是否已配置」。
    """

    def __init__(self, env: dict[str, str] | None = None) -> None:
        self._env = dict(os.environ if env is None else env)

    def get(self, key: str, default: str = "") -> str:
        if key not in SECRET_KEYS:
            raise KeyError(f"{key} 不是 EKBS 认可的密钥键（见 ekbs.config.SECRET_KEYS）")
        for name in SECRET_ENV_ALIASES.get(key, (key,)):
            value = self._env.get(name)
            if value:
                return value
        return default

    def __getattr__(self, item: str) -> str:  # pragma: no cover - 便利访问
        if item.isupper():
            return self.get(item)
        raise AttributeError(item)

    def is_configured(self, key: str) -> bool:
        return bool(self.get(key))

    def redacted(self) -> dict[str, bool]:
        """安全的自检输出：只有布尔，没有值。"""
        return {key: self.is_configured(key) for key in SECRET_KEYS}

    def require(self, *keys: str) -> None:
        """断言这些密钥已注入；缺失时抛出**不含值**的错误。"""
        missing = [k for k in keys if not self.is_configured(k)]
        if missing:
            raise ConfigError(
                "缺少必需密钥：" + ", ".join(missing) + "（应放在 ~/.ekbs/secrets.env，权限 600）"
            )


# ---------------------------------------------------------------------------
# 加载
# ---------------------------------------------------------------------------


def _reject_secret_keys(raw: str, path: Path) -> None:
    """反向检查：``application.yaml`` 里出现密钥类键名即报错（`04` §2.6）。"""
    try:
        data = yaml.safe_load(raw) or {}
    except yaml.YAMLError as exc:  # YAML 语法错误也要在这里变成清晰的 ConfigError
        raise ConfigError(f"{path} 不是合法 YAML：{exc}") from exc
    hits: list[str] = []

    def walk(node: Any, prefix: str) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                name = str(key)
                if _SECRET_KEY_RE.search(name):
                    hits.append(f"{prefix}{name}")
                walk(value, f"{prefix}{name}.")
        elif isinstance(node, list):
            for item in node:
                walk(item, prefix)

    walk(data, "")
    if hits:
        raise ConfigError(
            f"{path} 中出现了密钥类键名：{', '.join(hits)}。"
            "application.yaml 是**非敏感**配置，密钥只能放 ~/.ekbs/secrets.env（04 §2.6）。"
        )


def _resolve_dir(value: str, base: Path) -> str:
    path = Path(value)
    return str(path if path.is_absolute() else (base / path).resolve())


#: 各嵌入模型**官方支持**的输出维度与「最大行数」（来源：百炼《通用文本向量同步接口》
#: 与《OpenAI 兼容-Embedding》文档，2026-10-03 核对）。
#:
#: 为什么必须本地校验：**传不支持的维度不会报错，而是静默按默认 1024 返回**
#: （实测 `dimensions=1536/2048` → 返回 1024）。配置与实际向量一旦不一致，
#: 就踩上 C3「禁止混用向量」，而且要等检索异常才暴露。
EMBED_SUPPORTED_DIMS: dict[str, frozenset[int]] = {
    "qwen3.7-text-embedding-flash": frozenset({1024, 768, 512, 256}),
    "qwen3.7-text-embedding": frozenset({2560, 2048, 1536, 1024, 768, 512, 256}),
    "text-embedding-v4": frozenset({2048, 1536, 1024, 768, 512, 256, 128, 64}),
    "text-embedding-v3": frozenset({1024, 768, 512, 256, 128, 64}),
    "text-embedding-v2": frozenset({1536}),
    "text-embedding-v1": frozenset({1536}),
}
#: 官方「最大行数」：输入为**字符串列表**时单请求最多几条。
#: ⚠️ 2026-10-03 实测 API 当前容忍到 **25** 条（50 条报 `batch size ... should not be larger than 25`），
#: 但**以官方文档为准（20）**；运行时不顶满，见配置里的 `batch_size`。
EMBED_MAX_ROWS: dict[str, int] = {
    "qwen3.7-text-embedding-flash": 20,
    "qwen3.7-text-embedding": 20,
    "text-embedding-v4": 10,
    "text-embedding-v3": 10,
    "text-embedding-v2": 25,
    "text-embedding-v1": 25,
}
#: 官方「单行最大 token 数」
EMBED_MAX_ROW_TOKENS: dict[str, int] = {
    "qwen3.7-text-embedding-flash": 128_000,
    "qwen3.7-text-embedding": 128_000,
    "text-embedding-v4": 8_192,
    "text-embedding-v3": 8_192,
    "text-embedding-v2": 2_048,
    "text-embedding-v1": 2_048,
}


def validate_embed_model_settings(model: str, dim: int, batch_size: int) -> None:
    """校验嵌入模型的维度与批量（**未知模型跳过**，不挡自研/测试 provider）。"""
    allowed = EMBED_SUPPORTED_DIMS.get(model)
    if allowed and int(dim) not in allowed:
        raise ConfigError(
            f"models.embed.dim={dim} 不被 {model} 支持（官方支持：{sorted(allowed)}）。"
            f"⚠️ 传不支持的维度**不会报错**，API 会静默按默认 1024 返回 —— "
            f"那会让配置与向量不一致（C3 禁止混用向量）。"
        )
    max_rows = EMBED_MAX_ROWS.get(model)
    if max_rows and int(batch_size) > max_rows:
        raise ConfigError(
            f"models.embed.batch_size={batch_size} 超过 {model} 的官方最大行数 {max_rows}"
            f"（单行最大 {EMBED_MAX_ROW_TOKENS.get(model, 0)} token）。建议留余量，不顶满。"
        )


def load_settings(
    path: str | Path | None = None,
    *,
    data_dir: str | Path | None = None,
    db_path: str | Path | None = None,
    overrides: dict[str, Any] | None = None,
) -> Settings:
    """加载配置。

    参数优先级：显式入参 > 环境变量（``EKBS_CONFIG`` / ``EKBS_*__*``）> 配置文件 > 默认值。

    ``data_dir`` / ``db_path`` 会连同 ``log_dir`` 一起把相对路径解析到**仓库根目录**，
    这样从任何 cwd 启动（launchd 的 WorkingDirectory 是 ``/opt/ekbs/app``）都能定位数据目录。
    """
    config_path = (
        Path(path)
        if path
        else Path(os.environ.get(CONFIG_ENV_VAR, "config/application.dev.yaml"))
    )
    config_path = config_path if config_path.is_absolute() else (Path.cwd() / config_path)
    if not config_path.is_file():
        raise ConfigError(
            f"配置文件不存在：{config_path}（用 {CONFIG_ENV_VAR} 指定，生产为 /opt/ekbs/etc/application.yaml）"
        )

    raw = config_path.read_text(encoding="utf-8")
    _reject_secret_keys(raw, config_path)
    data: dict[str, Any] = yaml.safe_load(raw) or {}

    # 仓库根 = config/ 的上一级（生产：/opt/ekbs/app）
    repo_root = config_path.resolve().parent.parent
    app = data.setdefault("app", {})
    database = data.setdefault("database", {})
    security = data.setdefault("security", {})

    for key in ("log_dir", "data_dir"):
        if key in app:
            app[key] = _resolve_dir(str(app[key]), repo_root)
    if "path" in database:
        database["path"] = _resolve_dir(str(database["path"]), repo_root)
    if security.get("sensitive_words_path"):
        security["sensitive_words_path"] = _resolve_dir(
            str(security["sensitive_words_path"]), repo_root
        )

    if data_dir is not None:
        resolved = _resolve_dir(str(data_dir), repo_root)
        app["data_dir"] = resolved
        # ⚠️ 这里**必须是赋值，不能是 `setdefault`**：三份配置文件里都显式写了
        # `app.log_dir: var/log`，`setdefault` 于是永远不生效 —— 传 data_dir=<tmp> 的
        # 调用方（全部测试夹具）嘴上说"跑在临时目录上"，日志却照写仓库的 var/log。
        # 实测后果：`var/log/test.json.log` 涨到 19 MB，并且测试进程以 service="api"
        # 配置日志时会把 `kb_created` 这类记录混进 `api.json.log`，
        # 让人以为线上库被写过（排查时真被骗过一次）。
        # 需要"数据在临时目录、日志另放"的调用方，用 overrides 显式给 app.log_dir。
        app["log_dir"] = str(Path(resolved) / "log")
    if db_path is not None:
        database["path"] = str(Path(db_path))
    if overrides:
        data = _deep_merge(data, overrides)

    settings = Settings(**data)
    settings.config_path = config_path.resolve()
    settings.repo_root = repo_root

    # 嵌入模型的维度/批量必须落在官方支持范围内 —— 失败要**启动时就炸**，而不是检索时才怪
    validate_embed_model_settings(
        settings.models.embed.model, settings.models.embed.dim, settings.models.embed.batch_size
    )

    # 非 dev 环境的监听地址硬约束（04 §7.2）；dev 只告警，便于本机联调
    if settings.server.host not in {"127.0.0.1", "::1", "localhost"} and not settings.is_dev:
        raise ConfigError(
            f"server.host={settings.server.host} 非回环地址：对外只能由反向代理暴露（04 §7.2）"
        )
    return settings


def _deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """进程内单例（launchd 每个服务一个进程，配置只在启动时读一次）。"""
    return load_settings()
