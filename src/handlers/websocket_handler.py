# src/handlers/websocket_handler.py
# --- Layer: WebSocket Handler ---
# Purpose: Manages WebSocket connections, handles client IDENTIFICATION,
#          routes messages to the orchestrator with user-specific context,
#          and streams responses back to the client.
# Changes:
# - Implemented email-based authentication for incoming connections.
# - Stores user-specific system prompts per session.
# - Passes user-specific system prompt to the orchestrator.
# - Updated authentication to use email/password with bcrypt.
# - Added logging for message receipt and response sending.
# - Refactored logging: Removed basicConfig, replaced print with logger calls.
# - CORRECTED: Removed all password authentication logic. Expects 'identify' message.
# - Added handling for ToolResultData and RePromptContext in _format_response_part. (Renamed from InternalMonologue)

import asyncio
import json
import uuid
import traceback
from typing import Dict, Set, Optional
import logging

import websockets
from websockets.server import WebSocketServerProtocol
from websockets.exceptions import ConnectionClosedOK, ConnectionClosedError

# import bcrypt # REMOVED bcrypt import

from src.core.orchestrator import ConversationOrchestrator
from src.core.llm_service import (
    LLMResponsePart, TextChunk, ToolCallIntent, ErrorInfo, EndOfTurn, LLMConfig,
    ToolResultData,
    RePromptContext # Updated import
)

from dataclasses import dataclass

logger = logging.getLogger(__name__)

@dataclass
class AuthenticatedSession: # Renaming might be confusing now, but structure is ok
    email: str
    system_prompt: str

