"""异常分类：所有解析错误都必须带上下文（文件、格式、sheet/行号、依赖名）。"""

from __future__ import annotations


class ExcelRagParserError(Exception):
    """所有解析异常的基类。"""


class UnsupportedFormatError(ExcelRagParserError):
    """文件后缀不在支持列表内。"""

    def __init__(self, path: str, suffix: str, supported: list[str]) -> None:
        self.path = path
        self.suffix = suffix
        self.supported = supported
        super().__init__(
            f"不支持的文件格式 {suffix!r}（{path}）；支持：{', '.join(supported)}"
        )


class CorruptFileError(ExcelRagParserError):
    """文件存在但无法被对应解析库读取（损坏、加密、非法内容）。"""

    def __init__(self, path: str, fmt: str, cause: str) -> None:
        self.path = path
        self.fmt = fmt
        self.cause = cause
        super().__init__(f"文件无法读取（{path}，格式 {fmt}）：{cause}")


class MissingDependencyError(ExcelRagParserError):
    """读取某格式所需的可选依赖未安装。"""

    def __init__(self, fmt: str, package: str, extra: str) -> None:
        self.fmt = fmt
        self.package = package
        self.extra = extra
        super().__init__(
            f"读取 {fmt} 需要可选依赖 {package}；请安装：pip install 'excel-rag-parser[{extra}]'"
        )


class EmptyWorkbookError(ExcelRagParserError):
    """工作簿里没有任何可识别的内容。"""

    def __init__(self, path: str) -> None:
        self.path = path
        super().__init__(f"工作簿没有任何可用内容（{path}）")
