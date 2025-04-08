# src/main.py
# --- Application Entry Point ---
# Purpose: Initializes all core components and starts the WebSocket server.
# Changes:
# - Initial implementation.

import asyncio
import os
import sys
import traceback
from pathlib import Path

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

async def main():
    """Initializes components and starts the server."""
    print("--- Starting Laserfocus Host ---")

    # 1. Load API Key
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("CRITICAL ERROR: GEMINI_API_KEY environment variable not set.")
        print("Please create a .env file in the project root or set the variable.")
        return

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
    try:
        fs_root = os.environ.get("MCP_FS_ROOT", "<Not Specified>") # Get path from env
        filesystem_info = f"You have access to the local filesystem within the directory: '{fs_root}'"
        # Replace placeholder in the template
        final_system_prompt = base_system_prompt.format(filesystem_access_info=filesystem_info)
        print("System prompt finalized with dynamic info.")
    except Exception as e:
         print(f"WARNING: Failed to format system prompt with dynamic info: {e}")
         final_system_prompt = base_system_prompt # Fallback to template

    # 3. Initialize LLM Adapter and Service
    try:
        print("Initializing LLM Components...")
        adapter = GeminiAdapter(api_key=api_key)
        # Pass the *finalized* prompt content
        llm_service = LLMService(adapter=adapter, base_system_prompt=final_system_prompt)
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
                mcp_coordinator=mcp_coordinator # Pass the initialized coordinator
            )
            print("Conversation Orchestrator Initialized.")

            # 6. Initialize WebSocket Handler
            print("Initializing WebSocket Handler...")
            handler = WebSocketHandler(orchestrator=orchestrator)
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