class WebSocketHandler:
    """Handles WebSocket communication, identification, and message routing."""

    def __init__(self, orchestrator: ConversationOrchestrator, base_system_prompt_template: str, authorized_users: Dict[str, Dict]):
        """
        Initializes the WebSocketHandler.

        Args:
            orchestrator: An instance of ConversationOrchestrator.
            base_system_prompt_template: The base system prompt template.
            authorized_users: Dict mapping emails to user data (expects 'prompt_addition').
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
        """Registers a new connection and generates a session ID."""
        session_id = str(uuid.uuid4())
        self._connections[websocket] = session_id
        self._sessions[session_id] = websocket
        logger.info(f"Connection registered with Session ID: {session_id} (Peer: {websocket.remote_address})")
        return session_id

    async def _unregister_connection(self, websocket: WebSocketServerProtocol):
        """Unregisters a connection upon disconnection and cleans up session data."""
        if websocket in self._connections:
            session_id = self._connections.pop(websocket)
            email = self._authenticated_sessions.get(session_id, AuthenticatedSession(email="<unknown>", system_prompt=""))
            if session_id in self._sessions:
                del self._sessions[session_id]
            if session_id in self._authenticated_sessions:
                del self._authenticated_sessions[session_id]
            logger.info(f"Connection unregistered for Session ID: {session_id} (User: {email.email})")
        else:
            logger.warning("Attempted to unregister an unknown connection.")

    def _format_response_part(self, part: LLMResponsePart) -> Optional[Dict]:
        """Formats an LLMResponsePart into a JSON serializable dict for the client."""
        payload: Optional[Dict] = None
        if isinstance(part, TextChunk):
             payload = {"type": "text", "payload": {"content": part.content}}
        elif isinstance(part, ToolCallIntent):
             # Note: Arguments might not be safely JSON serializable directly
             # Consider adding a check or conversion here if needed.
             payload = {"type": "status", "payload": {"state": "calling_tool", "tool": part.tool_name, "message": f"Attempting to use tool: {part.tool_name}", "arguments": part.arguments}}
        elif isinstance(part, ToolResultData):
             # Note: part.result could be complex. Ensure it's JSON serializable.
             # We might need error handling or selective serialization here.
             try:
                # Attempt to serialize, assuming result is mostly JSON-friendly
                 payload = {"type": "tool_result", "payload": {"tool_name": part.tool_name, "result": part.result}}
             except TypeError as e:
                 logger.warning(f"Could not serialize tool result data for {part.tool_name}: {e}. Sending simplified error.")
                 payload = {"type": "tool_result", "payload": {"tool_name": part.tool_name, "result": {"error": "Result data not JSON serializable", "type": str(type(part.result))}}}
        elif isinstance(part, RePromptContext): # Updated type check
             # The message is already a ChatMessage TypedDict, which should be serializable
             payload = {"type": "re_prompt_context", "payload": {"message": part.message}} # Updated message type
        elif isinstance(part, ErrorInfo):
             payload = {"type": "error", "payload": {"message": part.message, "details": part.details}}
        elif isinstance(part, EndOfTurn):
             payload = {"type": "end", "payload": {}}
        return payload

    async def handle_connection(self, websocket: WebSocketServerProtocol):
        """Handles a single WebSocket connection lifecycle, including identification."""
        session_id = await self._register_connection(websocket)
        identified = False # Changed flag name
        user_session: Optional[AuthenticatedSession] = None

        try:
            # --- Identification Phase --- Changed from Authentication
            logger.info(f"Handler ({session_id}): Waiting for identification message...")
            try:
                identify_message_str = await websocket.recv()
                logger.info(f"Handler ({session_id}): Received raw identify message: {identify_message_str}")

                identify_message = json.loads(identify_message_str)

                # --- Expect 'identify' type with 'email' ---
                if identify_message.get("type") == "identify" and "email" in identify_message:
                    email = identify_message["email"]
                    logger.info(f"Handler ({session_id}): Attempting identification for user: {email}")

                    # Check if email exists in our config (for prompt_addition)
                    if email in self.authorized_users:
                        user_data = self.authorized_users[email]
                        persona_definition = user_data.get("prompt_addition", "") # Default to empty if missing

                        # --- Identification successful ---
                        identified = True
                        logger.info(f"Handler ({session_id}): User '{email}' identified successfully.")

                        # Format the final prompt using the template and persona
                        try:
                            final_system_prompt = self.base_system_prompt_template.format(
                                persona_definition=persona_definition
                            )
                        except KeyError as e:
                            logger.warning(f"Handler ({session_id}): Placeholder {e} not found in template, using raw template.", exc_info=True)
                            final_system_prompt = self.base_system_prompt_template # Fallback

                        user_session = AuthenticatedSession(email=email, system_prompt=final_system_prompt)
                        self._authenticated_sessions[session_id] = user_session # Store session info
                        logger.debug(f"Handler ({session_id}): Stored identified session for {email}.")

                        # Send success message (renamed from auth_success for clarity)
                        await websocket.send(json.dumps({
                            "type": "identify_success",
                            "payload": {"sessionId": session_id}
                        }))

                    else:
                        # Email not found in config
                        identified = False
                        logger.warning(f"Handler ({session_id}): Identification failed - Email not found in configuration: {email}")
                        await websocket.send(json.dumps({
                            "type": "identify_fail", # Renamed message type
                            "payload": {"message": "Email not recognized."}
                        }))

                else:
                    # Invalid message format
                    identified = False
                    logger.warning(f"Handler ({session_id}): Identification failed - Invalid message type ('{identify_message.get('type')}') or missing fields.")
                    await websocket.send(json.dumps({"type": "identify_fail", "payload": {"message": "Invalid identification request format."}}))

            except json.JSONDecodeError:
                 logger.warning(f"Handler ({session_id}): Identification failed - Invalid JSON received.")
                 await websocket.send(json.dumps({"type": "identify_fail", "payload": {"message": "Invalid JSON during identification."}}))
                 identified = False
            except ConnectionClosedOK:
                 logger.info(f"Handler ({session_id}): Connection closed by client during identification.")
                 identified = False
            except Exception as e:
                 logger.error(f"Handler ({session_id}): Unexpected error during identification: {e}", exc_info=True)
                 await websocket.send(json.dumps({"type": "identify_fail", "payload": {"message": f"Server error during identification."}}))
                 identified = False

            # If identification failed for any reason, close the connection
            if not identified:
                await websocket.close(code=1008, reason="Identification Failed") # 1008 = Policy Violation
                return # Stop processing for this connection

            # --- Main message loop (only if identified) ---
            logger.info(f"Handler ({session_id}, {user_session.email}): Identification successful, entering message loop.")
            async for message_str in websocket:
                logger.debug(f"Handler ({session_id}, {user_session.email}): Received raw: '{message_str[:100]}...'")

                if not user_session: # Should not happen if identified
                     logger.critical(f"Handler ({session_id}): CRITICAL - Missing user session data despite identification. Closing.")
                     await websocket.close(code=1011, reason="Internal Server Error")
                     break

                try:
                    message = json.loads(message_str)
                    msg_type = message.get("type")
                    payload = message.get("payload")

                    if msg_type == "message" and payload and "text" in payload:
                        user_text = payload["text"]
                        logger.info(f"Handler ({session_id}, {user_session.email}): Processing user text: '{user_text[:100]}...'")
                        llm_config = LLMConfig({})
                        async for part in self.orchestrator.handle_input(
                            session_id=session_id,
                            text=user_text,
                            llm_config=llm_config,
                            system_prompt=user_session.system_prompt
                        ):
                            formatted_part = self._format_response_part(part)
                            if formatted_part:
                                try:
                                    response_json = json.dumps(formatted_part)
                                    logger.debug(f"Handler ({session_id}, {user_session.email}): Sending part: {response_json[:150]}...")
                                    await websocket.send(response_json)
                                except TypeError as e:
                                     # This might happen if complex non-serializable objects leak into tool results/arguments
                                     logger.error(f"Handler ({session_id}): Failed to serialize response part of type {formatted_part.get('type')}: {e}", exc_info=True)
                                     # Send a generic error back to the client
                                     error_payload = {"type": "error", "payload": {"message": "Internal server error: Failed to serialize response part.", "details": f"Type: {formatted_part.get('type')}, Error: {e}"}}
                                     await websocket.send(json.dumps(error_payload))
                            else:
                                # Handle case where _format_response_part returns None (though it shouldn't with current logic)
                                logger.warning(f"Handler ({session_id}, {user_session.email}): Formatted part was None for orchestrator part type: {type(part)}")
                    else:
                        logger.warning(f"Handler ({session_id}, {user_session.email}): Received unknown message type or format: {msg_type}")
                        await websocket.send(json.dumps({"type": "error", "payload": {"message": "Unknown message type received."}}))

                except json.JSONDecodeError:
                    logger.warning(f"Handler ({session_id}, {user_session.email}): Invalid JSON received in message loop.")
                    await websocket.send(json.dumps({"type": "error", "payload": {"message": "Invalid JSON received."}}))
                except ConnectionClosedOK:
                    logger.info(f"Handler ({session_id}, {user_session.email}): Client closed connection.")
                    break
                except ConnectionClosedError as e:
                    logger.warning(f"Handler ({session_id}, {user_session.email}): Connection closed with error: {e}")
                    break
                except Exception as e:
                    logger.error(f"Handler ({session_id}, {user_session.email}): Error processing message: {e}", exc_info=True)
                    try:
                        await websocket.send(json.dumps({"type": "error", "payload": {"message": f"Internal server error: {e}"}}))
                    except ConnectionClosedError:
                        logger.warning(f"Handler ({session_id}, {user_session.email}): Could not send error message, connection already closed.")
                    break

        except Exception as e:
            logger.error(f"Handler ({session_id}): Unhandled error in connection handler: {e}", exc_info=True)
        finally:
            await self._unregister_connection(websocket)

    async def start_server(self, host: str, port: int):
        """Starts the WebSocket server."""
        # The `serve` function runs forever until stopped (e.g., Ctrl+C)
        logger.info(f"Starting WebSocket server on ws://{host}:{port}...")
        async with websockets.serve(self.handle_connection, host, port):
            await asyncio.Future()  # Run forever
