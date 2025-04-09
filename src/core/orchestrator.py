# src/core/orchestrator.py
# --- Layer: Conversation Orchestrator ---
# Purpose: Manages conversation flow, state, LLM interaction, and tool execution.
# Changes:
# - Initial implementation.
# - Implemented handle_input logic including LLM calls and tool execution flow.
# - Modified handle_input to accept and pass a user-specific system_prompt.
# - Added logging for handle_input start/end.
# - Added detailed history message logging controlled by ORCHESTRATOR_DETAILED_LOGGING env var.
# - Improved detailed logging: Added colors, indented JSON, fixed TypeError for None content.
# - Refactored logging: Removed basicConfig, use DEBUG level for detailed logs, removed custom flag.
# - Yield ToolResultData after executing a tool.
# - Yield RePromptContext after adding tool result to history (representing re-prompt info). (Renamed from InternalMonologue)

import asyncio
import json
import os
import traceback
from typing import Dict, List, AsyncGenerator, Optional, Any, cast
import logging

# Import components and types from other modules
from .llm_service import (
    LLMService,
    ChatMessage,
    LLMResponsePart,
    TextChunk,
    ToolCallIntent,
    ErrorInfo,
    ToolDefinition,
    LLMConfig,
    EndOfTurn,
    ToolResultData,
    RePromptContext # Updated import
)
from .mcp_coordinator import MCPCoordinator, ToolRegistryEntry

# Configure logging - REMOVED basicConfig call
# logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__) # Get logger named after the module

# ANSI Color Codes for Logging - Keep these for the debug formatter
COLOR_BLUE = "\033[94m"
COLOR_GREEN = "\033[92m"
COLOR_YELLOW = "\033[93m"
COLOR_MAGENTA = "\033[95m"
COLOR_CYAN = "\033[96m"
COLOR_RESET = "\033[0m"

