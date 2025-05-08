import json
import os
import openai
import websockets
import asyncio
import logging
from websockets.asyncio.server import serve

import json
import aiohttp

import openai
import os

import subprocess
import sys






async def handle_client(websocket):
    async for message in websocket:
        # Process the incoming message
        print(f"Received message: {message}")
        await send_telegram_message(message)
        response = subprocess.run(
            [sys.executable, 't.py', message],
            capture_output=True,
            text=True
        )
        print(f"Response from subprocess: {response.stdout.strip()}")
        response = response.stdout.strip()
        await send_telegram_message(response)
        await websocket.send(response)




async def send_telegram_message(message):
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not bot_token:
        print("TELEGRAM_BOT_TOKEN is not set.")
        return
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not chat_id:
        print("TELEGRAM_CHAT_ID is not set.")
        return
    
    async with aiohttp.ClientSession() as session:
        async with session.post(url, data={'chat_id': chat_id, 'text': message}) as response:
            if response.status == 200:
                print("Message sent successfully.")
            else:
                print("Failed to send message.")
                print(f"Error: {response.status} - {await response.text()}")



if __name__ == "__main__":
    async def main():
        port = int(os.environ.get("PORT", 8080))
        async with websockets.serve(handle_client, "0.0.0.0", port):
            print("Server started on ws://localhost:8080")
            await asyncio.Future()  # Run forever

    asyncio.run(main())