# src/handlers/websocket_handler.py
# --- Layer: WebSocket Handler ---
# Purpose: Manages WebSocket connections, handles initial user authentication,
#          routes messages to the orchestrator with user-specific context,
#          and streams responses back to the client.
# Changes:
# - Implemented email-based authentication for incoming connections.
# - Stores user-specific system prompts per session.
# - Passes user-specific system prompt to the orchestrator.
# - Updated authentication to use email/password with bcrypt.
# - Added logging for message receipt and response sending.

import asyncio
import json
import uuid
import traceback
from typing import Dict, Set, Optional
import logging

# Use 'websockets' library for handling WebSocket connections
# pip install websockets
import websockets
from websockets.server import WebSocketServerProtocol
from websockets.exceptions import ConnectionClosedOK, ConnectionClosedError

# Import bcrypt for password hashing
import bcrypt

# Import core components and types
from src.core.orchestrator import ConversationOrchestrator
from src.core.llm_service import (
    LLMResponsePart,
    TextChunk,
    ToolCallIntent,
    ErrorInfo,
    EndOfTurn,
    LLMConfig # If we want to pass config from client someday
)

# Define a structure to hold authenticated session data
from dataclasses import dataclass

# Configure logging (ensure it's configured, possibly redundant if done elsewhere)
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

@dataclass
class AuthenticatedSession:
    email: str
    system_prompt: str # This will now store the fully formatted prompt

