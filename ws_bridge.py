import asyncio
import json
import signal
import os
import uuid
import time
from typing import Dict, Optional
import aiohttp_cors

import aiohttp
from aiohttp import web
from websockets.legacy.server import serve, WebSocketServerProtocol
from websockets.exceptions import ConnectionClosed
import logging

FMT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
logging.basicConfig(level=logging.INFO, format=FMT, force=True)

start_time = time.time()

# ================== 設定 ==================
WS_HOST = "0.0.0.0"
WS_PORT = 9001

REST_HOST = "0.0.0.0"
REST_PORT = 8080

PILL_IDENTIFY_URL = os.getenv(
    "PILL_IDENTIFY_URL",
    "https://host.docker.internal:9487/pill_identify/upload_images",
)

PREVIEW_INTERVAL = 0.1

camera_conns: Dict[str, Optional[WebSocketServerProtocol]] = {
    "cam1": None,
    "cam2": None,
}

latest_preview: Dict[str, Optional[bytes]] = {
    "cam1": None,
    "cam2": None,
}

preview_locks: Dict[str, asyncio.Lock] = {
    "cam1": asyncio.Lock(),
    "cam2": asyncio.Lock(),
}


class PendingCapture:
    def __init__(self) -> None:
        self.images: Dict[str, Optional[bytes]] = {"cam1": None, "cam2": None}
        self.event = asyncio.Event()


pending_captures: Dict[str, PendingCapture] = {}


async def handle_camera_stream(cam_id: str, ws: WebSocketServerProtocol):
    logging.info(f"[{cam_id}] camera connected")
    camera_conns[cam_id] = ws

    try:
        async for message in ws:
            if isinstance(message, (bytes, bytearray)):
                jpeg_bytes = bytes(message)
                async with preview_locks[cam_id]:
                    latest_preview[cam_id] = jpeg_bytes
            else:
                try:
                    msg = json.loads(message)
                    logging.info(f"[{cam_id}] text msg from camera: {msg}")
                except Exception:
                    logging.info(f"[{cam_id}] non-JSON text from camera: {message}")
    except ConnectionClosed:
        logging.info(f"[{cam_id}] camera disconnected")
    except Exception as e:
        logging.info(f"[{cam_id}] camera handler error: {e}")
    finally:
        if camera_conns.get(cam_id) == ws:
            camera_conns[cam_id] = None
        logging.info(f"[{cam_id}] camera handler stopped")


async def handle_viewer_stream(cam_id: str, ws: WebSocketServerProtocol):
    logging.info(f"[viewer] connected to {cam_id}")
    try:
        while True:
            async with preview_locks[cam_id]:
                frame = latest_preview[cam_id]

            if frame is not None:
                try:
                    await ws.send(frame)
                except ConnectionClosed:
                    logging.info(f"[viewer:{cam_id}] connection closed while sending")
                    break

            await asyncio.sleep(PREVIEW_INTERVAL)
    except ConnectionClosed:
        logging.info(f"[viewer:{cam_id}] disconnected")
    except Exception as e:
        logging.info(f"[viewer:{cam_id}] error: {e}")


async def ws_router(ws: WebSocketServerProtocol, path: str):
    logging.info(f"[WS] new connection path={path}")
    if path == "/cam1":
        await handle_camera_stream("cam1", ws)
    elif path == "/cam2":
        await handle_camera_stream("cam2", ws)
    elif path == "/viewer/cam1":
        await handle_viewer_stream("cam1", ws)
    elif path == "/viewer/cam2":
        await handle_viewer_stream("cam2", ws)
    else:
        logging.info(f"[WS] unknown path: {path}")
        try:
            await ws.close(code=4000, reason="Unknown path")
        except Exception:
            pass


async def start_ws_server(stop_event: asyncio.Event):
    logging.info(f"[WS] listening on ws://{WS_HOST}:{WS_PORT}")
    async with serve(
        ws_router,
        WS_HOST,
        WS_PORT,
        max_size=None,
        ping_interval=20,
        ping_timeout=20,
    ):
        await stop_event.wait()
    logging.info("[WS] server stopped")


