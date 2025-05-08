import json
import os
import subprocess
import sys
import asyncio
import logging
import websockets
import aiohttp
from http.server import BaseHTTPRequestHandler, HTTPServer
import threading

# WebSocket client handler function (with path handling)
async def handle_client(websocket, path):
    print(f"Connection established on path: {path}")  # Log the path
    
    # If path is correct, process the message
    if path == "/ws":
        async for message in websocket:
            print(f"Received message: {message}")
            await send_telegram_message(message)

            # Call subprocess (t.py) with the message as an argument
            response = subprocess.run(
                [sys.executable, 't.py', message],
                capture_output=True,
                text=True
            )
            print(f"Response from subprocess: {response.stdout.strip()}")
            response = response.stdout.strip()

            # Send the response via WebSocket
            await send_telegram_message(response)
            await websocket.send(response)
    else:
        print(f"Received a connection on an invalid path: {path}")
        await websocket.close()

# Telegram message sending function
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

# ---- HTTP fallback server for health checks ----
def run_http_server(port):
    class HealthCheckHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OK")

    httpd = HTTPServer(("", port), HealthCheckHandler)
    httpd.serve_forever()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))

    # Start HTTP server in a thread for health check
    threading.Thread(target=run_http_server, args=(port,), daemon=True).start()

    # Start WebSocket server (without path argument in websockets.serve())
    async def main():
        async with websockets.serve(handle_client, "0.0.0.0", port):
            print(f"WebSocket server started on ws://0.0.0.0:{port}")
            await asyncio.Future()  # Keep server running indefinitely

    asyncio.run(main())
