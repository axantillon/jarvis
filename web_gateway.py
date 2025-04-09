# JARVIS was here!
# web_gateway.py
# JARVIS was here!
# --- FastAPI Gateway Server ---
# Purpose: Serves the static web client files, authenticates users via WebSocket,
#          and proxies messages to the main backend server.
# Changes:
# - Initial creation.
# - Added authentication logic to the /ws endpoint.
# - Gateway now expects first client message to be auth credentials.
# - Gateway sends an identification message to the backend upon successful auth.
# - Loads user hashes from environment variables (requires TONY_HASH, PETER_HASH, etc.).
# - Requires bcrypt: pip install bcrypt

import asyncio
import websockets
import fastapi
from fastapi.staticfiles import StaticFiles
from starlette.responses import FileResponse
from starlette.websockets import WebSocket, WebSocketDisconnect
import logging
import os
import time
import json # Added for parsing auth messages

# --- Password Hashing --- #
# Requires: pip install bcrypt
try:
    import bcrypt
    logger.info("bcrypt library loaded successfully.")
except ImportError:
    logger.error("CRITICAL ERROR: bcrypt library not found. Please install it: pip install bcrypt")
    # Decide how to handle this - exit? Or let it fail later?
    bcrypt = None # Set to None to cause errors later if not installed

# Utility function to verify a password
def verify_password(plain_password: str, hashed_password_str: str) -> bool:
    if not bcrypt:
        logger.error("Cannot verify password, bcrypt library is missing.")
        return False
    if not isinstance(plain_password, str) or not isinstance(hashed_password_str, str):
         logger.warning("Invalid types received for password verification.")
         return False
    try:
        hashed_password_bytes = hashed_password_str.encode('utf-8') # Assume hash is stored as string
        # Decode if it's base64 encoded? Usually bcrypt hashes are stored directly as strings
        # containing the salt and hash. Let's assume direct UTF-8 encoding works.
        # If hashes are stored base64 encoded, you'd need:
        # import base64
        # hashed_password_bytes = base64.b64decode(hashed_password_str)

        password_bytes = plain_password.encode('utf-8')
        return bcrypt.checkpw(password_bytes, hashed_password_bytes)
    except ValueError as e:
        logger.error(f"Error during bcrypt verification (likely malformed hash): {e}")
        return False
    except Exception as e:
        logger.error(f"Unexpected error during password verification: {e}")
        return False
# --- End Password Hashing --- #


# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Configuration ---
# Address of the original backend WebSocket server
BACKEND_WS_URI = "ws://localhost:8765"
# Directory containing the web client files (HTML, CSS, JS)
# Assumes web_client is in the same directory as this script
STATIC_DIR = os.path.join(os.path.dirname(__file__), "web_client")

# --- Load User Authorization Configuration (Hashes only) ---
# Load from environment variables like the main app does
# Ensure TONY_HASH and PETER_HASH are set in the gateway's environment.
AUTHORIZED_USER_HASHES = {
    "axantillon@gmail.com": os.environ.get("TONY_HASH"),
    "aguilarcarboni@gmail.com": os.environ.get("PETER_HASH"),
    # Add other users here if needed, mapping email to ENV VAR NAME for hash
}

# --- Validate Loaded Hashes ---
def validate_auth_hashes(auth_dict):
    all_valid = True
    logger.info("Validating loaded authentication hashes...")
    for email, hashed_pw in auth_dict.items():
        if not hashed_pw:
            logger.error(f"CRITICAL ERROR: Missing hashed_password env var for user '{email}'. Authentication for this user will fail.")
            all_valid = False
        else:
            # Basic check: bcrypt hashes usually start with '$2'
            if not isinstance(hashed_pw, str) or not hashed_pw.startswith('$2'):
                 logger.warning(f"Potential issue: Hash for user '{email}' does not look like a standard bcrypt hash.")
            logger.info(f"Hash found for user '{email}'.") # Don't log the hash itself!
    if all_valid:
        logger.info("All configured user hashes seem to be present.")
    else:
        logger.error("One or more user hashes are missing from environment variables.")
    return all_valid

