from __future__ import annotations

import pytest

from excel_rag_parser import ParseOptions


def test_defaults_valid() -> None:
    options = ParseOptions()
    assert options.max_chunk_chars == 2000
    assert options.numeric_column_ratio == 0.7


@pytest.mark.parametrize("kwargs", [
    {"max_chunk_chars": 100},
    {"min_slice_rows": 0},
    {"numeric_column_ratio": 0.0},
    {"numeric_column_ratio": 1.5},
    {"header_rows": 0},
    {"date_serial_min": 50000, "date_serial_max": 40000},
])
def test_invalid_options_raise(kwargs: dict) -> None:
    with pytest.raises(ValueError):
        ParseOptions(**kwargs)
