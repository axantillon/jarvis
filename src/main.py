# src/main.py
# --- Application Entry Point ---
# Purpose: Initializes all core components and starts the WebSocket server.
# Changes:
# - Initial implementation.
# - Added AUTHORIZED_USERS dictionary for email/password auth, requires bcrypt.
# - Reverted user config to be hardcoded in this file due to env var parsing issues.

import asyncio
import os
import sys
import traceback
# import json # No longer needed
from pathlib import Path

# --- Password Hashing --- #
# You need to install bcrypt: pip install bcrypt
import bcrypt

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

# --- User Authorization Configuration ---
# Configuration is hardcoded here, but hashes are loaded from env vars.
# Ensure TONY_HASH and PETER_HASH are set in your .env file.
AUTHORIZED_USERS = {
    "axantillon@gmail.com": {
        "hashed_password": os.environ.get("TONY_HASH"),
        "prompt_addition": "You are currently interacting with Tony Stark (User: axantillon@gmail.com). Address him with appropriate respect (e.g., 'Sir', or directly when appropriate). He expects concise, technically accurate information but appreciates your wit."
    },
    "aguilarcarboni@gmail.com": {
        "hashed_password": os.environ.get("PETER_HASH"),
        "prompt_addition": "You are currently interacting with Peter Parker (User: aguilarcarboni@gmail.com). Be friendly, slightly more casual, and helpful. He might need more detailed explanations of complex technical topics (just kidding, he's quite sharp!)."
    }
    # Add other original users back if needed
}

# --- Validate Loaded Hashes ---
# Check if hashes were loaded correctly after defining the dict
def validate_auth_config(auth_dict):
    all_valid = True
    for email, data in auth_dict.items():
        if not data.get("hashed_password"):
            print(f"CRITICAL ERROR: Missing hashed_password for user '{email}'. Ensure corresponding environment variable (e.g., TONY_HASH) is set.")
            all_valid = False
        # Optional: Add check for bcrypt hash format validity here if needed
    return all_valid

AUTH_CONFIG_VALID = validate_auth_config(AUTHORIZED_USERS)
# --- End User Authorization Configuration & Validation ---

async def main():
    """Initializes components and starts the server."""
    print("--- Starting Laserfocus Host ---")

    # --- ADD Check for Auth Config Validity ---
    if not AUTH_CONFIG_VALID:
        print("Exiting due to invalid authorization configuration.")
        return
    # --- END Check ---

    # 1. Load API Key
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("CRITICAL ERROR: GEMINI_API_KEY environment variable not set.")
        print("Please create a .env file in the project root or set the variable.")
        return

    # 1b. Load Authorized Users from Environment Variable (REMOVED)
    # authorized_users_json = os.environ.get("LFH_AUTHORIZED_USERS_JSON")
    # authorized_users = {}
    # ... (removed loading and parsing logic) ...
    # Use the hardcoded dictionary directly
    authorized_users = AUTHORIZED_USERS
    if not authorized_users:
        print("WARNING: AUTHORIZED_USERS dictionary is empty. No users can log in.")
        # Decide if this should be a critical error and return
    else:
        print(f"Using hardcoded authorized users configuration for {len(authorized_users)} user(s).")

    # 2. Load Base System Prompt from file
    try:
        with open(SYSTEM_PROMPT_PATH, 'r') as f:
             base_system_prompt = f.read()
        print(f"Loaded system prompt from {SYSTEM_PROMPT_PATH}")
    except FileNotFoundError:
        print(f"CRITICAL ERROR: System prompt file not found at {SYSTEM_PROMPT_PATH}.")
        return
    except Exception as e:
        print(f"CRITICAL ERROR: Failed to read system prompt file: {e}")
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
        print("Base system prompt template processed with dynamic fs info.")
    except Exception as e:
         print(f"WARNING: Failed to format base system prompt template with dynamic info: {e}")
         base_system_prompt_template = base_system_prompt # Fallback to original template

    # 3. Initialize LLM Adapter and Service
    try:
        print("Initializing LLM Components...")
        adapter = GeminiAdapter(api_key=api_key)
        # Pass the BASE prompt TEMPLATE and authorized users to the handler.
        # The final formatting with persona happens inside the handler.
        llm_service = LLMService(adapter=adapter, base_system_prompt=base_system_prompt_template)
        print("LLM Components Initialized.")
    except Exception as e:
        print(f"CRITICAL ERROR: Failed to initialize LLM components: {e}")
        traceback.print_exc()
        return

    # 4. Initialize MCP Coordinator (using async with for proper lifecycle)
    try:
        # MCPCoordinator handles its own initialization logging internally
        async with MCPCoordinator(config_path=MCP_CONFIG_PATH) as mcp_coordinator:
            print("MCP Coordinator Context Entered.")

            # 5. Initialize Orchestrator
            print("Initializing Conversation Orchestrator...")
            orchestrator = ConversationOrchestrator(
                llm_service=llm_service,
                mcp_coordinator=mcp_coordinator, # Pass the initialized coordinator
                # We no longer pass the base prompt here directly,
                # as it will be determined per-user in the handler
            )
            print("Conversation Orchestrator Initialized.")

            # 6. Initialize WebSocket Handler
            print("Initializing WebSocket Handler...")
            # Pass the BASE prompt TEMPLATE and authorized users to the handler.
            # The final formatting with persona happens inside the handler.
            handler = WebSocketHandler(
                orchestrator=orchestrator,
                base_system_prompt_template=base_system_prompt_template,
                authorized_users=authorized_users
            )
            print("WebSocket Handler Initialized.")

            # 7. Start WebSocket Server
            await handler.start_server(host=HOST, port=PORT)

    except FileNotFoundError:
         print(f"CRITICAL ERROR: MCP config file not found at '{MCP_CONFIG_PATH}'.")
    except ValueError as e:
         # Catch config loading errors from MCPCoordinator
         print(f"CRITICAL ERROR: Failed to load or validate MCP config: {e}")
    except RuntimeError as e:
         # Catch TaskGroup or Client init errors
         print(f"CRITICAL ERROR: Runtime error during initialization: {e}")
         traceback.print_exc()
    except Exception as e:
        print(f"CRITICAL ERROR: An unexpected error occurred: {e}")
        traceback.print_exc()
    finally:
         print("--- Laserfocus Host Shutting Down ---")


if __name__ == "__main__":
    # Handle potential policy issues on Windows for asyncio
    if os.name == 'nt':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nServer stopped manually.")
    except Exception as e:
         # Catch errors during asyncio.run itself if any occur outside main()
         print(f"Fatal error during asyncio execution: {e}")
         traceback.print_exc()
