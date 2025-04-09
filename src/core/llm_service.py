# src/core/llm_service.py
# --- Layer: LLM Service ---
# Purpose: Provides a standard interface for interacting with LLMs,
#          handles prompt compilation, and parses responses into
#          text chunks or tool call intents.
# Changes:
# - Initial implementation: Define data classes, LLMAdapter protocol,
#   and basic LLMService structure.
# - Define concrete types for ChatMessage, ToolDefinition, LLMConfig.
# - Removed dependency on server_tool_prompts for prompt compilation.
# - Reverted _parse_stream to the original (always buffer) logic for simplicity.
# - Refined LLMConfig: Removed api_key, made model/safety optional overrides.
# - Modified generate_response and _compile_system_prompt to handle user-specific system prompts.
# - Corrected generate_response to pass prompt/history as a dict to the adapter.
# - Added ToolResultData and RePromptContext data classes to LLMResponsePart.
# - Fixed NameError for ToolDefinition type hint by using string literal ('ToolDefinition').
# - Reverted LLMResponsePart Union definition to use direct types instead of string literals.
# - Added detailed debug logging to _parse_stream.

import asyncio
import json
import logging
import traceback
from typing import (
    Any,
    AsyncGenerator,
    Dict,
    List,
    Optional,
    Protocol,
    Union,
    TypedDict,
    runtime_checkable,
)
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# --- Data Classes for Structured Responses ---

@dataclass
class TextChunk:
    """Represents a plain text part of the LLM response."""
    content: str

@dataclass
class ToolCallIntent:
    """Represents the LLM's intent to call a specific tool."""
    tool_name: str # Should be the qualified name (e.g., "server_id:tool_name")
    arguments: Dict[str, Any]

@dataclass
class ErrorInfo:
    """Represents an error encountered during LLM interaction or parsing."""
    message: str
    code: Optional[int] = None
    details: Optional[Any] = None

@dataclass
class EndOfTurn:
    """Signals the successful completion of the LLM's response turn."""
    pass # No content needed, the type itself is the signal

# --- Core Data Structures ---

class ChatMessage(TypedDict):
    """
    Represents a single message in the conversation history.
    Based on host_mvp_implementation_v3.md definition.
    """
    role: Union['user', 'assistant', 'system', 'tool']
    content: Optional[str] # Text content (user, assistant, system)
    data: Optional[Any] # Structured data (tool results)
    tool_name: Optional[str] # Name of tool if role is "tool"

class ToolParameterProperty(TypedDict, total=False):
    """Represents properties within JSON schema parameters (simplified)."""
    type: str
    description: str

class ToolParameters(TypedDict, total=False):
    """Represents the JSON schema for tool parameters (simplified)."""
    type: str # Typically "object"
    properties: Dict[str, ToolParameterProperty]
    required: List[str]

class ToolDefinition(TypedDict):
    """
    Represents the information about an available tool needed by the LLMService.
    This combines info from mcp.types.Tool and MCPCoordinator's registry entry.
    """
    qualified_name: str # e.g., "server_id:tool_name"
    server_id: str
    description: str
    parameters: ToolParameters # JSON Schema for arguments

# --- LLM Configuration (Per Request) ---

class LLMConfig(TypedDict, total=False):
    """Configuration parameters for a *single* LLM API call."""
    temperature: Optional[float]
    max_output_tokens: Optional[int]
    model_name: Optional[str]
    # safety_settings: Optional[Dict[str, str]] # NOTE: Cannot be passed to stream method currently
    # system_instruction: Optional[str] # NOTE: Cannot be passed to stream method currently

# --- Adapter Protocol ---

@runtime_checkable
class LLMAdapter(Protocol):
    """Protocol defining the interface for specific LLM adapters."""

    async def stream_generate(
        self,
        prompt_and_history: Any, # Adapter-specific input format
        config: LLMConfig       # Per-request config (no API key)
    ) -> AsyncGenerator[str, None]:
        """Streams raw text chunks from the underlying LLM."""
        ... # pragma: no cover

# --- New Data Classes for Enhanced Frontend Info ---

