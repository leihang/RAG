"""配置与密钥纪律（`04` §2.6、`01` §7、`00` §7）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from ekbs.config import Secrets, load_settings
from ekbs.errors import ConfigError

BASE_YAML = """
app:
  name: ekbs
  env: dev
  log_dir: var/log
  data_dir: var
server:
  host: 127.0.0.1
  port: 8000
  workers: 1
database:
  path: var/index/ekbs.db
models:
  embed:
    provider: mock
    model: mock-embed-v1
    dim: 64
  llm:
    provider: mock
    model: mock-llm-v1
permission:
  corpus_egress_ack: false
  l3_ingest_enabled: false
"""


def _write(tmp_path: Path, text: str) -> Path:
    config = tmp_path / "config" / "application.yaml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(text, encoding="utf-8")
    return config


def test_secret_key_in_yaml_is_rejected(tmp_path: Path) -> None:
    """反向检查：application.yaml 里出现密钥类键名必须报错退出（04 §2.6）。"""
    config = _write(tmp_path, BASE_YAML + "\nmodels_extra:\n  dashscope_api_key: sk-xxx\n")
    with pytest.raises(ConfigError) as excinfo:
        load_settings(config)
    assert "密钥类键名" in str(excinfo.value)


def test_legitimate_token_keys_are_not_rejected(tmp_path: Path) -> None:
    """合法非敏感键（缓存键模板、刷新提前量、合规确认编号）不能被误伤。"""
    extra = """
dingtalk:
  openapi:
    token_cache_key: "ekbs:dt:app_token:{app_key}"
    token_refresh_ahead_seconds: 300
permission:
  corpus_egress_ack: false
  corpus_egress_ack_ticket: "COMP-2026-001"
"""
    config = _write(tmp_path, BASE_YAML + extra)
    settings = load_settings(config)
    assert settings.permission.corpus_egress_ack_ticket == "COMP-2026-001"


def test_workers_must_be_one(tmp_path: Path) -> None:
    """workers != 1 会带来 ≤300s 的旧权限窗口（04 §1.5），配置层直接拒绝。"""
    config = _write(tmp_path, BASE_YAML.replace("workers: 1", "workers: 2"))
    with pytest.raises(Exception) as excinfo:
        load_settings(config)
    assert "workers" in str(excinfo.value)


def test_compliance_gate_blocks_real_corpus_on_cloud_embedding(settings) -> None:
    """合规闸：云端嵌入 = 全量语料出网，书面确认前禁止真实语料入库（00 §7）。"""
    settings.models.embed.provider = "dashscope"
    settings.permission.corpus_egress_ack = False
    with pytest.raises(ConfigError):
        settings.assert_compliance_gate(real_corpus=True)
    # 合成语料不受限
    settings.assert_compliance_gate(real_corpus=False)
    # 拿到书面确认后放行
    settings.permission.corpus_egress_ack = True
    settings.assert_compliance_gate(real_corpus=True)


def test_compliance_gate_allows_local_embedding(settings) -> None:
    """本地嵌入不出网，不受合规闸限制。"""
    settings.models.embed.provider = "local"
    settings.permission.corpus_egress_ack = False
    settings.assert_compliance_gate(real_corpus=True)


def test_l3_ingest_gate(settings) -> None:
    """C5 未验收前禁止真实 L3 入库（00 C5、01 §7）。"""
    settings.permission.l3_ingest_enabled = False
    with pytest.raises(ConfigError):
        settings.assert_l3_ingest_allowed(3)
    settings.assert_l3_ingest_allowed(2)
    settings.permission.l3_ingest_enabled = True
    settings.assert_l3_ingest_allowed(3)


def test_secrets_never_expose_values(monkeypatch) -> None:
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-super-secret")
    secrets = Secrets()
    assert secrets.is_configured("DASHSCOPE_API_KEY") is True
    assert "sk-super-secret" not in str(secrets.redacted())
    assert secrets.redacted()["DASHSCOPE_API_KEY"] is True
    with pytest.raises(KeyError):
        secrets.get("NOT_A_SECRET_KEY")


def test_dev_config_loads_and_resolves_paths() -> None:
    from tests.conftest import DEV_CONFIG

    settings = load_settings(DEV_CONFIG)
    assert settings.is_dev
    assert settings.db_path.is_absolute()
    assert settings.models.embed.provider == "mock"
    assert settings.retrieval.rerank.top_n == 15
    assert settings.retrieval.rerank.keep == 5
    assert settings.server.workers == 1
