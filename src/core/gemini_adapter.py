# src/core/gemini_adapter.py
# --- Layer: LLM Adapter ---
# Purpose: Implements the LLMAdapter protocol for Google Gemini models.
#          Handles API communication using the NEW google-genai SDK.
# Changes:
# - FINAL FIX 4: Map internal 'tool' role to 'user' role for API history,
#   as 'function' role is invalid in this context.
# - Using correct import for the new google-genai SDK.
# - Refactored logging: Added logger, replaced print with logger calls.

from google import genai
from google.genai import types as genai_types
from google.api_core import exceptions as google_exceptions
import traceback
import json
import asyncio
import logging # <-- Add logging import
from typing import (
    Any,
    AsyncGenerator,
    Dict,
    List,
    Optional,
    cast,
    Generator
)
from functools import partial

# Import necessary types and protocol from llm_service
# Use relative import if they are in the same package/directory structure
from .llm_service import LLMAdapter, LLMConfig, ChatMessage

# Default model (using user preference)
DEFAULT_MODEL_NAME = "gemini-2.0-flash-thinking-exp-01-21"

# Note: Safety settings cannot be passed to stream method currently
DEFAULT_SAFETY_SETTINGS_DICT = {
    'HARASSMENT': 'BLOCK_MEDIUM_AND_ABOVE',
    'HATE_SPEECH': 'BLOCK_MEDIUM_AND_ABOVE',
    'SEXUALLY_EXPLICIT': 'BLOCK_MEDIUM_AND_ABOVE',
    'DANGEROUS_CONTENT': 'BLOCK_MEDIUM_AND_ABOVE',
}

# Sentinel object to signal the end of the sync generator
_SENTINEL = object()
logger = logging.getLogger(__name__) # <-- Add logger definition

