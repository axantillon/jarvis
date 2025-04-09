# src/main.py
# --- Application Entry Point ---
# Purpose: Initializes all core components and starts the WebSocket server.
#          Relies on the web_gateway.py for user authentication.
# Changes:
# - Initial implementation.
# - Added AUTHORIZED_USERS dictionary for email/password auth, requires bcrypt.
# - Reverted user config to be hardcoded in this file due to env var parsing issues.
# - REMOVED bcrypt dependency and password hash loading/validation.
# - REMOVED direct user authentication logic (moved to web_gateway.py).
# - Kept AUTHORIZED_USERS structure for user-specific prompt additions.
# - Added centralized logging configuration via setup_logging() and LOGGING_MODE env var.

import asyncio
import logging
import os
import sys
import traceback
# import json # No longer needed
from pathlib import Path

# --- ANSI Color Codes for Logging Formatter ---
COLOR_DEBUG = "\033[90m"    # Grey
COLOR_INFO = "\033[94m"     # Blue
COLOR_WARNING = "\033[93m"  # Yellow
COLOR_ERROR = "\033[91m"    # Red
COLOR_CRITICAL = "\033[95m" # Magenta
COLOR_RESET = "\033[0m"
COLOR_NAME = "\033[96m"     # Cyan

# --- Custom Logging Formatter with Colors ---
class ColorFormatter(logging.Formatter):
    LOG_FORMAT = "%(asctime)s - {color_name}%(name)s{color_reset} - %(levelname)s - %(message)s"
    LEVEL_COLORS = {
        logging.DEBUG: COLOR_DEBUG,
        logging.INFO: COLOR_INFO,
        logging.WARNING: COLOR_WARNING,
        logging.ERROR: COLOR_ERROR,
        logging.CRITICAL: COLOR_CRITICAL,
    }

    def format(self, record):
        log_format = self.LOG_FORMAT.format(
            color_name=COLOR_NAME, color_reset=COLOR_RESET
        )
        formatter = logging.Formatter(log_format, datefmt='%Y-%m-%d %H:%M:%S')

        # Default format without level color
        formatted_message = formatter.format(record)

        # Find the position of the level name to add color
        level_name_start = formatted_message.find(record.levelname)
        if level_name_start != -1:
             level_name_end = level_name_start + len(record.levelname)
             level_color = self.LEVEL_COLORS.get(record.levelno, COLOR_RESET)
             # Reconstruct message with color around levelname
             formatted_message = (
                 formatted_message[:level_name_start]
                 + level_color
                 + record.levelname
                 + COLOR_RESET
                 + formatted_message[level_name_end:]
             )

        return formatted_message

# --- Central Logging Setup Function ---
def setup_logging():
    """Configures logging based on LOGGING_MODE environment variable."""
    log_mode = os.environ.get("LOGGING_MODE", "PRODUCTION").upper()
    print(f"--- Configuring Logging Mode: {log_mode} ---") # Print mode early

    # Default level (for production)
    default_level = logging.WARNING
    log_levels = {
        "src": default_level,
        "websockets": logging.WARNING,
        "google": logging.WARNING,
        "httpcore": logging.WARNING,
    }

    # Configure based on mode
    if log_mode == "DEVELOPMENT":
        log_levels["src"] = logging.INFO
        log_levels["websockets"] = logging.INFO
    elif log_mode == "WEBSOCKET_DEBUG":
        log_levels["src"] = logging.INFO
        log_levels["src.handlers.websocket_handler"] = logging.DEBUG
        log_levels["websockets"] = logging.DEBUG
        log_levels["httpcore"] = logging.DEBUG
    elif log_mode == "ORCHESTRATOR_DEBUG":
        log_levels["src"] = logging.INFO
        log_levels["src.core.orchestrator"] = logging.DEBUG
        log_levels["src.core.llm_service"] = logging.DEBUG
        # --- Quieten the adapter in this mode ---
        log_levels["src.core.gemini_adapter"] = logging.INFO # Changed from DEBUG to INFO
        # --- End adapter quietening ---
        log_levels["src.core.mcp_coordinator"] = logging.DEBUG
        log_levels["websockets"] = logging.WARNING
        log_levels["httpcore"] = logging.WARNING
        log_levels["google"] = logging.WARNING
    elif log_mode == "PRODUCTION":
        pass # Keep defaults
    else:
        print(f"--- WARNING: Unknown LOGGING_MODE '{log_mode}'. Using PRODUCTION defaults. ---")
        log_mode = "PRODUCTION"

    # Get root logger and set basic config
    # We configure the root logger's handler, not using basicConfig directly
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG) # Capture everything at root, filter at handler/logger level

    # Remove existing handlers to avoid duplicates if script is re-run in some contexts
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)

    # Create console handler and set level & formatter
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.DEBUG) # Handler level, let loggers control filtering
    # Use ColorFormatter if terminal supports color (basic check)
    if sys.stdout.isatty():
         formatter = ColorFormatter()
    else:
         formatter = logging.Formatter(
              '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
              datefmt='%Y-%m-%d %H:%M:%S'
         )
    console_handler.setFormatter(formatter)

    # Add handler to the root logger
    root_logger.addHandler(console_handler)

    # Apply specific levels
    for name, level in log_levels.items():
        logging.getLogger(name).setLevel(level)
        # Also log the final level being set for clarity during startup
        logging.debug(f"Setting log level for '{name}' to {logging.getLevelName(level)}")

    logging.info(f"Logging configured for {log_mode} mode.") # Use logger after setup

