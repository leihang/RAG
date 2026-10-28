"""解析参数（``ParseOptions``）+ 校验。

所有启发式阈值集中在这里，便于生产按模板调参；``__post_init__`` 做兜底校验，
避免非法配置在下游产生难以解释的结果。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass


@dataclass
class ParseOptions:
    """一次解析的全部可调参数。"""

    #: 显式表头行数；None = 启发式（连续非数值行）
    header_rows: int | None = None
    #: 单个 chunk 的字符预算（不含元数据标签）；超过则按行切片
    max_chunk_chars: int = 2000
    #: 长表切片时每片最少保留的行数（避免切出 1 行碎片）
    min_slice_rows: int = 3
    #: 数值列占比阈值：≥ 该值且无「分析/对比」列 → 判为纯数值表（走 JSON/SQL）
    numeric_column_ratio: float = 0.7
    #: 调试用：关闭数字隔离，全部走 Markdown
    force_all_text: bool = False
    #: 是否把剥离出来的说明/备注切成独立文本块
    include_text_notes: bool = True
    #: General 格式整数落此区间时按 Excel 日期序列号处理（≈2009–2036）
    date_serial_min: int = 40000
    date_serial_max: int = 50000
    #: 透传 logger；None = 用库默认 logger
    logger: logging.Logger | None = None

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        if self.header_rows is not None and self.header_rows < 1:
            raise ValueError(f"header_rows 必须 >= 1 或 None，收到 {self.header_rows}")
        if self.max_chunk_chars < 200:
            raise ValueError(f"max_chunk_chars 太小（{self.max_chunk_chars}），至少 200")
        if self.min_slice_rows < 1:
            raise ValueError(f"min_slice_rows 必须 >= 1，收到 {self.min_slice_rows}")
        if not 0.0 < self.numeric_column_ratio <= 1.0:
            raise ValueError(
                f"numeric_column_ratio 必须在 (0, 1]，收到 {self.numeric_column_ratio}"
            )
        if self.date_serial_min >= self.date_serial_max:
            raise ValueError("date_serial_min 必须小于 date_serial_max")
