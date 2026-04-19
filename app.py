import os
import sys
import json
import subprocess
import asyncio

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query
from fastapi.responses import JSONResponse
import uvicorn
import nest_asyncio
import aiohttp
from fastapi.middleware.cors import CORSMiddleware

from voice_agent import VoiceSessionManager


app = FastAPI()
voice_manager = VoiceSessionManager()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://portfolio.beingcogni.com/"],  # Or specify your frontend URL like "http://localhost:3000"
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/")
async def root():
    return {"message": "Service is running!"}


@app.head("/")
async def head_root():
    return {"message": "Service is running!"}

@app.get("/healthcheck")
async def healthcheck():
    print("Healthcheck endpoint accessed.")  # Log for debugging
    if os.environ.get("HEALTHCHECK") == "true":
        return JSONResponse(content={"status": "healthy"}, status_code=200)
    else:
        return JSONResponse(content={"status": "unhealthy"}, status_code=500)
    
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    try:
        while True:
            print("Waiting for message...")
            data = await websocket.receive_text()
            print(f"Received message: {data}")
            await send_telegram_message(data)

            response = subprocess.run(
                [sys.executable, 't.py', data],
                capture_output=True,
                text=True
            )

            print(f"Response from subprocess: {response.stdout.strip()}")
            response = response.stdout.strip()
            await send_telegram_message(response)
            await websocket.send_text(response)
    except WebSocketDisconnect:
        print("Client disconnected")
        return
    except Exception as e:
        print(f"Error: {e}")
        await websocket.close()


@app.websocket("/ws/voice")
async def voice_websocket_endpoint(
    websocket: WebSocket,
    session_id: str = Query(default=None),
):
    if not session_id:
        await websocket.close(code=4000, reason="session_id query param required")
        return

    await websocket.accept()
    print(f"[Voice] client connected  session={session_id[:8]}…")

    session = await voice_manager.get_or_create(session_id)

    async def send_json(payload: dict):
        await websocket.send_text(json.dumps(payload))

    try:
        while True:
            message = await websocket.receive()

            # ── binary frame = raw audio from MediaRecorder ──────────────────
            if message.get("bytes"):
                audio_bytes = message["bytes"]

                # 1. STT
                try:
                    transcript = await voice_manager.transcribe(audio_bytes)
                except Exception as exc:
                    print(f"[Voice] STT error: {exc}")
                    await send_json({"type": "error", "stage": "stt", "message": str(exc)})
                    continue

                if not transcript:
                    await send_json({"type": "error", "stage": "stt", "message": "Could not transcribe audio"})
                    continue

                # Send transcript so FE can display what user said
                await send_json({"type": "transcript", "text": transcript})
                await send_telegram_message(f"[Voice] User: {transcript}")

                # 2. LLM
                try:
                    reply = await voice_manager.get_llm_response(session, transcript)
                except Exception as exc:
                    print(f"[Voice] LLM error: {exc}")
                    await send_json({"type": "error", "stage": "llm", "message": str(exc)})
                    continue

                # Send text response so FE can display it
                await send_json({"type": "response_text", "text": reply})
                await send_telegram_message(f"[Voice] Bot: {reply}")

                # 3. TTS
                try:
                    audio_mp3 = await voice_manager.synthesize(reply)
                except Exception as exc:
                    print(f"[Voice] TTS error: {exc}")
                    await send_json({"type": "error", "stage": "tts", "message": str(exc)})
                    continue

                # Send audio as binary frame — FE plays it
                await websocket.send_bytes(audio_mp3)

            # ── text frame — optional ping / control messages ─────────────────
            elif message.get("text"):
                try:
                    ctrl = json.loads(message["text"])
                    if ctrl.get("type") == "ping":
                        await send_json({"type": "pong"})
                except Exception:
                    pass  # ignore malformed text

    except WebSocketDisconnect:
        print(f"[Voice] client disconnected session={session_id[:8]}…")
    except Exception as exc:
        print(f"[Voice] unexpected error: {exc}")
        try:
            await websocket.close()
        except Exception:
            pass
    finally:
        await voice_manager.purge_stale()


async def send_telegram_message(message):
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not bot_token or not chat_id:
        print("TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID is not set.")
        return

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    async with aiohttp.ClientSession() as session:
        async with session.post(url, data={'chat_id': chat_id, 'text': message}) as response:
            if response.status == 200:
                print("Message sent successfully.")
            else:
                print(f"Failed to send message: {response.status} - {await response.text()}")

if __name__ == "__main__":
    nest_asyncio.apply()
    # Use the port from Render's environment variable or fallback to 10000 if not available
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run("app:app", host="0.0.0.0", port=port, reload=False)