# --- Password Hashing --- # - REMOVED
# You need to install bcrypt: pip install bcrypt
# import bcrypt # REMOVED

# Utility function to hash a password (use this offline to generate hashes)
# def hash_password(plain_password: str) -> bytes:
#     if not isinstance(plain_password, str):
#         raise TypeError("Password must be a string")
#     password_bytes = plain_password.encode('utf-8')
#     salt = bcrypt.gensalt()
#     hashed = bcrypt.hashpw(password_bytes, salt)
#     return hashed

# Utility function to verify a password (used by the handler)
# Note: We'll actually perform verification inside the handler using bcrypt.checkpw
# def verify_password(plain_password: str, hashed_password: bytes) -> bool:
#     if not isinstance(plain_password, str) or not isinstance(hashed_password, bytes):
#          return False # Or raise TypeError
#     password_bytes = plain_password.encode('utf-8')
#     return bcrypt.checkpw(password_bytes, hashed_password)
# --- End Password Hashing --- #

# Ensure the src directory is in the Python path if running from root
# This might not be needed depending on how you run it, but can help
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

# Load environment variables (from .env file at project root)
from dotenv import load_dotenv
load_dotenv(dotenv_path=project_root / '.env')

# --- Call Logging Setup EARLY ---
setup_logging() # Configure logging before importing other modules that might log

# Import core components
from src.core.gemini_adapter import GeminiAdapter
from src.core.llm_service import LLMService
from src.core.mcp_coordinator import MCPCoordinator
from src.core.orchestrator import ConversationOrchestrator
from src.handlers.websocket_handler import WebSocketHandler

# --- Configuration ---

# Path to MCP configuration (relative to project root)
MCP_CONFIG_PATH = str(project_root / "mcp.json")
# Path to the system prompt file
SYSTEM_PROMPT_PATH = str(project_root / "system_prompt.txt")

# WebSocket Server Configuration
HOST = "0.0.0.0" # Listen on all interfaces for container compatibility
PORT = 8765 # Default WebSocket port, change if needed

# --- User Authorization Configuration --- - MODIFIED
# Configuration only contains user-specific prompt additions.
# Authentication (password hashing/checking) is handled by web_gateway.py.
AUTHORIZED_USERS = {
    "axantillon@gmail.com": {
        # "hashed_password": os.environ.get("TONY_HASH"), # REMOVED - Handled by gateway
        "prompt_addition": "You are currently interacting with Tony Stark (User: axantillon@gmail.com). Address him with appropriate respect (e.g., 'Sir', or directly when appropriate). He expects concise, technically accurate information but appreciates your wit."
    },
    "aguilarcarboni@gmail.com": {
        # "hashed_password": os.environ.get("PETER_HASH"), # REMOVED - Handled by gateway
        "prompt_addition": "You are currently interacting with Peter Parker (User: aguilarcarboni@gmail.com). Be friendly, slightly more casual, and helpful. He might need more detailed explanations of complex technical topics (just kidding, he's quite sharp!)."
    }
    # Add other original users back if needed, but only their prompt_addition
}

# --- Validate Loaded Hashes --- # - REMOVED
# def validate_auth_config(auth_dict):
#     all_valid = True
#     for email, data in auth_dict.items():
#         if not data.get("hashed_password"):
#             print(f"CRITICAL ERROR: Missing hashed_password for user '{email}'. Ensure corresponding environment variable (e.g., TONY_HASH) is set.")
#             all_valid = False
#         # Optional: Add check for bcrypt hash format validity here if needed
#     return all_valid

# AUTH_CONFIG_VALID = validate_auth_config(AUTHORIZED_USERS) # REMOVED
# --- End User Authorization Configuration & Validation --- # - REMOVED

