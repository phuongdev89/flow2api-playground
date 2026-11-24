import sys
from pathlib import Path

import pytest

# Cho phép chạy pytest từ thư mục gốc project.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture(autouse=True)
def _clear_generation_models_cache():
    from app import generation

    generation._models_cache.clear()
    yield
    generation._models_cache.clear()