class ConversationOrchestrator:
    """
    Orchestrates the conversation flow between the user, LLM, and tools.
    """

    def __init__(self, llm_service: LLMService, mcp_coordinator: MCPCoordinator):
        """
        Initializes the ConversationOrchestrator.

        Args:
            llm_service: An instance of LLMService.
            mcp_coordinator: An instance of MCPCoordinator.
        """
        if not isinstance(llm_service, LLMService):
            raise TypeError("llm_service must be an instance of LLMService")
        if not isinstance(mcp_coordinator, MCPCoordinator):
             raise TypeError("mcp_coordinator must be an instance of MCPCoordinator")

        self.llm_service = llm_service
        self.mcp_coordinator = mcp_coordinator
        # Simple in-memory history storage {session_id: [ChatMessage]}
        self._histories: Dict[str, List[ChatMessage]] = {}
        # TODO: Add configuration (max history, retries, etc.) if needed
        self._max_history_len = 50 # Example: Keep last 50 messages

        # --- Removed detailed logging config ---
        # self._log_history_messages = os.environ.get("ORCHESTRATOR_DETAILED_LOGGING", "false").lower() in ("true", "1", "t")
        # if self._log_history_messages:
        #     logger.info("Orchestrator detailed history logging ENABLED.")
        # else:
        #     logger.info("Orchestrator detailed history logging DISABLED.")
        # --- End removal ---

    def _get_history(self, session_id: str) -> List[ChatMessage]:
        """Retrieves or initializes history for a session."""
        if session_id not in self._histories:
            self._histories[session_id] = []
        return self._histories[session_id]

    def _add_message(self, session_id: str, message: ChatMessage):
        """Adds a message to the history for a session, enforcing max length."""
        history = self._get_history(session_id)
        history.append(message)

        # --- Use logger.debug for detailed logging ---
        role = message['role']
        log_content = message.get('content')
        log_tool_name = message.get('tool_name')
        log_data = message.get('data')
        log_data_str = ""
        data_prefix = "" # To indicate formatting type

        # Determine color based on role
        role_color = COLOR_BLUE
        if role == 'user': role_color = COLOR_CYAN
        elif role == 'assistant': role_color = COLOR_GREEN
        elif role == 'tool': role_color = COLOR_YELLOW

        # Safely format data as indented JSON if possible
        if log_data is not None:
            try:
                log_data_str = json.dumps(log_data, indent=2, ensure_ascii=False) # Use indent=2, ensure_ascii=False
                data_prefix = "(JSON Data):" # Indicate successful JSON formatting
            except TypeError:
                log_data_str = str(log_data) # Fallback
                data_prefix = "(String Data):" # Indicate fallback formatting

        # Construct the log message with colors
        log_msg_parts = [
            f"{COLOR_MAGENTA}History Add ({session_id}){COLOR_RESET}:",
            f"Role: {role_color}{role}{COLOR_RESET}"
        ]

        if log_content is not None and isinstance(log_content, str):
             content_display = f"'{log_content[:150]}{'...' if len(log_content) > 150 else ''}'"
             log_msg_parts.append(f"Content: {COLOR_GREEN}{content_display}{COLOR_RESET}")
        elif log_content is not None:
             log_msg_parts.append(f"Content: {COLOR_GREEN}{str(log_content)}{COLOR_RESET}")

        if log_tool_name:
             log_msg_parts.append(f"ToolName: {COLOR_YELLOW}{log_tool_name}{COLOR_RESET}")

        if log_data_str:
             # Add prefix and put data on new line
             data_display = f"\n{data_prefix}\n{log_data_str}"
             if len(data_display) > 500: data_display = data_display[:500] + "\n..."
             log_msg_parts.append(f"Data: {COLOR_MAGENTA}{data_display}{COLOR_RESET}")

        logger.debug(" ".join(log_msg_parts))
        # --- End detailed logging ---

        # Simple truncation
        while len(history) > self._max_history_len:
            history.pop(0)

    def _get_tool_definitions(self) -> List[ToolDefinition]:
        """
        Extracts tool definitions from the MCPCoordinator's registry
        in the format expected by LLMService.
        """
        definitions: List[ToolDefinition] = []
        # Check if coordinator and registry exist
        if not self.mcp_coordinator or not self.mcp_coordinator.tool_registry:
             # Corrected log: Removed session_id which is not available here
             logger.warning("Attempted to get tool definitions, but MCPCoordinator or tool_registry is missing.")
             return definitions

        registry_size = len(self.mcp_coordinator.tool_registry) # Get size before loop

        for entry in self.mcp_coordinator.tool_registry.values():
             mcp_tool_def = entry.definition
             parameters = getattr(mcp_tool_def, 'inputSchema', {})
             if not isinstance(parameters, dict): parameters = {}

             definitions.append(ToolDefinition(
                  qualified_name=entry.qualified_name,
                  server_id=entry.server_id,
                  description=getattr(mcp_tool_def, 'description', 'No description available.'),
                  parameters=parameters
             ))

        # Corrected log: Removed session_id, logged count after loop
        logger.debug(f"Extracted {len(definitions)} tool definitions (from registry size {registry_size}).")
        return definitions

    async def _execute_tool_call(
        self,
        session_id: str,
        tool_intent: ToolCallIntent
    ) -> ChatMessage:
        """
        Executes a tool call and returns the resulting ChatMessage for history.

        Args:
            session_id: The session ID.
            tool_intent: The ToolCallIntent from the LLM.

        Returns:
            A ChatMessage representing the tool result (role='tool').
        """
        tool_result_message: ChatMessage
        try:
            # Use logger.info for starting, logger.debug for arguments
            logger.info(f"Orchestrator ({session_id}): Executing tool '{tool_intent.tool_name}'...")
            logger.debug(f"Orchestrator ({session_id}): Tool arguments: {tool_intent.arguments}")

            if not self.mcp_coordinator:
                 # Use logger.error for critical failures
                 logger.error(f"Orchestrator ({session_id}): MCP Coordinator not available during tool execution.")
                 raise RuntimeError("MCP Coordinator not available.")

            result_data = await self.mcp_coordinator.call_tool(
                qualified_tool_name=tool_intent.tool_name,
                arguments=tool_intent.arguments
            )
            logger.info(f"Orchestrator ({session_id}): Tool '{tool_intent.tool_name}' executed successfully.")
            # logger.debug(f"Orchestrator ({session_id}): Tool result data: {result_data}") # Optionally log result data

            tool_result_message = ChatMessage(
                role='tool',
                tool_name=tool_intent.tool_name,
                content=None,
                data=result_data
            )
        except Exception as e:
            # Use logger.error with exc_info=True for exceptions
            logger.error(f"Orchestrator ({session_id}): Error executing tool '{tool_intent.tool_name}': {e}", exc_info=True)
            # traceback.print_exc() # No longer needed

            tool_result_message = ChatMessage(
                role='tool',
                tool_name=tool_intent.tool_name,
                content=None,
                data={ # Structure the error data
                    "error": f"Tool execution failed: {type(e).__name__}",
                    "message": str(e),
                    # "details": traceback.format_exc() # Maybe too verbose
                }
            )
        return tool_result_message


    async def handle_input(
        self,
        session_id: str,
        text: str,
        llm_config: Optional[LLMConfig] = None,
        system_prompt: Optional[str] = None
    ) -> AsyncGenerator[LLMResponsePart, None]:
        """
        Handles user input, generates responses, and manages tool calls.

        Args:
            session_id: The unique identifier for the conversation session.
            text: The user's input text.
            llm_config: Optional configuration for the LLM call.
            system_prompt: Optional user-specific system prompt to override the default.

        Yields:
            LLMResponsePart objects representing the conversation turn, including
            TextChunk, ToolCallIntent, ErrorInfo, EndOfTurn, ToolResultData,
            and RePromptContext.
        """
        # Use logger.info for entry point
        logger.info(f"Orchestrator ({session_id}): Starting handle_input for text: '{text[:100]}...'")
        current_llm_config = llm_config if llm_config is not None else LLMConfig({})
        session_history = self._get_history(session_id)

        # 1. Add user message to history
        user_message = ChatMessage(role='user', content=text, data=None, tool_name=None)
        self._add_message(session_id, user_message) # This will log via logger.debug if enabled

        # handled_successfully = False # Not currently used

        # --- Start LLM Interaction Loop ---
        while True:
            tool_definitions = self._get_tool_definitions()
            history_for_llm = list(session_history)

            assistant_text_buffer = ""
            last_response_part_was_tool_call = False

            try:
                logger.info(f"Orchestrator ({session_id}): Calling LLM service...") # Use logger.info
                response_stream = self.llm_service.generate_response(
                    history=history_for_llm,
                    tool_definitions=tool_definitions,
                    config=current_llm_config,
                    system_prompt=system_prompt
                )

                # 2. Process LLM response stream
                async for part in response_stream:
                    last_response_part_was_tool_call = False

                    if isinstance(part, TextChunk):
                        assistant_text_buffer += part.content
                        yield part # Yield immediately

                    elif isinstance(part, ToolCallIntent):
                        logger.info(f"Orchestrator ({session_id}): Received tool intent: {part.tool_name}") # Use logger.info
                        last_response_part_was_tool_call = True

                        if assistant_text_buffer:
                             assistant_message = ChatMessage(
                                  role='assistant', content=assistant_text_buffer, data=None, tool_name=None
                             )
                             self._add_message(session_id, assistant_message) # Logs via DEBUG
                             assistant_text_buffer = ""

                        yield part # Yield intent to caller

                        tool_result_message = await self._execute_tool_call(session_id, part) # Logs internally

                        # --- Yield the raw tool result ---
                        yield ToolResultData(
                            tool_name=tool_result_message['tool_name'],
                            result=tool_result_message['data']
                        )
                        # --- End yield tool result ---

                        self._add_message(session_id, tool_result_message) # Logs result via DEBUG

                        # --- Yield the re-prompt context info ---
                        yield RePromptContext(message=tool_result_message) # Updated type
                        # --- End yield re-prompt context ---

                        break # Re-prompt LLM

                    elif isinstance(part, ErrorInfo):
                        logger.error(f"Orchestrator ({session_id}): Received error from LLM stream: {part.message}") # Use logger.error
                        logger.debug(f"Orchestrator ({session_id}): LLM Error details: {part.details}") # Log details at debug
                        yield part

                    elif isinstance(part, EndOfTurn):
                        pass # Ignore in orchestrator

                    # --- Check for new types (shouldn't happen here) ---
                    elif isinstance(part, (ToolResultData, RePromptContext)): # Updated check
                        logger.warning(f"Orchestrator ({session_id}): Unexpectedly received {type(part)} from LLM stream.")
                    # --- End check ---

                    else:
                         unknown_part_msg = f"Orchestrator ({session_id}): Received unknown part type from LLM stream: {type(part)}"
                         logger.warning(unknown_part_msg) # Use logger.warning
                         yield ErrorInfo(message=unknown_part_msg)

                # --- LLM Turn Finished ---
                if not last_response_part_was_tool_call:
                    # Add final assistant message if any text was buffered and no tool call occurred
                    if assistant_text_buffer:
                        final_assistant_message = ChatMessage(
                            role='assistant', content=assistant_text_buffer, data=None, tool_name=None
                        )
                        self._add_message(session_id, final_assistant_message) # Logs via DEBUG
                    # If the loop finished without yielding a tool call, we are done with this user input
                    logger.info(f"Orchestrator ({session_id}): Finished processing user input.") # Use logger.info
                    yield EndOfTurn() # Explicitly yield EndOfTurn when the loop finishes naturally
                    break # Exit the while True loop

            except Exception as e:
                logger.error(f"Orchestrator ({session_id}): Unhandled error in LLM interaction loop: {e}", exc_info=True) # Use logger.error
                yield ErrorInfo(message=f"Internal orchestrator error: {e}", details=traceback.format_exc())
                break # Exit loop on unhandled error