async def main():
    """Initializes components and starts the server."""
    logging.info("--- Starting Laserfocus Host ---")

    # --- ADD Check for Auth Config Validity --- # - REMOVED
    # if not AUTH_CONFIG_VALID:
    #     print("Exiting due to invalid authorization configuration.")
    #     return
    # --- END Check --- # - REMOVED

    # 1. Load API Key
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        logging.critical("CRITICAL ERROR: GEMINI_API_KEY environment variable not set.")
        logging.critical("Please create a .env file in the project root or set the variable.")
        return

    # 1b. Load Authorized Users from Environment Variable (REMOVED)
    # authorized_users_json = os.environ.get("LFH_AUTHORIZED_USERS_JSON")
    # authorized_users = {}
    # ... (removed loading and parsing logic) ...
    # Use the hardcoded dictionary directly
    authorized_users = AUTHORIZED_USERS # Still pass this to handler for prompt additions
    if not authorized_users:
        logging.warning("WARNING: AUTHORIZED_USERS dictionary is empty. No user-specific prompts can be applied.")
        # Decide if this should be a critical error and return
    else:
        logging.info(f"Loaded user prompt configurations for {len(authorized_users)} user(s).")

    # 2. Load Base System Prompt from file
    try:
        with open(SYSTEM_PROMPT_PATH, 'r') as f:
             base_system_prompt = f.read()
        logging.info(f"Loaded system prompt from {SYSTEM_PROMPT_PATH}")
    except FileNotFoundError:
        logging.critical(f"CRITICAL ERROR: System prompt file not found at {SYSTEM_PROMPT_PATH}.")
        return
    except Exception as e:
        logging.critical(f"CRITICAL ERROR: Failed to read system prompt file: {e}")
        return

    # 2b. Inject dynamic information into the prompt
    # NOTE: This happens *before* user authentication. We format the user-specific
    # part later in the WebSocketHandler.
    try:
        fs_root = os.environ.get("MCP_FS_ROOT", "<Not Specified>") # Get path from env
        filesystem_info = f"You have access to the local filesystem within the directory: '{fs_root}'"
        # Replace only the filesystem placeholder in the template for now
        # The {persona_definition} will be handled per-user.
        base_system_prompt_template = base_system_prompt.replace("{filesystem_access_info}", filesystem_info)
        # base_system_prompt_template = base_system_prompt.format(filesystem_access_info=filesystem_info)
        logging.info("Base system prompt template processed with dynamic fs info.")
    except Exception as e:
         logging.warning(f"WARNING: Failed to format base system prompt template with dynamic info: {e}")
         base_system_prompt_template = base_system_prompt # Fallback to original template

    # 3. Initialize LLM Adapter and Service
    try:
        logging.info("Initializing LLM Components...")
        adapter = GeminiAdapter(api_key=api_key)
        # Pass the BASE prompt TEMPLATE and authorized users to the handler.
        # The final formatting with persona happens inside the handler.
        llm_service = LLMService(adapter=adapter, base_system_prompt=base_system_prompt_template)
        logging.info("LLM Components Initialized.")
    except Exception as e:
        logging.critical(f"CRITICAL ERROR: Failed to initialize LLM components: {e}", exc_info=True)
        # traceback.print_exc() # No longer needed, exc_info=True does this
        return

    # 4. Initialize MCP Coordinator (using async with for proper lifecycle)
    try:
        # MCPCoordinator handles its own initialization logging internally
        async with MCPCoordinator(config_path=MCP_CONFIG_PATH) as mcp_coordinator:
            logging.info("MCP Coordinator Context Entered.")

            # 5. Initialize Orchestrator
            logging.info("Initializing Conversation Orchestrator...")
            orchestrator = ConversationOrchestrator(
                llm_service=llm_service,
                mcp_coordinator=mcp_coordinator, # Pass the initialized coordinator
                # We no longer pass the base prompt here directly,
                # as it will be determined per-user in the handler
            )
            logging.info("Conversation Orchestrator Initialized.")

            # 6. Initialize WebSocket Handler
            logging.info("Initializing WebSocket Handler...")
            # Pass the BASE prompt TEMPLATE and authorized users (for prompt additions) to the handler.
            handler = WebSocketHandler(
                orchestrator=orchestrator,
                base_system_prompt_template=base_system_prompt_template,
                authorized_users=authorized_users # Still needed for prompt additions
            )
            logging.info("WebSocket Handler Initialized.")

            # 7. Start WebSocket Server
            await handler.start_server(host=HOST, port=PORT)

    except FileNotFoundError:
         logging.critical(f"CRITICAL ERROR: MCP config file not found at '{MCP_CONFIG_PATH}'.")
    except ValueError as e:
         # Catch config loading errors from MCPCoordinator
         logging.critical(f"CRITICAL ERROR: Failed to load or validate MCP config: {e}")
    except RuntimeError as e:
         # Catch TaskGroup or Client init errors
         logging.critical(f"CRITICAL ERROR: Runtime error during initialization: {e}", exc_info=True)
    except Exception as e:
        logging.critical(f"CRITICAL ERROR: An unexpected error occurred: {e}", exc_info=True)
    finally:
         logging.info("--- Laserfocus Host Shutting Down ---")


if __name__ == "__main__":
    # Handle potential policy issues on Windows for asyncio
    if os.name == 'nt':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("\nServer stopped manually.")
    except Exception as e:
         # Catch errors during asyncio.run itself if any occur outside main()
         logging.critical(f"Fatal error during asyncio execution: {e}", exc_info=True)
