"""UI E2E tests (Playwright + Edge headless) kiểm tra 3 yêu cầu giao diện:
1. Card lỗi (.result-card.is-failed) có chiều cao bằng card bình thường across ratios & densities.
2. Chế độ Video Frames style composer với Start frame + End frame, swap và frame picker.
3. Sau đăng nhập endpoint + apikey, UI nạp đúng danh sách model thực từ /v1/models.
"""

import asyncio
import base64
import json
import socket
import threading
import time

import pytest
import uvicorn
from fastapi import FastAPI, Header
from fastapi.responses import JSONResponse

import app.main as main
from app import generation
from app.database import PlaygroundDatabase

playwright_sync = pytest.importorskip("playwright.sync_api")


def _run_coro(coro):
    """Chạy coroutine trên thread riêng để tránh xung đột loop của Playwright sync."""
    out = {}

    def worker():
        try:
            out["res"] = asyncio.run(coro)
        except BaseException as exc:
            out["err"] = exc

    t = threading.Thread(target=worker)
    t.start()
    t.join()
    if "err" in out:
        raise out["err"]
    return out.get("res")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _start_uvicorn(asgi_app, port: int):
    config = uvicorn.Config(asgi_app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10.0
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    if not server.started:
        raise RuntimeError("Server did not start in time")
    return server, thread


@pytest.fixture
def live_servers(tmp_path, monkeypatch):
    # Mock upstream flow2api server
    upstream = FastAPI()
    captured_generate = []

    @upstream.get("/v1/models")
    async def upstream_models(authorization: str | None = Header(default=None)):
        if authorization != "Bearer ui-test-key":
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return {
            "object": "list",
            "data": [
                {"id": "gemini-3.1-flash-image-landscape", "description": "Nano Banana 2.1"},
                {"id": "gemini-3.1-flash-image-portrait-2k", "description": "Nano Banana 2.1"},
                {"id": "gemini-3.1-flash-lite-image-landscape", "description": "Nano Banana 2 Lite"},
                {"id": "veo-3.1-lite-4s-landscape", "description": "Veo 3.1 - Lite · 4s · landscape"},
                {"id": "veo-3.1-lite-6s-landscape", "description": "Veo 3.1 - Lite · 6s · landscape"},
                {"id": "veo-3.1-lite-8s-landscape", "description": "Veo 3.1 - Lite · 8s · landscape"},
                {"id": "veo-3.1-lite-i2v-4s-landscape", "description": "Veo 3.1 - Lite I2V · 4s · landscape"},
                {"id": "veo-3.1-lite-i2v-6s-portrait", "description": "Veo 3.1 - Lite I2V · 6s · portrait"},
                {"id": "veo-3.1-lite-r2v-8s-landscape", "description": "Veo 3.1 - Lite R2V · 8s · landscape"},
                {"id": "veo-3.1-lite-extend-8s-landscape", "description": "Veo 3.1 - Lite Extend · landscape"},
                {"id": "veo-3.1-lite", "description": "Veo 3.1 - Lite · 8s · landscape"},
                {"id": "veo-3.1-fast-4s-landscape", "description": "Veo 3.1 - Fast · 4s · landscape"},
                {"id": "veo-3.1-quality-8s-landscape", "description": "Veo 3.1 - Quality · 8s · landscape"},
                {"id": "omni-1.1-flash-4s-landscape", "description": "Omni 1.1 Flash · 4s · landscape"},
                *[
                    {
                        "id": f"omni-1.2-flash-{sec}s-{orient}",
                        "description": f"Omni 1.2 Flash · {sec}s · {orient}",
                    }
                    for sec in range(3, 16)
                    for orient in ("landscape", "portrait")
                ],
                {"id": "omni-1.2-flash", "description": "Omni 1.2 Flash · 8s · landscape"},
            ],
        }

    @upstream.post("/v1/chat/completions")
    async def upstream_chat(body: dict):
        captured_generate.append(body)
        return {"choices": [{"message": {"content": "ok"}}]}

    upstream_port = _free_port()
    upstream_srv, upstream_thr = _start_uvicorn(upstream, upstream_port)

    # Playground server với DB tạm
    db = PlaygroundDatabase(str(tmp_path / "ui_playground.db"))
    monkeypatch.setattr("app.routes.playground_db", db)
    monkeypatch.setattr("app.generation._flow_clients", {})
    pg_port = _free_port()
    pg_srv, pg_thr = _start_uvicorn(main.app, pg_port)

    yield {
        "upstream_url": f"http://127.0.0.1:{upstream_port}",
        "pg_url": f"http://127.0.0.1:{pg_port}",
        "db": db,
        "captured": captured_generate,
    }

    pg_srv.should_exit = True
    upstream_srv.should_exit = True
    pg_thr.join(timeout=3)
    upstream_thr.join(timeout=3)


def test_ui_models_failed_card_height_and_frames_composer(live_servers):
    with playwright_sync.sync_playwright() as p:
        try:
            browser = p.chromium.launch(channel="msedge", headless=True)
        except Exception as exc:
            pytest.skip(f"Edge headless không khả dụng trên máy test: {exc}")

        try:
            context = browser.new_context(viewport={"width": 1440, "height": 900})
            page = context.new_page()

            page.goto(f"{live_servers['pg_url']}/playground", wait_until="domcontentloaded")
            page.wait_for_selector("#login-button")
            page.fill("#api-url", live_servers["upstream_url"])
            page.fill("#api-key", "ui-test-key")
            page.click("#login-button")
            page.wait_for_selector("#workspace:not([hidden])")

            # 1) Kiểm tra danh sách model đã lấy đúng theo endpoint + apikey & slider thời lượng
            page.click("#gen-trigger")
            page.wait_for_selector("#gen-dialog:not([hidden])")
            page.click("#gen-model-image")
            image_options = page.locator("#gen-model-list .gen-model-option .gen-model-name").all_inner_texts()
            assert image_options == ["Nano Banana 2.1", "Nano Banana 2 Lite"], image_options

            page.click('.gen-tab[data-mode="video"]')
            page.click("#gen-model-video")
            video_options = page.locator("#gen-model-list .gen-model-option .gen-model-name").all_inner_texts()
            assert video_options == ["Veo 3.1 - Lite", "Omni 1.2 Flash"], video_options

            # Veo 3.1 - Lite: slider chỉ kéo 4-6-8
            page.click('#gen-model-list .gen-model-option:has-text("Veo 3.1 - Lite")')
            assert page.get_attribute("#gen-length-slider", "min") == "4"
            assert page.get_attribute("#gen-length-slider", "max") == "8"
            assert page.get_attribute("#gen-length-slider", "step") == "2"
            assert page.locator("#gen-length-ticks .gen-length-tick").all_inner_texts() == ["4s", "6s", "8s"]

            # Omni 1.2 Flash: slider kéo được từ 3 đến 15s
            page.click("#gen-model-video")
            page.click('#gen-model-list .gen-model-option:has-text("Omni 1.2 Flash")')
            assert page.get_attribute("#gen-length-slider", "min") == "3"
            assert page.get_attribute("#gen-length-slider", "max") == "15"
            assert page.get_attribute("#gen-length-slider", "step") == "1"
            assert page.locator("#gen-length-ticks .gen-length-tick").all_inner_texts() == ["3s", "6s", "9s", "12s", "15s"]
            page.locator("#gen-length-slider").fill("13")
            page.locator("#gen-length-slider").dispatch_event("input")
            assert page.locator("#gen-length-value").inner_text() == "13 Sec"

            # Quay lại Veo 3.1 - Lite -> tự snap về 4-6-8
            page.click("#gen-model-video")
            page.click('#gen-model-list .gen-model-option:has-text("Veo 3.1 - Lite")')
            assert page.get_attribute("#gen-length-slider", "max") == "8"
            assert page.get_attribute("#gen-length-slider", "step") == "2"
            assert page.locator("#gen-length-value").inner_text() == "8 Sec"

            # 2) Kiểm tra chế độ Video Frames với 0 ảnh: bấm được ngay và style composer Start/End frame
            page.click('#gen-vmode .gen-seg-btn[data-vmode="frames"]')
            assert page.locator(".composer.is-frames").count() == 1
            assert page.locator("#composer-frames:not([hidden])").count() == 1
            assert "Start frame" in page.locator("#frame-slot-start").inner_text()
            assert "End frame" in page.locator("#frame-slot-end").inner_text()
            assert "Start frame" in page.get_attribute("#prompt", "placeholder")

            # 2b) Kiểm tra kéo-thả (drag & drop) tệp ảnh vào Playground:
            # KHÔNG gọi Flow upstream lúc thả tệp, lưu cục bộ trên Playground và chọn sẵn làm tham chiếu!
            page.click("#frames-exit")  # click ngoài gen-dialog sẽ tự đóng dialog
            page.click("#gen-trigger")
            page.click('.gen-tab[data-mode="image"]')
            page.click("#gen-trigger")

            tiny_png_b64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
            page.evaluate(
                """(b64) => {
                  const bytes = Uint8Array.from(atob(b64), c => c.charCodeAt(0));
                  const file = new File([bytes], 'dropped-ref.png', { type: 'image/png' });
                  const dt = new DataTransfer();
                  dt.items.add(file);
                  document.querySelector('.stage').dispatchEvent(new DragEvent('drop', { dataTransfer: dt, bubbles: true, cancelable: true }));
                }""",
                tiny_png_b64,
            )
            page.wait_for_selector("#composer-refs:not([hidden]) .composer-ref-chip")
            assert len(live_servers["captured"]) == 0  # Không hề gửi lên Flow lúc thả tệp!
            assert page.locator(".result-card .result-ref-badge").count() == 1

            # Kéo-thả 1 tệp nữa qua #ref-upload-input (chỉ lưu Playground) + drop lên #frame-slot-start ở chế độ Frames
            page.click("#gen-trigger")
            page.click('.gen-tab[data-mode="video"]')
            page.click('#gen-vmode .gen-seg-btn[data-vmode="frames"]')
            page.click("#gen-trigger")
            page.evaluate(
                """(b64) => {
                  const bytes = Uint8Array.from(atob(b64), c => c.charCodeAt(0));
                  const file = new File([bytes], 'frame-drop.png', { type: 'image/png' });
                  const dt = new DataTransfer();
                  dt.items.add(file);
                  document.querySelector('#frame-slot-start').dispatchEvent(new DragEvent('drop', { dataTransfer: dt, bubbles: true, cancelable: true }));
                }""",
                tiny_png_b64,
            )
            page.wait_for_function(
                "() => document.querySelector('#frame-slot-start .frame-slot-sub')?.textContent === 'frame-drop'"
            )
            assert len(live_servers["captured"]) == 0  # Vẫn chưa tải lên Flow!

            # Seed project với 2 ảnh thật (PNG 1x1) + nhiều tỉ lệ + job lỗi cho mỗi tỉ lệ
            tiny_png = "data:image/png;base64," + base64.b64encode(
                base64.b64decode(
                    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
                )
            ).decode()

            page.evaluate(
                """async ({ png }) => {
                  const res = await fetch('/playground/api/projects', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json', 'X-Playground-Request': '1' },
                    body: JSON.stringify({ name: 'UI Check', prompt: '', results: [] })
                  });
                  const proj = await res.json();
                  for (const name of ['Img Start', 'Img End']) {
                    await fetch(`/playground/api/projects/${proj.id}/items`, {
                      method: 'POST',
                      headers: { 'Content-Type': 'application/json', 'X-Playground-Request': '1' },
                      body: JSON.stringify({ name, image: png })
                    });
                  }
                  window.__testProjectId = proj.id;
                }""",
                {"png": tiny_png},
            )

            # Seed trực tiếp item kết quả 16:9 và 9:16 + job lỗi 16:9 và 9:16 vào DB để test chiều cao card lỗi
            import hashlib
            owner = hashlib.sha256(b"ui-test-key").hexdigest()
            proj_id = page.evaluate("window.__testProjectId")
            db = live_servers["db"]
            _run_coro(
                db.append_results(
                    owner,
                    proj_id,
                    [
                        {
                            "id": "norm-169",
                            "kind": "image",
                            "url": tiny_png,
                            "created_at": 1,
                            "name": "Normal 16:9",
                            "meta": {"ratio": "16:9", "mode": "image"},
                        },
                        {
                            "id": "norm-916",
                            "kind": "image",
                            "url": tiny_png,
                            "created_at": 2,
                            "name": "Normal 9:16",
                            "meta": {"ratio": "9:16", "mode": "image"},
                        },
                    ],
                )
            )
            for jid, mode, ratio in [
                ("err-169", "video", "16:9"),
                ("err-916", "image", "9:16"),
            ]:
                _run_coro(db.create_job(jid, owner, proj_id, {"mode": mode, "ratio": ratio}))
                _run_coro(
                    db.update_job_status(
                        jid,
                        "error",
                        error="Upstream failed with a very long error message that used to stretch card height beyond normal cards",
                        results=[],
                        owner_hash=owner,
                        project_id=proj_id,
                    )
                )

            # Lưu queue localStorage khớp để reconcile biết đúng ratio mỗi job lỗi, rồi reload F5
            page.evaluate(
                """(pid) => {
                  localStorage.setItem('playground_job_queue', JSON.stringify([
                    { projectId: pid, jobId: 'err-169', mode: 'video', ratio: '16:9', count: 1, startedAt: Date.now() },
                    { projectId: pid, jobId: 'err-916', mode: 'image', ratio: '9:16', count: 1, startedAt: Date.now() },
                  ]));
                }""",
                proj_id,
            )
            page.reload(wait_until="domcontentloaded")
            page.wait_for_selector(".result-card.is-failed")

            # 3) Đo chiều cao thực tế card lỗi vs card bình thường ở cả 3 mức density (Large/Medium/Small)
            for cols in (6, 10, 16):
                page.click(f'.density-option[data-columns="{cols}"]')
                heights = page.evaluate(
                    """() => {
                      const cards = [...document.querySelectorAll('.result-card')];
                      const norm169 = cards.find(c => c.dataset.resultId === 'norm-169').getBoundingClientRect();
                      const norm916 = cards.find(c => c.dataset.resultId === 'norm-916').getBoundingClientRect();
                      const failed = cards.filter(c => c.classList.contains('is-failed')).map(c => ({
                        ratio: c.querySelector('.result-card-frame').style.aspectRatio,
                        rect: c.getBoundingClientRect(),
                        actions: [...c.querySelectorAll('button')].map(b => b.getBoundingClientRect()),
                      }));
                      return { norm169, norm916, failed };
                    }"""
                )
                err169 = next(f for f in heights["failed"] if "16 / 9" in f["ratio"])
                err916 = next(f for f in heights["failed"] if "9 / 16" in f["ratio"])
                assert abs(err169["rect"]["height"] - heights["norm169"]["height"]) <= 1.0, (cols, err169, heights["norm169"])
                assert abs(err916["rect"]["height"] - heights["norm916"]["height"]) <= 1.0, (cols, err916, heights["norm916"])
                # Nút Retry/Delete nằm gọn bên trong card lỗi (không tràn ra ngoài)
                for f in (err169, err916):
                    for btn_rect in f["actions"]:
                        assert btn_rect["right"] <= f["rect"]["right"] + 1
                        assert btn_rect["bottom"] <= f["rect"]["bottom"] + 1

            # 4) Chọn Start frame và End frame qua frame-picker và swap
            page.click("#gen-trigger")
            page.click('.gen-tab[data-mode="video"]')
            page.click('#gen-vmode .gen-seg-btn[data-vmode="frames"]')
            page.click("#frame-slot-start .frame-slot-trigger")
            page.wait_for_selector("#frame-picker:not([hidden])")
            page.locator("#frame-picker-grid .frame-picker-item").first.click()
            assert page.locator("#frame-slot-start.is-filled").count() == 1

            page.click("#frame-slot-end .frame-slot-trigger")
            page.wait_for_selector("#frame-picker:not([hidden])")
            page.locator("#frame-picker-grid .frame-picker-item").nth(1).click()
            assert page.locator("#frame-slot-end.is-filled").count() == 1

            before_start = page.locator("#frame-slot-start .frame-slot-sub").inner_text()
            before_end = page.locator("#frame-slot-end .frame-slot-sub").inner_text()
            page.click("#frames-swap")
            assert page.locator("#frame-slot-start .frame-slot-sub").inner_text() == before_end
            assert page.locator("#frame-slot-end .frame-slot-sub").inner_text() == before_start

            # Đóng picker/dialog nếu còn mở rồi test gửi job Frames (chặn POST để kiểm payload gửi lên server)
            sent_payloads = []
            def intercept_gen(route):
                sent_payloads.append(json.loads(route.request.post_data))
                route.fulfill(status=400, content_type="application/json", body='{"detail":"stop-test"}')
            page.route("**/playground/api/projects/*/generate", intercept_gen)
            page.fill("#prompt", "Chuyển động mượt giữa hai frame")
            page.click("#send")
            page.wait_for_timeout(300)
            assert len(sent_payloads) == 1
            assert sent_payloads[0]["mode"] == "video"
            assert sent_payloads[0]["vmode"] == "frames"
            assert len(sent_payloads[0]["references"]) == 2
        finally:
            browser.close()
