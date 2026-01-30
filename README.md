# browser-camera-bridge

## Why This exists
- WebRTC prioritizes adaptive streaming over image fidelity
- Resolution is adjusted dynamically and cannot be reliably locked
- Full camera resolution is not guaranteed at capture time </br>

This bridge exists to provide explicit, full-resolution image capture from browser-based cameras.

## What This Is
This service is intended to be used with cameras running in a web browser.</br>
Each camera:
- runs in a browser
- obtains camera permission via getUserMedia()
- streams JPEG preview frames over WebSocket
- uploads full-resolution images via REST when a capture is triggered </br>

The bridge coordinates multiple cameras (e.g. cam1, cam2), collects captured images, and forwards them to a downstream backend service.

## Typical Use Case
- A device (tablet / laptop / kiosk / mobile) opens a web page
- The browser requests camera access
- Preview frames are streamed to this service
- A client triggers a capture request
- Browsers upload full-resolution images
- Images are forwarded to an external recognition service

## Architecture Overview
```
Browser Camera (cam1) ---\
                           --> browser-camera-bridge --> Backend Service
Browser Camera (cam2) ---/

Viewer <--- WebSocket preview stream
```
