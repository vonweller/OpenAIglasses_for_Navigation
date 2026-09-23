"""Local K230 protocol acceptance server; no cloud ASR and no media recording."""

import argparse
import asyncio
import json
import math
import struct
import time

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse
import uvicorn


app = FastAPI()
started = time.monotonic()
report = {
    "camera_connections": 0, "audio_connections": 0, "playback_connections": 0,
    "frames": 0, "invalid_jpegs": 0, "audio_blocks": 0, "invalid_pcm": 0,
    "pcm_peak": 0, "nonzero_blocks": 0, "playback_bytes": 0,
    "controls_sent": [], "latest_stats": {}, "samples": [],
    "snapshot_bytes": 0, "snapshot_complete": False,
}


def wav_header() -> bytes:
    return struct.pack("<4sI4s4sIHHIIHH4sI", b"RIFF", 0x7FFFFFFF, b"WAVE", b"fmt ",
                       16, 1, 1, 8000, 16000, 2, 16, b"data", 0x7FFFFFF0)


def tone_chunk(index: int) -> bytes:
    return struct.pack("<160h", *(
        int(1800 * math.sin(2 * math.pi * 440 * (index * 160 + sample) / 8000))
        for sample in range(160)
    ))


@app.websocket("/ws/camera")
async def camera(ws: WebSocket) -> None:
    await ws.accept()
    report["camera_connections"] += 1
    connection = report["camera_connections"]
    for command in ("SET:FRAMESIZE=VGA", "SET:QUALITY=14", "SET:FPS=24"):
        await ws.send_text(command)
    begin = time.monotonic()
    snapshot = False
    steps = [
        (5, ("SNAP:HQ",)),
        (10, ("SET:FRAMESIZE=SVGA", "SET:QUALITY=10", "SET:FPS=15")),
        (22, ("SET:FRAMESIZE=VGA", "SET:QUALITY=14", "SET:FPS=24")),
    ]
    try:
        while True:
            age = time.monotonic() - begin
            if connection == 1 and age > 34:
                await ws.close(code=1012)
                return
            if connection == 1 and steps and age >= steps[0][0]:
                _, commands = steps.pop(0)
                for command in commands:
                    await ws.send_text(command)
                    report["controls_sent"].append(command)
            message = await asyncio.wait_for(ws.receive(), 10)
            data = message.get("bytes")
            text = message.get("text")
            if message["type"] == "websocket.disconnect":
                return
            if text == "SNAP:BEGIN":
                snapshot = True
            elif text == "SNAP:END":
                report["snapshot_complete"] = report["snapshot_bytes"] > 0
                snapshot = False
            if data is not None:
                if snapshot:
                    report["snapshot_bytes"] = len(data)
                report["frames"] += 1
                if not (data.startswith(b"\xff\xd8") and data.endswith(b"\xff\xd9")):
                    report["invalid_jpegs"] += 1
            elif text and text.startswith("STAT:"):
                stats = json.loads(text[5:])
                report["latest_stats"] = stats
                report["samples"].append({"elapsed": round(time.monotonic() - started, 1), **stats})
                report["samples"] = report["samples"][-100:]
    except (WebSocketDisconnect, asyncio.TimeoutError):
        return


@app.websocket("/ws_audio")
async def audio(ws: WebSocket) -> None:
    await ws.accept()
    report["audio_connections"] += 1
    connection = report["audio_connections"]
    begin = time.monotonic()
    session_ready = False
    restart_sent = False
    try:
        while True:
            message = await asyncio.wait_for(ws.receive(), 15)
            if message["type"] == "websocket.disconnect":
                return
            if message.get("text") == "START":
                session_ready = True
                await ws.send_text("OK:STARTED")
            data = message.get("bytes")
            if data is not None:
                if not session_ready or len(data) != 640:
                    report["invalid_pcm"] += 1
                report["audio_blocks"] += 1
                if data:
                    peak = max(abs(x[0]) for x in struct.iter_unpack("<h", data))
                    report["pcm_peak"] = max(report["pcm_peak"], peak)
                    report["nonzero_blocks"] += int(peak > 0)
            if not restart_sent and connection <= 2 and time.monotonic() - begin > 20:
                command = "RESTART" if connection == 1 else "RESET"
                await ws.send_text(command)
                report["controls_sent"].append(command)
                restart_sent = True
    except (WebSocketDisconnect, asyncio.TimeoutError):
        return


@app.get("/stream.wav")
async def playback():
    report["playback_connections"] += 1
    connection = report["playback_connections"]

    async def stream():
        yield wav_header()
        begin = time.monotonic()
        while True:
            for i in range(50):
                chunk = tone_chunk(i)
                report["playback_bytes"] += len(chunk)
                yield chunk
                await asyncio.sleep(0.02)
            await asyncio.sleep(5)
            report["playback_bytes"] += 160
            yield bytes(160)
            if connection == 1 and time.monotonic() - begin > 14:
                return
    return StreamingResponse(stream(), media_type="audio/wav")


@app.get("/report")
def status():
    return {"elapsed_seconds": round(time.monotonic() - started, 2), **report}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8082)
    args = parser.parse_args()
    uvicorn.run(app, host=args.host, port=args.port, access_log=False)


if __name__ == "__main__":
    main()
