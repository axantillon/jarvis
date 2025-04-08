# web_gateway.py
# --- FastAPI Gateway Server ---
# Purpose: Serves the static web client files and proxies WebSocket messages
#          to the main backend server (assumed running on localhost:8765).
# Changes:
# - Initial creation.

import asyncio
import websockets
import fastapi
from fastapi.staticfiles import StaticFiles
from starlette.responses import FileResponse
from starlette.websockets import WebSocket, WebSocketDisconnect
import logging
import os

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Configuration ---
# Address of the original backend WebSocket server
BACKEND_WS_URI = "ws://localhost:8765"
# Directory containing the web client files (HTML, CSS, JS)
# Assumes web_client is in the same directory as this script
STATIC_DIR = os.path.join(os.path.dirname(__file__), "web_client")

# --- FastAPI Application ---
app = fastapi.FastAPI()

# --- WebSocket Proxy Endpoint ---
async def forward_to_backend(client_ws: WebSocket, backend_ws):
    """Forwards messages from the client to the backend."""
    try:
        while True:
            data = await client_ws.receive_text()
            await backend_ws.send(data)
            logger.info(f"C->B: {data}")
    except WebSocketDisconnect:
        logger.info("Client disconnected.")
    except Exception as e:
        logger.error(f"Error receiving from client or sending to backend: {e}")
    finally:
        if not backend_ws.closed:
            await backend_ws.close()
            logger.info("Closed backend connection due to client forward error.")

async def forward_to_client(client_ws: WebSocket, backend_ws):
    """Forwards messages from the backend to the client."""
    try:
        while True:
            data = await backend_ws.recv()
            await client_ws.send_text(data)
            logger.info(f"B->C: {data}")
    except Exception as e:
        # Handle cases where backend might close unexpectedly
        if isinstance(e, websockets.exceptions.ConnectionClosedOK):
            logger.info("Backend connection closed normally.")
        elif isinstance(e, websockets.exceptions.ConnectionClosedError):
             logger.warning(f"Backend connection closed with error: {e}")
        else:
            logger.error(f"Error receiving from backend or sending to client: {e}")
    finally:
        if client_ws.client_state != client_ws.client_state.DISCONNECTED:
             try:
                 await client_ws.close()
                 logger.info("Closed client connection due to backend forward error.")
             except RuntimeError as e:
                 # Handle cases where client might already be closing
                 logger.warning(f"Error closing client connection: {e}")


@app.websocket("/ws")
async def websocket_proxy_endpoint(client_ws: WebSocket):
    """Handles client WebSocket connections and proxies to the backend."""
    await client_ws.accept()
    logger.info(f"Client connected: {client_ws.client}")
    backend_ws = None
    try:
        # Establish connection to the backend server
        logger.info(f"Connecting to backend: {BACKEND_WS_URI}")
        backend_ws = await websockets.connect(BACKEND_WS_URI)
        logger.info("Connected to backend.")

        # Run forwarding tasks concurrently
        client_to_backend_task = asyncio.create_task(
            forward_to_backend(client_ws, backend_ws)
        )
        backend_to_client_task = asyncio.create_task(
            forward_to_client(client_ws, backend_ws)
        )

        # Wait for either task to complete (which means one side disconnected)
        done, pending = await asyncio.wait(
            {client_to_backend_task, backend_to_client_task},
            return_when=asyncio.FIRST_COMPLETED,
        )

        # Cancel pending tasks to ensure cleanup
        for task in pending:
            task.cancel()
            try:
                await task # Allow cancellation to propagate
            except asyncio.CancelledError:
                logger.info("Task cancelled successfully.")

        logger.info("Forwarding tasks finished.")

    except websockets.exceptions.WebSocketException as e:
        logger.error(f"Could not connect to backend server at {BACKEND_WS_URI}: {e}")
        await client_ws.close(code=1011, reason=f"Backend connection failed: {e}")
    except Exception as e:
        logger.error(f"An unexpected error occurred in the proxy endpoint: {e}")
        await client_ws.close(code=1011, reason="Internal server error")
    finally:
        # Ensure backend connection is closed if it was opened
        if backend_ws and not backend_ws.closed:
            await backend_ws.close()
            logger.info("Backend connection closed in finally block.")
        logger.info(f"Client disconnected: {client_ws.client}")


# --- Static File Serving ---

# Serve index.html for the root path
@app.get("/")
async def get_index():
    index_path = os.path.join(STATIC_DIR, "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path)
    else:
        return fastapi.Response(content="index.html not found", status_code=404)

# Mount the rest of the static files (CSS, JS)
# Note: Adjust the path if web_client is located elsewhere relative to web_gateway.py
if os.path.exists(STATIC_DIR):
    app.mount("/", StaticFiles(directory=STATIC_DIR), name="static")
    logger.info(f"Serving static files from: {STATIC_DIR}")
else:
     logger.error(f"Static directory not found at: {STATIC_DIR}")
     logger.error("Static file serving will not work.")


# --- Main Execution (for running with uvicorn) ---
if __name__ == "__main__":
    import uvicorn
    logger.info("Starting gateway server with Uvicorn...")
    # Use reload=True for development, remove for production
    uvicorn.run("web_gateway:app", host="0.0.0.0", port=8000, reload=True)