@dataclass
class ToolResultData:
    """Represents the actual result obtained from executing a tool."""
    tool_name: str
    result: Any # The raw data returned by the tool

@dataclass
class RePromptContext: # Renamed from InternalMonologue
    """Represents the context (tool result message) added before re-prompting the LLM."""
    # Using ChatMessage structure is convenient for representing the re-prompt part
    message: ChatMessage

# --- Union type for the parts yielded by the service ---
# Define this *after* all constituent types are defined.
# Use direct types here as they are defined above in the file.
LLMResponsePart = Union[
    TextChunk, ToolCallIntent, ErrorInfo, EndOfTurn, ToolResultData, RePromptContext
]

# --- LLM Service ---

class LLMService:
    """
    Orchestrates interaction with an LLM via a specific adapter.

    Handles prompt compilation and parses the adapter's raw output stream.
    """

    def __init__(self, adapter: LLMAdapter, base_system_prompt: str):
        """Initializes the LLMService."""
        if not isinstance(adapter, LLMAdapter):
            raise TypeError("Adapter must implement the LLMAdapter protocol.")
        self._adapter = adapter
        self._base_system_prompt = base_system_prompt
        self._tool_start_delimiter = "```tool\n"
        self._tool_end_delimiter = "\n```"
        logger.info("LLMService initialized.")

    def _clean_mcp_schema_for_gemini(self, mcp_schema: Dict[str, Any]) -> Dict[str, Any]:
        """
        Converts an MCP inputSchema dictionary into a format closer to
        Gemini FunctionDeclaration parameters, removing problematic keys.
        Based on: https://ai.google.dev/gemini-api/docs/function-calling?example=weather#use_model_context_protocol_mcp
        """
        if not isinstance(mcp_schema, dict):
            return {} # Return empty if schema is not a dict
        gemini_params = json.loads(json.dumps(mcp_schema)) # Deep copy
        gemini_params.pop('additionalProperties', None)
        gemini_params.pop('$schema', None)
        if 'properties' in gemini_params and isinstance(gemini_params['properties'], dict):
            for prop_key, prop_value in gemini_params['properties'].items():
                if isinstance(prop_value, dict):
                     prop_value.pop('additionalProperties', None)
                     prop_value.pop('$schema', None)
        return gemini_params

    def _compile_system_prompt(
        self,
        base_prompt_content: str, # Added: The base content to use
        tool_definitions: List['ToolDefinition'] # Use string literal here for forward reference
    ) -> str:
        """
        Compiles the final system prompt for the LLM.

        Includes base instructions, detailed tool descriptions formatted similarly
        to Gemini FunctionDeclarations, and the host rules for invoking tools
        via JSON blobs within custom delimiters. Attempts to encourage conversational
        text around tool calls.

        Args:
            base_prompt_content: The base system instructions (potentially user-specific).
            tool_definitions: A list of available tools with their definitions.

        Returns:
            The complete system prompt string.
        """
        logger.debug("Compiling system prompt...")
        prompt_lines = [base_prompt_content]
        prompt_lines.append("\n--- Tool Usage Instructions ---")

        if not tool_definitions:
            prompt_lines.append("No tools are available for you to use.")
        else:
            prompt_lines.append(
                "When you decide to use a tool to answer a user's request:"
                "\n1. First, briefly tell the user what action you are taking (e.g., 'Okay, searching memory for related notes...')."
                "\n2. Then, on a **new line**, provide the required tool call JSON object, enclosed *exactly* like this, with **no other text on the same line or within the delimiters**:"
                f"\n{self._tool_start_delimiter}"
                '{ "tool": "server_id:tool_name", "arguments": { /* ...args... */ } }'
                f"\n{self._tool_end_delimiter}"
                "\nAfter you receive the result from the tool, summarize it for the user."
                "\n\n--- Available Tools ---"
                "\nHere are the tools available to you (described in a format similar to function declarations):"
            )

            for tool_def in tool_definitions:
                 prompt_lines.append(f"\nTool Name: {tool_def['qualified_name']}")
                 prompt_lines.append(f"  Description: {tool_def.get('description', 'No description')}")
                 parameters_schema = tool_def.get('parameters', {})
                 cleaned_parameters = self._clean_mcp_schema_for_gemini(parameters_schema)
                 if cleaned_parameters:
                      try:
                           params_str = json.dumps(cleaned_parameters, indent=4)
                           prompt_lines.append(f"  Parameters Schema (JSON):\n{params_str}")
                      except Exception as e:
                            logger.warning(f"Failed to dump parameters schema to JSON for tool {tool_def['qualified_name']}: {e}", exc_info=True)
                            prompt_lines.append(f"  Parameters Schema: {cleaned_parameters}")
                 else:
                      prompt_lines.append("  Parameters Schema: None")

        prompt_lines.append("\n--- Conversation ---")
        compiled_prompt = "\n".join(prompt_lines)
        logger.debug(f"Compiled system prompt length: {len(compiled_prompt)} chars.")
        return compiled_prompt

    async def _parse_stream(
        self,
        raw_adapter_stream: AsyncGenerator[str, None]
    ) -> AsyncGenerator[LLMResponsePart, None]: # Type hint remains Union
        """
        Parses the raw text stream from the adapter (Original Buffering Logic).

        Identifies text chunks and tool call JSON blobs based on delimiters,
        handling buffering and potential parsing errors.

        Args:
            raw_adapter_stream: The async generator yielding raw text chunks from the adapter.

        Yields:
            Structured LLMResponsePart objects (TextChunk, ToolCallIntent, ErrorInfo).
        """
        buffer = ""
        logger.debug("Starting to parse adapter stream...")
        try:
            chunk_count = 0
            async for chunk in raw_adapter_stream:
                chunk_count += 1
                # Represent potential hidden chars for logging
                log_chunk = repr(chunk)
                logger.debug(f"Parser received chunk #{chunk_count}: {log_chunk}")
                buffer += chunk
                logger.debug(f"  Buffer is now ({len(buffer)} chars): {repr(buffer)}")

                while True: # Process buffer repeatedly
                    logger.debug("  Processing buffer loop...")
                    start_index = buffer.find(self._tool_start_delimiter)
                    logger.debug(f"  Find start '{repr(self._tool_start_delimiter)}': index={start_index}")

                    if start_index == -1:
                        # No start delimiter found in current buffer
                        logger.debug("  No start delimiter found. Breaking inner loop (will yield buffer at end of stream).")
                        break # Exit inner loop, wait for more chunks or end of stream

                    # Found a tool start delimiter
                    logger.debug(f"  Found start delimiter at index {start_index}.")
                    if start_index > 0:
                        # Yield text before the delimiter
                        text_to_yield = buffer[:start_index]
                        logger.debug(f"  Yielding TextChunk: {repr(text_to_yield)}")
                        yield TextChunk(content=text_to_yield)
                        buffer = buffer[start_index:]
                        logger.debug(f"  Trimmed buffer to: {repr(buffer)}")
                        # After yielding text, reset start_index relative to new buffer (should be 0)
                        start_index = 0 # Explicitly set for clarity

                    # Now, look for the end delimiter *after* the start delimiter
                    end_index = buffer.find(self._tool_end_delimiter, len(self._tool_start_delimiter))
                    logger.debug(f"  Find end '{repr(self._tool_end_delimiter)}' (after start): index={end_index}")

                    if end_index == -1:
                        # Found start but not end, need more chunks
                        logger.debug("  Found start but not end. Breaking inner loop to wait for more chunks.")
                        break # Exit inner loop, wait for more chunks

                    # Found both start and end delimiters
                    logger.debug(f"  Found end delimiter at index {end_index}.")
                    json_content_start = start_index + len(self._tool_start_delimiter)
                    json_content = buffer[json_content_start:end_index].strip()
                    logger.debug(f"  Extracted JSON content: {repr(json_content)}")

                    try:
                        tool_data = json.loads(json_content)
                        if isinstance(tool_data, dict) and "tool" in tool_data and "arguments" in tool_data:
                            logger.debug(f"  Successfully parsed tool data. Yielding ToolCallIntent: {tool_data}")
                            yield ToolCallIntent(
                                tool_name=str(tool_data["tool"]), # Ensure name is string
                                arguments=tool_data["arguments"] # Arguments can be any JSON structure
                            )
                        else:
                             logger.warning(f"Parsed JSON blob does not match expected tool call format: {json_content}")
                             logger.debug("  Yielding ErrorInfo for invalid format.")
                             yield ErrorInfo(message="Invalid tool call format received from LLM.")
                    except json.JSONDecodeError as e:
                        logger.error(f"Failed to parse JSON tool call blob: {e}\nContent: {repr(json_content)}", exc_info=True)
                        logger.debug(f"  Yielding ErrorInfo for JSON decode error: {e}")
                        yield ErrorInfo(message=f"Failed to parse tool call JSON: {e}", details=json_content)
                    except Exception as e: # Catch other potential errors during processing
                        logger.error(f"Unexpected error processing tool call data: {e}\nContent: {repr(json_content)}", exc_info=True)
                        logger.debug(f"  Yielding ErrorInfo for unexpected processing error: {e}")
                        yield ErrorInfo(message=f"Unexpected error processing tool call: {e}", details=json_content)

                    # Move buffer past the processed tool call section
                    buffer = buffer[end_index + len(self._tool_end_delimiter):]
                    logger.debug(f"  Trimmed buffer after tool processing: {repr(buffer)}")
                    # Continue processing the rest of the buffer in the inner loop
                    logger.debug("  Continuing inner loop...")

            # After the stream ends, yield any remaining text in the buffer
            if buffer:
                logger.debug(f"Stream ended. Yielding final TextChunk from buffer: {repr(buffer)}")
                yield TextChunk(content=buffer)

        except Exception as e:
            logger.error(f"Error during raw adapter stream processing: {e}", exc_info=True)
            yield ErrorInfo(message=f"Stream parsing error: {e}", details=traceback.format_exc())
        finally:
            logger.debug("Finished parsing adapter stream.")

    async def generate_response(
        self,
        history: List[ChatMessage], # Use direct type here
        tool_definitions: List['ToolDefinition'], # Use string literal here
        config: LLMConfig, # This is the config passed by the caller (e.g., Orchestrator)
        system_prompt: Optional[str] = None # Added: User-specific override
    ) -> AsyncGenerator[LLMResponsePart, None]: # Type hint remains Union
        """
        Generates a response from the LLM based on history and available tools.

        Args:
            history: The conversation history (excluding system prompt).
            tool_definitions: List of tools available for the LLM.
            config: Configuration for the LLM call.
            system_prompt: Optional user-specific system prompt to use instead of the default.

        Yields:
            LLMResponsePart objects representing text chunks, tool intents, or errors.
        """
        logger.info("LLM Service: Generating response...")
        try:
            # Determine the system prompt content to use
            prompt_content_to_use = system_prompt if system_prompt is not None else self._base_system_prompt
            # Compile the full system prompt including tool descriptions
            final_system_prompt = self._compile_system_prompt(prompt_content_to_use, tool_definitions)

            # Prepare input for the adapter (this structure might be adapter-specific)
            # Assuming a structure like {'system': str, 'history': List[ChatMessage]}
            # based on Gemini adapter needs. Adjust if other adapters expect different formats.
            adapter_input = {
                "system": final_system_prompt,
                "history": history
            }

            logger.debug(f"LLM Service: Calling adapter stream_generate with config: {config}")
            # logger.debug(f"LLM Service: Adapter input structure:\nSystem: {final_system_prompt[:200]}...\nHistory items: {len(history)}")

            raw_stream = self._adapter.stream_generate(adapter_input, config)
            async for part in self._parse_stream(raw_stream):
                yield part

            # Signal the end of the turn after successfully processing the stream
            yield EndOfTurn()
            logger.info("LLM Service: Finished generating response stream.")

        except Exception as e:
            logger.error(f"LLM Service: Error during response generation: {e}", exc_info=True)
            yield ErrorInfo(message=f"LLM service error: {e}", details=traceback.format_exc())