class WebSocketHandler:
    """Handles WebSocket communication with frontend clients."""

    def __init__(self, orchestrator: ConversationOrchestrator, base_system_prompt_template: str, authorized_users: Dict[str, Dict]):
        """
        Initializes the WebSocketHandler.

        Args:
            orchestrator: An instance of ConversationOrchestrator.
            base_system_prompt_template: The base system prompt template with placeholders.
            authorized_users: A dictionary mapping authorized emails to user data (hash, prompt_addition).
        """
        if not isinstance(orchestrator, ConversationOrchestrator):
            raise TypeError("orchestrator must be an instance of ConversationOrchestrator")
        if not isinstance(base_system_prompt_template, str):
             raise TypeError("base_system_prompt_template must be a string")
        if not isinstance(authorized_users, dict):
             raise TypeError("authorized_users must be a dictionary")

        self.orchestrator = orchestrator
        self.base_system_prompt_template = base_system_prompt_template
        self.authorized_users = authorized_users
        # Keep track of active connections and their associated session IDs
        self._connections: Dict[WebSocketServerProtocol, str] = {}
        # Store authenticated user data (email, specific prompt) per session ID
        self._authenticated_sessions: Dict[str, AuthenticatedSession] = {}
        # Map session IDs back to websockets for potential direct messaging (use with care)
        self._sessions: Dict[str, WebSocketServerProtocol] = {}

    async def _register_connection(self, websocket: WebSocketServerProtocol) -> str:
        """Registers a new connection and generates a session ID. Authentication happens later."""
        session_id = str(uuid.uuid4())
        self._connections[websocket] = session_id
        self._sessions[session_id] = websocket # Store websocket reference by session_id
        print(f"Handler: Connection registered with Session ID: {session_id}")
        return session_id

    async def _unregister_connection(self, websocket: WebSocketServerProtocol):
        """Unregisters a connection upon disconnection and cleans up session data."""
        if websocket in self._connections:
            session_id = self._connections.pop(websocket)
            if session_id in self._sessions:
                del self._sessions[session_id]
            # Clean up authenticated session data as well
            if session_id in self._authenticated_sessions:
                del self._authenticated_sessions[session_id]
            print(f"Handler: Connection unregistered for Session ID: {session_id}")
        else:
            print("Handler: Attempted to unregister an unknown connection.")

    def _format_response_part(self, part: LLMResponsePart) -> Optional[Dict]:
        """Formats an LLMResponsePart into a JSON serializable dict for the client."""
        payload: Optional[Dict] = None
        if isinstance(part, TextChunk):
            payload = {"type": "text", "payload": {"content": part.content}}
        elif isinstance(part, ToolCallIntent):
            # Send a status update indicating tool use is starting
            # The actual result comes later if the orchestrator feeds it back
            payload = {
                "type": "status",
                "payload": {
                    "state": "processing",
                    "tool": part.tool_name,
                    "message": f"Attempting to use tool: {part.tool_name}",
                    "arguments": part.arguments # Send args for potential display/debug
                }
            }
        elif isinstance(part, ErrorInfo):
            payload = {
                "type": "error",
                "payload": {
                    "message": part.message,
                    "details": part.details # Include details if present
                }
            }
        elif isinstance(part, EndOfTurn):
             payload = {"type": "end", "payload": {}} # Simple end signal

        return payload

    async def handle_connection(self, websocket: WebSocketServerProtocol):
        """Handles a single WebSocket connection lifecycle, including authentication."""
        session_id = await self._register_connection(websocket)
        authenticated = False
        user_session: Optional[AuthenticatedSession] = None

        try:
            # --- Authentication Phase ---
            print(f"Handler ({session_id}): Waiting for authentication message...")
            try:
                # Remove asyncio.wait_for, wait indefinitely for the first message
                # auth_message_str = await asyncio.wait_for(websocket.recv(), timeout=10.0)
                auth_message_str = await websocket.recv()
                auth_message = json.loads(auth_message_str)

                if auth_message.get("type") == "auth" and "email" in auth_message and "password" in auth_message:
                    email = auth_message["email"]

                    # Check if email exists in our config
                    if email in self.authorized_users:
                        user_data = self.authorized_users[email]
                        stored_hashed_pw_str = user_data.get("hashed_password")
                        persona_definition = user_data.get("prompt_addition", "") # Default to empty if missing

                        # Verify password using bcrypt
                        password_provided = auth_message["password"]
                        if not stored_hashed_pw_str or not isinstance(stored_hashed_pw_str, str):
                            print(f"Handler ({session_id}): Auth failed - Missing/invalid stored hash for {email}.")
                            authenticated = False # Treat missing hash as failure
                        else:
                            stored_hashed_pw_bytes = stored_hashed_pw_str.encode('utf-8')
                            password_provided_bytes = password_provided.encode('utf-8')
                            # Perform the check
                            try:
                                if bcrypt.checkpw(password_provided_bytes, stored_hashed_pw_bytes):
                                    # Password matches!
                                    authenticated = True
                                    print(f"Handler ({session_id}): User '{email}' authenticated successfully (password verified).")
                                else:
                                     # Password does not match
                                     authenticated = False
                                     print(f"Handler ({session_id}): Authentication failed - Incorrect password for {email}.")
                            except ValueError:
                                print(f"Handler ({session_id}): Auth failed - Invalid hash format stored for {email}.")
                                authenticated = False

                        # If authenticated, store session data
                        if authenticated:
                            # Format the final prompt using the template and persona
                            try:
                                final_system_prompt = self.base_system_prompt_template.format(
                                    persona_definition=persona_definition
                                )
                            except KeyError as e:
                                print(f"Handler ({session_id}): WARNING - Placeholder {e} not found in template, using raw template.")
                                final_system_prompt = self.base_system_prompt_template # Fallback

                            user_session = AuthenticatedSession(email=email, system_prompt=final_system_prompt)
                            self._authenticated_sessions[session_id] = user_session
                            await websocket.send(json.dumps({
                                "type": "auth_success",
                                "payload": {"sessionId": session_id}
                            }))

                    else:
                        # Email not found OR password incorrect
                        if not authenticated: # Only print password failure message if it already failed
                             # Send generic failure message (don't reveal *why* it failed to client)
                             print(f"Handler ({session_id}): Authentication failed for email {email} (reason logged above).")
                             await websocket.send(json.dumps({
                                  "type": "auth_failed",
                                  "payload": {"message": "Authentication failed."}
                             }))

                else:
                    # Email not found in config
                    print(f"Handler ({session_id}): Authentication failed - Unauthorized email: {email}")
                    await websocket.send(json.dumps({
                        "type": "auth_failed",
                        "payload": {"message": "Authentication failed."}
                    }))
            except json.JSONDecodeError:
                 print(f"Handler ({session_id}): Authentication failed - Invalid JSON.")
                 await websocket.send(json.dumps({"type": "auth_failed", "payload": {"message": "Invalid JSON during authentication."}}))
            except Exception as e:
                 print(f"Handler ({session_id}): Authentication error: {e}")
                 await websocket.send(json.dumps({"type": "auth_failed", "payload": {"message": f"Server error during authentication: {e}"}}))
                 # Don't proceed if auth fails unexpectedly
                 authenticated = False

            # If authentication failed for any reason, close the connection
            if not authenticated:
                await websocket.close(code=1008, reason="Authentication Failed") # 1008 = Policy Violation
                # Unregister will be called in the finally block
                return # Stop processing for this connection

            # --- Main message loop (only if authenticated) ---
            logger.info(f"Handler ({session_id}, {user_session.email}): Authentication successful, entering message loop.")
            async for message_str in websocket:
                logger.info(f"Handler ({session_id}, {user_session.email}): Received message: '{message_str[:100]}...'") # Log full message received
                # Ensure user_session is available (should always be true if authenticated)
                if not user_session:
                     logger.critical(f"Handler ({session_id}): CRITICAL - Missing user session data despite authentication. Closing.")
                     await websocket.close(code=1011, reason="Internal Server Error") # 1011 = Internal Error
                     break

                try:
                    message = json.loads(message_str)
                    msg_type = message.get("type")
                    payload = message.get("payload")

                    if msg_type == "message" and payload and "text" in payload:
                        user_text = payload["text"]
                        logger.info(f"Handler ({session_id}, {user_session.email}): Processing user text: '{user_text[:100]}...'")

                        # TODO: Allow passing LLMConfig from client if needed
                        llm_config = LLMConfig({}) # Empty config for now

                        # Track the full response text being sent
                        full_response_text = ""
                        # Call the orchestrator and stream results back
                        # Pass the user-specific system prompt from the authenticated session
                        async for part in self.orchestrator.handle_input(
                            session_id=session_id,
                            text=user_text,
                            llm_config=llm_config,
                            system_prompt=user_session.system_prompt # Pass user-specific prompt
                        ):
                            # Format and send the part to the client
                            response_payload = self._format_response_part(part)
                            if response_payload:
                                # Log the chunk being sent
                                # logger.debug(f"Handler ({session_id}, {user_session.email}): Sending part: {response_payload}")
                                # Accumulate text chunks for final log
                                if response_payload.get("type") == "text":
                                    full_response_text += response_payload.get("payload", {}).get("content", "")
                                
                                await websocket.send(json.dumps(response_payload))
                        
                        # Log the complete response text after the stream ends
                        logger.info(f"Handler ({session_id}, {user_session.email}): Finished streaming response. Full text: '{full_response_text[:200]}...'")

                    # Handle other message types if needed (e.g., config changes)
                    else:
                        logger.warning(f"Handler ({session_id}, {user_session.email}): Received unknown/unsupported message type: {msg_type}")
                
                except json.JSONDecodeError:
                    logger.error(f"Handler ({session_id}, {user_session.email}): Received invalid JSON: {message_str[:100]}...")
                    # Optionally send an error back to the client
                    # await websocket.send(json.dumps({"type": "error", "payload": {"message": "Invalid JSON received"}}))
                except Exception as e:
                    # Catch potential errors during message processing or streaming
                    error_trace = traceback.format_exc()
                    logger.error(f"Handler ({session_id}, {user_session.email}): Error handling message: {e}\n{error_trace}")
                    # Send a generic error to the client
                    try:
                        await websocket.send(json.dumps({
                            "type": "error",
                            "payload": {"message": f"An internal server error occurred: {e}", "details": error_trace}
                        }))
                    except (ConnectionClosedOK, ConnectionClosedError):
                        # Connection might already be closed
                        pass
                    # Consider whether to break the loop or continue after an error
                    # break

        except (ConnectionClosedOK, ConnectionClosedError) as e:
            logger.info(f"Handler ({session_id}): Connection closed ({type(e).__name__}).")
        except Exception as e:
            # Catch unexpected errors during the initial connection or auth phase
            error_trace = traceback.format_exc()
            logger.error(f"Handler ({session_id}): Unhandled exception in connection handler: {e}\n{error_trace}")
        finally:
            # Ensure connection is always unregistered
            await self._unregister_connection(websocket)

    async def start_server(self, host: str, port: int):
        """Starts the WebSocket server."""
        # Note: Binding to 0.0.0.0 means listen on all interfaces
        # Clients connect using the actual IP or hostname
        effective_host = host if host != "0.0.0.0" else "<all interfaces>"
        print(f"Attempting to start WebSocket server on ws://{effective_host}:{port}...")
        try:
            async with websockets.serve(self.handle_connection, host, port) as server:
                # Log after the server is successfully started and listening
                actual_host, actual_port = server.sockets[0].getsockname()[:2]
                print(f"*** WebSocket server successfully started and listening on ws://{actual_host}:{actual_port} ***")
                await asyncio.Future() # Run forever until cancelled
        except OSError as e:
             print(f"!!! FAILED to start WebSocket server on ws://{effective_host}:{port} - {e} !!!")
             # Re-raise the exception so the main application knows it failed
             raise
