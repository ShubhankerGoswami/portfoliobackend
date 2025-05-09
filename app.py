import os
import sys
import subprocess
import asyncio

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
import uvicorn
import nest_asyncio
import aiohttp

app = FastAPI()


@app.get("/healthcheck")

async def healthcheck():
    if os.environ.get("HEALTHCHECK") == "true":
        return JSONResponse(content={"status": "healthy"}, status_code=200)
    else:
        return JSONResponse(content={"status": "unhealthy"}, status_code=500)
    
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    try:
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
    #port = int(os.environ.get("PORT", 8080))
    uvicorn.run("app:app", host="0.0.0.0", port=10000, reload=False)
    