AUTH_HASHES_VALID = validate_auth_hashes(AUTHORIZED_USER_HASHES)
# Note: We proceed even if not all are valid, but log errors.
# --- End User Authorization Configuration & Validation ---

# --- FastAPI Application ---
app = fastapi.FastAPI()

# --- Health Check Endpoint ---
# Dedicated endpoint for health checks (handles GET and HEAD)
@app.api_route("/healthz", methods=["GET", "HEAD"])
async def health_check():
    """Simple health check endpoint."""
    return fastapi.Response(content="OK", status_code=200)

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
    """Handles client WebSocket connections, authenticates, and proxies to the backend."""
    await client_ws.accept()
    logger.info(f"Client connected: {client_ws.client}. Waiting for authentication...")
    backend_ws = None
    authenticated_email = None

    try:
        # 1. Receive Authentication Message
        auth_data_raw = await client_ws.receive_text()
        logger.info(f"Received auth message from client: {auth_data_raw}") # Be cautious logging raw data if sensitive
        try:
            auth_data = json.loads(auth_data_raw)
            if not isinstance(auth_data, dict) or auth_data.get("type") != "auth":
                raise ValueError("Invalid auth message format or type")
            email = auth_data.get("email")
            password = auth_data.get("password")
            if not email or not password:
                raise ValueError("Missing email or password in auth message")
        except (json.JSONDecodeError, ValueError) as e:
            logger.warning(f"Failed to parse auth message or invalid format: {e}")
            await client_ws.send_text(json.dumps({"type": "auth_fail", "reason": f"Invalid auth message format: {e}"}))
            await client_ws.close(code=1008, reason="Invalid auth message format")
            return # Close connection

        # 2. Verify Credentials
        logger.info(f"Attempting authentication for user: {email}")
        hashed_password = AUTHORIZED_USER_HASHES.get(email)

        if not hashed_password:
            logger.warning(f"Authentication failed: User '{email}' not found or hash missing.")
            await client_ws.send_text(json.dumps({"type": "auth_fail", "reason": "User not found or not configured."}))
            await client_ws.close(code=1008, reason="Authentication failed")
            return

        if verify_password(password, hashed_password):
            logger.info(f"Authentication successful for user: {email}")
            authenticated_email = email
            await client_ws.send_text(json.dumps({"type": "auth_success"}))
        else:
            logger.warning(f"Authentication failed for user: {email} (Incorrect password)")
            await client_ws.send_text(json.dumps({"type": "auth_fail", "reason": "Incorrect password"}))
            await client_ws.close(code=1008, reason="Authentication failed")
            return

        # 3. Connect to Backend (Only after successful authentication)
        if authenticated_email:
            connect_attempts = 0
            max_attempts = 10
            initial_delay = 1.0
            max_delay = 10.0
            timeout_seconds = 60
            start_time = time.monotonic()

            while True:
                current_time = time.monotonic()
                if current_time - start_time > timeout_seconds:
                    logger.error(f"Backend connection timed out after {timeout_seconds} seconds for user {authenticated_email}.")
                    raise websockets.exceptions.WebSocketException("Backend connection timeout")

                connect_attempts += 1
                logger.info(f"Attempting backend connection for {authenticated_email}: {BACKEND_WS_URI} (Attempt {connect_attempts}/{max_attempts})")
                try:
                    backend_ws = await websockets.connect(BACKEND_WS_URI)
                    logger.info(f"Connected to backend successfully for user: {authenticated_email}.")
                    break # Exit loop
                except Exception as e: # Catch broader exceptions during connect retry
                    logger.warning(f"Backend connection attempt {connect_attempts} failed for {authenticated_email}: {e}. Retrying...")
                    if connect_attempts >= max_attempts:
                        logger.error(f"Failed to connect to backend after {max_attempts} attempts for {authenticated_email}.")
                        raise e # Re-raise last error

                    delay = min(initial_delay * (2 ** (connect_attempts - 1)), max_delay)
                    logger.info(f"Waiting {delay:.2f} seconds before next retry...")
                    await asyncio.sleep(delay)

            # 4. Send Identification to Backend
            identify_message = json.dumps({"type": "identify", "email": authenticated_email})
            logger.info(f"Sending identification to backend: {identify_message}")
            await backend_ws.send(identify_message)

            # 5. Start Proxying Messages
            logger.info(f"Starting message proxying for user: {authenticated_email}")
            client_to_backend_task = asyncio.create_task(
                forward_to_backend(client_ws, backend_ws) # Pass authenticated email if needed by forwarder? No, just log maybe.
            )
            backend_to_client_task = asyncio.create_task(
                forward_to_client(client_ws, backend_ws)
            )

            done, pending = await asyncio.wait(
                {client_to_backend_task, backend_to_client_task},
                return_when=asyncio.FIRST_COMPLETED,
            )

            for task in pending:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    logger.info(f"Proxy task cancelled for {authenticated_email}.")

            logger.info(f"Forwarding tasks finished for {authenticated_email}.")

    except WebSocketDisconnect:
         logger.info(f"Client {client_ws.client} disconnected {'before authentication.' if not authenticated_email else f'(as {authenticated_email}).'}")
    except websockets.exceptions.WebSocketException as e:
        logger.error(f"WebSocket Error (Backend Connection Failed?): {e} for client {client_ws.client} {f'(user: {authenticated_email})' if authenticated_email else ''}")
        # Ensure client connection is closed if backend fails
        if client_ws.client_state != client_ws.client_state.DISCONNECTED:
            await client_ws.close(code=1011, reason=f"Backend connection failed: {e}")
    except Exception as e:
        logger.error(f"Unexpected error in proxy endpoint for {client_ws.client} {f'(user: {authenticated_email})' if authenticated_email else ''}: {e}", exc_info=True)
        if client_ws.client_state != client_ws.client_state.DISCONNECTED:
            await client_ws.close(code=1011, reason="Internal server error")
    finally:
        if backend_ws and backend_ws.open:
            await backend_ws.close()
            logger.info(f"Backend connection closed in finally block for {authenticated_email or 'unauthenticated client'}.")
        logger.info(f"Client connection closed: {client_ws.client} {f'(user: {authenticated_email})' if authenticated_email else ''}")