class GeminiAdapter(LLMAdapter):
    """
    LLMAdapter implementation for Google Gemini, using the google-genai SDK.
    NOTE: Due to apparent limitations/signature of generate_content_stream,
    per-call configuration for temp, tokens, safety, system_instruction
    is currently NOT applied for streaming calls. Relies on defaults.
    System prompt is prepended to the 'contents' list.
    Tool results are mapped to the 'user' role for history context.
    """

    def __init__(
        self,
        api_key: str,
        default_model_name: str = DEFAULT_MODEL_NAME,
        default_safety_settings: Optional[Dict[str, str]] = None,
    ):
        """Initializes the Gemini Adapter using google.genai.Client."""
        self._default_model_name = default_model_name
        self._default_safety_settings_dict = default_safety_settings if default_safety_settings is not None else DEFAULT_SAFETY_SETTINGS_DICT

        try:
            # Mask API Key for logging if needed (simple example)
            api_key_display = f"{api_key[:4]}...{api_key[-4:]}" if api_key and len(api_key) > 8 else "******"
            logger.info(f"Initializing google-genai Client (API Key: {api_key_display}, Model: {self._default_model_name})...") # Use logger.info
            self._client = genai.Client(api_key=api_key)
            logger.info("google-genai Client initialized successfully.") # Use logger.info
        except Exception as e:
             logger.critical(f"Error initializing google-genai Client: {e}", exc_info=True) # Use logger.critical
             raise RuntimeError(f"Failed to initialize google-genai Client: {e}") from e

    def _format_contents_for_gemini(
        self,
        system_prompt: Optional[str],
        history: List[ChatMessage]
        ) -> List[genai_types.ContentDict]:
        """
        Converts system prompt and history into the google-genai ContentDict list format.
        Prepends system prompt as first user message with synthetic model response.
        Maps internal 'tool' role messages to 'user' role for API compatibility.
        """
        contents: List[genai_types.ContentDict] = []
        logger.debug("Formatting history for Gemini API...") # Use logger.debug

        if system_prompt and system_prompt.strip():
            contents.append(genai_types.ContentDict(role="user", parts=[genai_types.PartDict(text=system_prompt)]))
            contents.append(genai_types.ContentDict(role="model", parts=[genai_types.PartDict(text="Understood. I will follow these instructions.")]))

        last_role = "model" if contents else None

        for i, message in enumerate(history):
            role = message['role']
            content = message.get('content')
            data = message.get('data')
            tool_name = message.get('tool_name') # Get tool name for context

            mapped_role: Optional[str] = None
            parts: List[genai_types.PartDict] = []

            if role == 'assistant':
                 mapped_role = 'model'
                 if content is not None and isinstance(content, str):
                      parts.append(genai_types.PartDict(text=content))
            elif role == 'user':
                 mapped_role = 'user'
                 if content is not None and isinstance(content, str):
                      parts.append(genai_types.PartDict(text=content))
            elif role == 'tool':
                 mapped_role = 'user'
                 if data is not None:
                      if isinstance(data, str): part_content = data
                      else:
                           try: part_content = json.dumps(data, indent=2)
                           except Exception: part_content = str(data)
                      # Add context that this is a tool result
                      tool_context = f"Result for tool '{tool_name}':\n" if tool_name else "Tool Result:\n"
                      parts.append(genai_types.PartDict(text=f"{tool_context}{part_content}"))
                 else:
                     logger.warning(f"History item {i} has role 'tool' but no data.") # Use logger.warning
            else:
                logger.warning(f"Skipping history item {i} with unhandled role: {role}") # Use logger.warning
                continue

            if not parts or not mapped_role:
                 logger.debug(f"Skipping history item {i}: No parts generated or role not mapped (Role: {role}).") # Use logger.debug
                 continue

            if mapped_role == last_role:
                 logger.warning(f"Appending consecutive '{mapped_role}' role messages at history index {i}.") # Use logger.warning
                 # Append anyway for now, Gemini might handle it.
                 contents.append(genai_types.ContentDict(role=mapped_role, parts=parts))
                 last_role = mapped_role
            else:
                 contents.append(genai_types.ContentDict(role=mapped_role, parts=parts))
                 last_role = mapped_role

        logger.debug(f"Formatted {len(contents)} content items for Gemini API.") # Use logger.debug
        return contents

    # Helper function to safely get next item or sentinel
    def _safe_next(self, sync_generator: Generator) -> Any:
        """Calls next() on the generator, returning _SENTINEL on StopIteration."""
        try:
            return next(sync_generator)
        except StopIteration:
            return _SENTINEL
        except Exception as e:
             logger.error(f"Error during sync generator next() call: {e}", exc_info=True) # Use logger.error
             raise

    async def _iterate_sync_generator_async(self, sync_generator: Generator) -> AsyncGenerator[Any, None]:
        """Wraps a synchronous generator in an async one using run_in_executor."""
        loop = asyncio.get_running_loop()
        func = partial(self._safe_next, sync_generator)
        while True:
            try:
                next_item = await loop.run_in_executor(None, func)
                if next_item is _SENTINEL: break
                else: yield next_item
            except Exception as e:
                logger.error(f"Error iterating sync generator in executor: {e}", exc_info=True) # Use logger.error
                raise

    async def stream_generate(
        self,
        prompt_and_history: Dict[str, Any],
        config: LLMConfig
    ) -> AsyncGenerator[str, None]:
        """
        Streams raw text chunks from the Gemini API using the new google-genai SDK.
        NOTE: Calls generate_content_stream with ONLY model and contents arguments.
        """
        if not self._client:
             logger.critical("generate_content_stream called but google-genai Client not initialized.") # Use logger.critical
             raise RuntimeError("google-genai Client not initialized.")

        try:
            system_prompt_text = prompt_and_history.get("system_prompt")
            history = prompt_and_history.get("history", [])
            formatted_contents = self._format_contents_for_gemini(system_prompt_text, history)

            if not formatted_contents:
                 logger.warning("Formatted contents list is empty after processing history/prompt. Skipping API call.") # Use logger.warning
                 return

            model_name_to_use = config.get('model_name', self._default_model_name)
            if not model_name_to_use.startswith("models/"):
                 model_name_for_api = f"models/{model_name_to_use}"
            else:
                 model_name_for_api = model_name_to_use

            request_args: Dict[str, Any] = {
                "model": model_name_for_api,
                "contents": formatted_contents,
            }

            logger.info(f"Calling generate_content_stream with model: {model_name_for_api}") # Use logger.info
            # Log contents at DEBUG level, potentially large
            if logger.isEnabledFor(logging.DEBUG):
                 try:
                     contents_json = json.dumps(formatted_contents, indent=2)
                     logger.debug(f"Contents being sent to Gemini API:\n{contents_json}")
                 except Exception:
                     logger.debug(f"Contents being sent (could not JSON dump):\n{formatted_contents}")

            sync_response_iterator = self._client.models.generate_content_stream(**request_args)

            async for chunk in self._iterate_sync_generator_async(sync_response_iterator):
                 logger.debug(f"Adapter received raw chunk: {chunk}") # Use logger.debug (verbose)
                 try:
                      if hasattr(chunk, 'text') and chunk.text:
                           yield chunk.text
                      elif chunk.parts:
                           chunk_text = "".join(part.text for part in chunk.parts if hasattr(part, 'text'))
                           if chunk_text:
                                yield chunk_text
                 except ValueError as e: # Often indicates safety block
                       logger.warning(f"Skipping chunk due to ValueError (likely blocked content): {e}") # Use logger.warning
                       continue
                 except AttributeError as e:
                      logger.warning(f"Skipping chunk due to AttributeError (unexpected structure): {chunk}. Error: {e}") # Use logger.warning
                      continue

        except google_exceptions.GoogleAPIError as e:
            logger.error(f"Gemini API Error ({type(e).__name__}): {e}", exc_info=True) # Use logger.error
            logger.debug(f"Request model: {request_args.get('model')}") # Use logger.debug
            logger.debug(f"Request contents roles: {[c.get('role') for c in request_args.get('contents', [])]}") # Use logger.debug
            if isinstance(e, google_exceptions.PermissionDenied):
                 logger.error("Error suggests API key or permissions issue.") # Use logger.error
            elif isinstance(e, google_exceptions.InvalidArgument):
                 logger.error(f"Error suggests invalid argument passed to Gemini API: {e}") # Use logger.error
            raise ConnectionError(f"Gemini API request failed: {e}") from e
        except Exception as e:
            logger.critical(f"Unexpected error in GeminiAdapter stream_generate: {e}", exc_info=True) # Use logger.critical
            raise RuntimeError(f"GeminiAdapter failed: {e}") from e
