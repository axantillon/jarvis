# src/core/orchestrator.py
# --- Layer: Conversation Orchestrator ---
# Purpose: Manages conversation flow, state, LLM interaction, and tool execution.
# Changes:
# - Initial implementation.
# - Implemented handle_input logic including LLM calls and tool execution flow.
# - Modified handle_input to accept and pass a user-specific system_prompt.
# - Added logging for handle_input start/end.

import asyncio
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
    ToolDefinition, # We'll need to construct this
    LLMConfig,
    EndOfTurn
)
from .mcp_coordinator import MCPCoordinator, ToolRegistryEntry

# Configure logging
# Setting level to INFO to capture the new logs
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

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

    def _get_history(self, session_id: str) -> List[ChatMessage]:
        """Retrieves or initializes history for a session."""
        if session_id not in self._histories:
            self._histories[session_id] = []
        return self._histories[session_id]

    def _add_message(self, session_id: str, message: ChatMessage):
        """Adds a message to the history for a session, enforcing max length."""
        history = self._get_history(session_id)
        history.append(message)
        # Simple truncation from the beginning (keeping system prompts might be better)
        while len(history) > self._max_history_len:
            # Careful not to remove a potential leading system prompt if we add one later
            # For now, just remove the oldest
            history.pop(0)

    def _get_tool_definitions(self) -> List[ToolDefinition]:
        """
        Extracts tool definitions from the MCPCoordinator's registry
        in the format expected by LLMService.
        """
        definitions: List[ToolDefinition] = []
        if not self.mcp_coordinator or not self.mcp_coordinator.tool_registry:
             return definitions

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
            print(f"Orchestrator: Executing tool '{tool_intent.tool_name}' with args: {tool_intent.arguments}") # Use original name
            # Ensure mcp_coordinator is ready (it should be if initialized via async with)
            if not self.mcp_coordinator:
                 raise RuntimeError("MCP Coordinator not available.")

            result_data = await self.mcp_coordinator.call_tool(
                qualified_tool_name=tool_intent.tool_name, # Use original name
                arguments=tool_intent.arguments
            )
            print(f"Orchestrator: Tool '{tool_intent.tool_name}' executed successfully.") # Use original name
            tool_result_message = ChatMessage(
                role='tool',
                tool_name=tool_intent.tool_name, # Use original name in history
                content=None, # Tool results go in 'data'
                data=result_data # Assuming call_tool returns the JSON-able result
            )
        except Exception as e:
            error_details = traceback.format_exc()
            print(f"Orchestrator: Error executing tool '{tool_intent.tool_name}': {e}\n{error_details}") # Use original name
            tool_result_message = ChatMessage(
                role='tool',
                tool_name=tool_intent.tool_name, # Use original name in history
                content=None, # Error details go in 'data'
                data={ # Structure the error data
                    "error": f"Tool execution failed: {type(e).__name__}",
                    "message": str(e),
                    # "details": error_details # Maybe too verbose for LLM history
                }
            )
        return tool_result_message


    async def handle_input(
        self,
        session_id: str,
        text: str,
        llm_config: Optional[LLMConfig] = None, # LLMConfig is defined in llm_service.py
        system_prompt: Optional[str] = None      # Added: User-specific system prompt
    ) -> AsyncGenerator[LLMResponsePart, None]:
        """
        Handles user input, generates responses, and manages tool calls.

        Args:
            session_id: The unique identifier for the conversation session.
            text: The user's input text.
            llm_config: Optional configuration for the LLM call.
            system_prompt: Optional user-specific system prompt to override the default.

        Yields:
            LLMResponsePart objects representing the conversation turn.
        """
        logger.info(f"Orchestrator ({session_id}): Starting handle_input for text: '{text[:100]}...'")
        current_llm_config = llm_config if llm_config is not None else LLMConfig({})
        session_history = self._get_history(session_id)

        # 1. Add user message to history
        user_message = ChatMessage(role='user', content=text, data=None, tool_name=None)
        self._add_message(session_id, user_message)

        handled_successfully = False  # Flag to track if we completed successfully

        # --- Start LLM Interaction Loop ---
        # This loop allows re-prompting after a tool call
        while True:
            tool_definitions = self._get_tool_definitions()
            history_for_llm = list(session_history) # Create a copy for this turn

            # Use a buffer to collect assistant's text before a potential tool call
            assistant_text_buffer = ""
            last_response_part_was_tool_call = False

            try:
                print(f"Orchestrator ({session_id}): Calling LLM service...")
                response_stream = self.llm_service.generate_response(
                    history=history_for_llm,
                    tool_definitions=tool_definitions,
                    config=current_llm_config,
                    system_prompt=system_prompt # Pass the user-specific prompt
                )

                # 2. Process LLM response stream
                async for part in response_stream:
                    last_response_part_was_tool_call = False # Reset on each new part

                    if isinstance(part, TextChunk):
                        # Accumulate text and yield it immediately
                        assistant_text_buffer += part.content
                        yield part

                    elif isinstance(part, ToolCallIntent):
                        print(f"Orchestrator ({session_id}): Received tool intent: {part.tool_name}")
                        last_response_part_was_tool_call = True

                        # A. Add preceding text (if any) as assistant message
                        if assistant_text_buffer:
                             assistant_message = ChatMessage(
                                  role='assistant',
                                  content=assistant_text_buffer,
                                  data=None,
                                  tool_name=None
                             )
                             self._add_message(session_id, assistant_message)
                             assistant_text_buffer = "" # Reset buffer

                        # B. Yield the intent (signals caller e.g., WebSocket handler)
                        yield part # Inform caller about the tool call attempt

                        # C. Execute the tool
                        tool_result_message = await self._execute_tool_call(session_id, part)

                        # D. Add tool result to history (crucial for next LLM turn)
                        self._add_message(session_id, tool_result_message)

                        # E. Break inner loop to re-prompt LLM with tool result
                        break # Exit the inner async for loop

                    elif isinstance(part, ErrorInfo):
                        # Yield error info immediately
                        print(f"Orchestrator ({session_id}): Received error from LLM stream: {part.message}")
                        yield part
                        # Optionally add a system message to history about the error?
                        # self._add_message(session_id, ChatMessage(role='system', content=f"LLM Error: {part.message}", data=part.details))
                        # Decide whether to break or continue based on error severity? For now, continue if possible.

                    # --- ADDED: Handle EndOfTurn --- #
                    elif isinstance(part, EndOfTurn):
                        # This signal is used by the LLMService to indicate completion.
                        # The Orchestrator itself doesn't need to do anything with it here,
                        # as the WebSocketHandler will handle formatting it for the client.
                        # We just need to prevent it falling into the 'else' block below.
                        pass # Explicitly ignore it in the orchestrator loop

                    else:
                         # Should not happen if LLMResponsePart is defined correctly
                         unknown_part_msg = f"Orchestrator ({session_id}): Received unknown part type from LLM stream: {type(part)}"
                         print(unknown_part_msg)
                         yield ErrorInfo(message=unknown_part_msg)

                # --- After processing the stream ---

                # If the loop finished *because* of a tool call, restart the outer loop
                if last_response_part_was_tool_call:
                    logger.info(f"Orchestrator ({session_id}): Re-prompting LLM after tool call {part.tool_name if isinstance(part, ToolCallIntent) else 'unknown'}.")
                    continue # Go back to the start of the 'while True' loop

                # If the loop finished *without* breaking for a tool call, it means the LLM turn is complete
                # Add the final assistant text to history
                if assistant_text_buffer:
                     assistant_message = ChatMessage(
                          role='assistant',
                          content=assistant_text_buffer,
                          data=None,
                          tool_name=None
                     )
                     self._add_message(session_id, assistant_message)
                     assistant_text_buffer = "" # Clear buffer just in case

                handled_successfully = True # Mark as successful if we reached the end naturally
                print(f"Orchestrator ({session_id}): LLM turn finished successfully.")
                break # Exit the 'while True' loop normally

            except Exception as e:
                error_trace = traceback.format_exc()
                logger.error(f"Orchestrator ({session_id}): Unhandled error in handle_input: {e}\n{error_trace}")
                yield ErrorInfo(message=f"Orchestrator error: {e}", details=error_trace)
                # Ensure we still break the loop on unhandled errors
                break
            finally:
                # Yield an EndOfTurn signal regardless of how the loop ended (success, error, tool call exit)
                # The WebSocket handler uses this to know the processing for this input is done.
                logger.info(f"Orchestrator ({session_id}): Turn completed, yielding EndOfTurn signal. handled_successfully={handled_successfully}")
                yield EndOfTurn()
