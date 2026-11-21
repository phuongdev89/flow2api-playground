"""Entrypoint: python main.py — chạy uvicorn src app."""
import uvicorn

if __name__ == "__main__":
    import os

    uvicorn.run(
        "app.main:app",
        host=os.environ.get("PLAYGROUND_HOST", "0.0.0.0"),
        port=int(os.environ.get("PLAYGROUND_PORT", "8000")),
    )
