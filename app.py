import os
import re
import sys
import json
import subprocess
import asyncio

# Force UTF-8 stdout/stderr so Windows charmap encoding never raises UnicodeEncodeError
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

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
    allow_origins=[
        "https://portfolio.beingcogni.com",   # production (no trailing slash — matches browser Origin header)
        "http://localhost:5173",               # Vite dev server
        "http://localhost:4173",               # Vite preview
    ],
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


@app.get("/ping")
async def ping():
    """Lightweight warmup endpoint — called by the frontend on page load to wake Render free-tier instance."""
    return {"status": "pong"}

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
async def voice_websocket_endpoint(websocket: WebSocket):
    session_id = websocket.query_params.get("session_id")

    if not session_id:
        await websocket.accept()
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

                # 2. Stream LLM → sentence TTS — first audio arrives after ~1st sentence
                try:
                    full_reply = ""
                    async for sentence in voice_manager.stream_response(session, transcript):
                        if not sentence:
                            continue
                        full_reply += (" " if full_reply else "") + sentence
                        # Notify FE of each sentence for progressive display
                        await send_json({"type": "response_text_chunk", "text": sentence})
                        # Skip TTS for display-only blocks (%%SUGGESTIONS%% etc.)
                        if '%%' in sentence:
                            continue
                        # TTS this sentence immediately and stream audio
                        audio_mp3 = await voice_manager.synthesize(sentence)
                        await websocket.send_bytes(audio_mp3)
                    # Send complete text so FE can do PDF highlighting
                    if full_reply:
                        await send_json({"type": "response_text", "text": full_reply})
                        await send_telegram_message(f"[Voice] Bot: {full_reply}")
                except Exception as exc:
                    print(f"[Voice] stream error: {exc}")
                    await send_json({"type": "error", "stage": "stream", "message": str(exc)})
                    continue

            # ── text frame — ping / greeting / jd_text control messages ─────────
            elif message.get("text"):
                try:
                    ctrl = json.loads(message["text"])

                    if ctrl.get("type") == "ping":
                        await send_json({"type": "pong"})

                    elif ctrl.get("type") == "greeting":
                        # Send welcome greeting — no LLM, just TTS
                        greeting = voice_manager.get_greeting_text()
                        await send_json({"type": "response_text", "text": greeting})
                        try:
                            audio_mp3 = await voice_manager.synthesize(greeting)
                            await websocket.send_bytes(audio_mp3)
                        except Exception as exc:
                            print(f"[Voice] greeting TTS error: {exc}")

                    elif ctrl.get("type") == "text_query":
                        query = ctrl.get("text", "").strip()
                        if query:
                            await send_json({"type": "transcript", "text": query})
                            await send_telegram_message(f"[Voice/Query] {query}")
                            try:
                                full_reply = ""
                                async for sentence in voice_manager.stream_response(session, query):
                                    if not sentence:
                                        continue
                                    full_reply += (" " if full_reply else "") + sentence
                                    await send_json({"type": "response_text_chunk", "text": sentence})
                                    # Skip TTS for display-only blocks (%%SUGGESTIONS%% etc.)
                                    if '%%' in sentence:
                                        continue
                                    audio_mp3 = await voice_manager.synthesize(sentence)
                                    await websocket.send_bytes(audio_mp3)
                                if full_reply:
                                    await send_json({"type": "response_text", "text": full_reply})
                            except Exception as exc:
                                print(f"[Voice] text query error: {exc}")
                                await send_json({"type": "error", "stage": "query", "message": str(exc)})

                    elif ctrl.get("type") == "jd_text":
                        jd = ctrl.get("text", "").strip()
                        if jd:
                            user_message = (
                                "A recruiter has shared the following job description. "
                                "Analyze how well Shubhanker Goswami fits this role — "
                                "3-4 conversational voice sentences, then append the %%SCORECARD%% block "
                                "as instructed in your rules:\n\n" + jd
                            )
                            await send_telegram_message(f"[Voice/JD] {jd[:300]}")
                            try:
                                # Non-streaming for JD — allows scorecard extraction before TTS
                                full_reply = await voice_manager.get_llm_response(
                                    session, user_message, max_tokens=700
                                )
                                if full_reply:
                                    # Strip all display-only blocks (scorecard, suggestions) before TTS
                                    voice_text = re.sub(
                                        r'%%\w+%%.*', '', full_reply, flags=re.DOTALL
                                    ).strip()
                                    if voice_text:
                                        audio_mp3 = await voice_manager.synthesize(voice_text)
                                        await websocket.send_bytes(audio_mp3)
                                    # Send full reply (scorecard included) for chat display
                                    await send_json({"type": "response_text", "text": full_reply})
                                    await send_telegram_message(f"[Voice/JD Bot] {full_reply}")
                            except Exception as exc:
                                print(f"[Voice] JD analysis error: {exc}")
                                await send_json({"type": "error", "stage": "jd", "message": str(exc)})

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
