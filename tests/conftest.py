import sys
from pathlib import Path

# Cho phép chạy pytest từ thư mục gốc project.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