# --- Static File Serving ---
# Reverted to explicit GET/HEAD handlers for /, plus StaticFiles mount

# Serve index.html for the root path GET requests
@app.get("/")
async def get_index():
    index_path = os.path.join(STATIC_DIR, "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path)
    else:
        return fastapi.Response(content="index.html not found", status_code=404)

# Explicitly handle HEAD requests for the root path (often used by health checks)
@app.head("/")
async def head_index():
    index_path = os.path.join(STATIC_DIR, "index.html")
    if os.path.exists(index_path):
        # FileResponse automatically handles HEAD requests appropriately
        return FileResponse(index_path)
    else:
        # If index.html doesn't exist, return 404 for HEAD too
        return fastapi.Response(content="", status_code=404)

# Mount the rest of the static files (CSS, JS) - MUST come AFTER explicit routes
if os.path.exists(STATIC_DIR):
    app.mount("/", StaticFiles(directory=STATIC_DIR), name="static")
    logger.info(f"Serving static files from: {STATIC_DIR}")
else:
     logger.error(f"Static directory not found at: {STATIC_DIR}")
     logger.error("Static file serving will not work.")


# --- Main Execution (for running with uvicorn) ---
if __name__ == "__main__":
    import uvicorn
    # Ensure bcrypt is available before starting
    if not bcrypt:
         logger.critical("bcrypt library is missing. Server cannot start.")
    else:
        logger.info("Starting gateway server with Uvicorn...")
        # Use reload=True for development, remove for production
        uvicorn.run("web_gateway:app", host="0.0.0.0", port=8000, reload=True)