async def handle_capture(request: web.Request) -> web.Response:
    t_start = time.time()
    logging.info("[capture] request received")

    missing = [cam for cam in ("cam1", "cam2") if camera_conns.get(cam) is None]
    if missing:
        detail = f"camera not connected: {', '.join(missing)}"
        logging.info(f"[capture] {detail}")
        return web.json_response({"state": False, "detail": detail}, status=503)

    capture_id = str(uuid.uuid4())
    pc = PendingCapture()
    pending_captures[capture_id] = pc

    cmd = json.dumps({"type": "capture", "capture_id": capture_id})
    logging.info(f"[capture] sending capture command id={capture_id} to cam1, cam2")
    t_start_send_requests = time.time()
    logging.info(f'[time check]receive requests --> check cam state: {t_start_send_requests - t_start:.2f} sec')
    for cam_id in ("cam1", "cam2"):
        ws = camera_conns.get(cam_id)
        if ws is None:
            continue
        try:
            await ws.send(cmd)
        except Exception as e:
            logging.info(f"[capture] failed to send cmd to {cam_id}: {e}")

    t_end_send_requests = time.time()

    try:
        await asyncio.wait_for(pc.event.wait(), timeout=10.0)
    except asyncio.TimeoutError:
        logging.info(f"[capture] timeout waiting for full-res images, id={capture_id}")
        pending_captures.pop(capture_id, None)
        return web.json_response({"state": False, "detail": "capture timeout waiting for cameras"}, status=504)

    images = pc.images
    img1 = images.get("cam1")
    img2 = images.get("cam2")
    pending_captures.pop(capture_id, None)

    if img1 is None or img2 is None:
        detail = "missing images from cameras"
        logging.info(f"[capture] {detail} (cam1={img1 is not None}, cam2={img2 is not None})")
        return web.json_response({"state": False, "detail": detail}, status=500)

    logging.info(f"[capture] got both images (cam1={len(img1)/1024:.1f}KB, cam2={len(img2)/1024:.1f}KB), forwarding to pill server...")

    t_get_images_from_cams = time.time()
    logging.info(f"[time check] send requests --> get frames from cams: {t_get_images_from_cams - t_end_send_requests:.2f}sec")

    try:
        async with aiohttp.ClientSession() as session:
            form = aiohttp.FormData()
            form.add_field("images", img1, filename="cam1_full.jpg", content_type="image/jpeg")
            form.add_field("images", img2, filename="cam2_full.jpg", content_type="image/jpeg")

            async with session.post(PILL_IDENTIFY_URL, data=form, ssl=False) as resp:
                text = await resp.text()
                logging.info(f"[capture] pill server status={resp.status}")
                try:
                    data = json.loads(text)
                except json.JSONDecodeError:
                    logging.info("[capture] pill server returned non-JSON")
                    return web.json_response({"state": False, "detail": "pill server returned non-JSON", "raw": text}, status=502)

                t_pill_server_get_images = time.time()
                logging.info(
                    f"[time check] get frames from cams --> pill server get images: {t_pill_server_get_images - t_get_images_from_cams:.2f}sec")
                return web.json_response(data, status=resp.status)
    except Exception as e:
        logging.info(f"[capture] error calling pill server: {e}")
        return web.json_response({"state": False, "detail": f"internal error: {e}"}, status=500)


async def handle_upload_full(request: web.Request) -> web.Response:
    cam_id = request.match_info.get("cam_id")
    if cam_id not in ("cam1", "cam2"):
        return web.json_response({"ok": False, "detail": "invalid camera id"}, status=400)

    capture_id = request.query.get("capture_id")
    if not capture_id:
        return web.json_response({"ok": False, "detail": "capture_id is required"}, status=400)

    pc = pending_captures.get(capture_id)
    if pc is None:
        logging.info(f"[upload_full] unknown/expired capture_id={capture_id}")
        return web.json_response({"ok": False, "detail": "unknown capture_id"}, status=410)

    reader = await request.multipart()
    field = await reader.next()
    if field is None or field.name not in ("image", "file"):
        return web.json_response({"ok": False, "detail": "missing image field"}, status=400)

    img_bytes = await field.read(decode=False)
    logging.info(f"[upload_full] got {len(img_bytes)/1024:.1f}KB from {cam_id}, capture_id={capture_id}")

    pc.images[cam_id] = img_bytes

    if pc.images["cam1"] is not None and pc.images["cam2"] is not None:
        if not pc.event.is_set():
            pc.event.set()

    return web.json_response({"ok": True})


async def start_rest_server(stop_event: asyncio.Event):
    app = web.Application()
    # ====== 新增 CORS ======
    cors = aiohttp_cors.setup(app, defaults={
        "*": aiohttp_cors.ResourceOptions(
            allow_credentials=True,
            expose_headers="*",
            allow_headers="*",
        )
    })
    # =======================
    capture_route = app.router.add_post("/capture", handle_capture)
    upload_route  = app.router.add_post("/upload_full/{cam_id}", handle_upload_full)

    # 把 CORS 套到每個 route
    cors.add(capture_route)
    cors.add(upload_route)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, REST_HOST, REST_PORT)
    await site.start()
    logging.info(f"[REST] listening on http://{REST_HOST}:{REST_PORT}")

    try:
        await stop_event.wait()
    finally:
        logging.info("[REST] shutting down...")
        await runner.cleanup()


async def main():
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass

    ws_task = asyncio.create_task(start_ws_server(stop_event))
    rest_task = asyncio.create_task(start_rest_server(stop_event))

    logging.info("[BRIDGE] started. Press Ctrl+C to stop.")
    await stop_event.wait()

    logging.info("[BRIDGE] stopping...")
    await asyncio.gather(ws_task, rest_task, return_exceptions=True)
    logging.info("[BRIDGE] stopped.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
