"""Entrypoint: python main.py — chạy uvicorn src app."""
import os
from pathlib import Path

import uvicorn

if __name__ == "__main__":
    # PLAYGROUND_RELOAD=1: uvicorn tự restart worker khi app/*.py đổi — không
    # cần docker restart sau khi sửa code. StatReload quét mtime (polling) nên
    # hoạt động cả trên mount Windows /mnt/d nơi inotify không có.
    reload_enabled = bool(os.environ.get("PLAYGROUND_RELOAD", "").strip())
    uvicorn.run(
        "app.main:app",
        host=os.environ.get("PLAYGROUND_HOST", "0.0.0.0"),
        port=int(os.environ.get("PLAYGROUND_PORT", "8000")),
        reload=reload_enabled,
        # Chỉ theo dõi app/ — static/ được đọc từ disk mỗi request nên không
        # cần reload; tránh quét tmp/ (kết quả generate ghi liên tục sẽ kích
        # hoạt reload giả).
        reload_dirs=[str(Path(__file__).resolve().parent / "app")] if reload_enabled else None,
    )
