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

# Union type for the parts yielded by the service
LLMResponsePart = Union[TextChunk, ToolCallIntent, ErrorInfo, EndOfTurn]

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
        tool_definitions: List[ToolDefinition]
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
                f"{self._tool_end_delimiter}"
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
    ) -> AsyncGenerator[LLMResponsePart, None]:
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
            async for chunk in raw_adapter_stream:
                logger.debug(f"Parser received chunk: '{chunk}'")
                buffer += chunk
                while True: # Process buffer repeatedly
                    start_index = buffer.find(self._tool_start_delimiter)
                    if start_index == -1: break
                    if start_index > 0:
                        yield TextChunk(content=buffer[:start_index])
                        buffer = buffer[start_index:]
                        start_index = 0
                    end_index = buffer.find(self._tool_end_delimiter, len(self._tool_start_delimiter))
                    if end_index == -1: break
                    json_content_start = start_index + len(self._tool_start_delimiter)
                    json_content = buffer[json_content_start:end_index].strip()
                    try:
                        tool_data = json.loads(json_content)
                        if isinstance(tool_data, dict) and "tool" in tool_data and "arguments" in tool_data:
                            logger.info(f"Parser yielding ToolCallIntent for: {tool_data.get('tool')}")
                            yield ToolCallIntent(tool_name=tool_data["tool"], arguments=tool_data["arguments"])
                        else: raise ValueError("Parsed JSON missing required 'tool' or 'arguments' keys.")
                    except Exception as e:
                         logger.error(f"Failed to parse/validate tool call JSON: '{json_content}'. Error: {e}", exc_info=True)
                         yield ErrorInfo(message=f"Failed to parse/validate tool call JSON.", details=f"Error: {e}. Content: '{json_content}'")
                    buffer = buffer[end_index + len(self._tool_end_delimiter):]
            if buffer: yield TextChunk(content=buffer)
            logger.debug("Finished parsing adapter stream.")
            yield EndOfTurn()
        except Exception as e:
            logger.error(f"Error receiving or processing data from LLM adapter stream: {e}", exc_info=True)
            yield ErrorInfo(message=f"Error receiving data from LLM adapter: {e}", details=traceback.format_exc())

    async def generate_response(
        self,
        history: List[ChatMessage],
        tool_definitions: List[ToolDefinition],
        config: LLMConfig, # This is the config passed by the caller (e.g., Orchestrator)
        system_prompt: Optional[str] = None # Added: User-specific override
    ) -> AsyncGenerator[LLMResponsePart, None]:
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
        try:
            # 1. Determine the effective base prompt
            effective_base_prompt = system_prompt if system_prompt is not None else self._base_system_prompt

            # 2. Compile the full system prompt including tool definitions
            compiled_system_prompt_str = self._compile_system_prompt(
                base_prompt_content=effective_base_prompt,
                tool_definitions=tool_definitions
            )

            # 3. Corrected: Prepare input as a dictionary for GeminiAdapter
            prompt_and_history_for_adapter = {
                "system_prompt": compiled_system_prompt_str,
                "history": history # Pass the original history; adapter handles formatting
            }

            # 4. Call the adapter to get the raw stream
            logger.debug(f"LLM Service: Calling adapter stream_generate... History length: {len(history)}")
            raw_stream = self._adapter.stream_generate(
                prompt_and_history=prompt_and_history_for_adapter, # Pass combined prompt+history
                config=config
            )

            # 5. Parse the raw stream and yield structured parts
            async for part in self._parse_stream(raw_stream):
                yield part

            # 6. Signal end of turn after stream finishes successfully
            yield EndOfTurn()

        except Exception as e:
            error_details = traceback.format_exc()
            logger.error(f"LLM Service: Error during generate_response: {e}\n{error_details}")
            yield ErrorInfo(message=f"Error generating response: {e}", details=error_details)
            yield EndOfTurn()
