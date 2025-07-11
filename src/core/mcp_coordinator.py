# src/core/mcpCoordinator.py

from dataclasses import dataclass
from typing import Dict, List, Optional, Any, Tuple
import json
import asyncio
import sys
import traceback
from pathlib import Path
import os
import logging # <-- Add logging import

from mcp import ClientSession, StdioServerParameters, types, Tool
from mcp.client.stdio import stdio_client

@dataclass
class ServerConfig:
    """Configuration for an MCP server."""
    name: str
    description: str
    type: str
    transport: str
    command: Optional[str] = None
    args: Optional[List[str]] = None
    url: Optional[str] = None
    env: Optional[Dict[str, str]] = None
    timeout_ms: int = 10000

@dataclass
class ToolRegistryEntry:
    """Entry in the tool registry."""
    qualified_name: str
    definition: types.Tool
    server_id: str
    client: ClientSession # Note: This client lives in a background task
    transport_type: str
    reliability: Dict[str, Any]
    performance: Dict[str, Any]

logger = logging.getLogger(__name__) # <-- Add logger definition

class MCPCoordinator:
    def __init__(self, config_path: str):
        """Initialize the MCP Coordinator."""
        self.config_path = Path(config_path)
        self.tool_registry: Dict[str, ToolRegistryEntry] = {}
        self.clients: Dict[str, ClientSession] = {} # Stores active client sessions
        # Note: _client_contexts is removed as contexts are managed within tasks now
        self._server_tasks: Dict[str, asyncio.Task] = {}
        self._tg: Optional[asyncio.TaskGroup] = None
        self._shutdown_event = asyncio.Event()
        # Circuit breaker settings (kept for potential future use)
        self.circuit_threshold = 5
        self.circuit_reset_time_ms = 60000
        logger.info(f"MCPCoordinator initialized with config path: {config_path}") # Use logger.info

    async def _load_config(self) -> Dict[str, ServerConfig]:
        """Load and validate the mcp.json configuration."""
        logger.info(f"Coordinator: Loading config from {self.config_path}...") # Use logger.info
        try:
            with open(self.config_path) as f:
                raw_config = json.load(f)

            # --- Removed SKIP_PUPPETEER check - rely only on mcp.json content --- 
            servers_to_load = raw_config.get("servers", {})
            # skip_puppeteer = os.environ.get("SKIP_PUPPETEER", "false").lower() in ["true", "1"]
            # if skip_puppeteer and "puppeteer" in servers_to_load:
            #     print("Coordinator: SKIP_PUPPETEER is set, removing puppeteer server from config.")
            #     del servers_to_load["puppeteer"]
            # --- End Removed Filter ---

            configs = {}
            # Iterate over the potentially filtered dictionary
            for server_id, server_data in servers_to_load.items():
                # Ensure 'id' isn't passed to ServerConfig constructor if it exists
                server_data.pop('id', None)
                configs[server_id] = ServerConfig(**server_data)
            logger.info(f"Coordinator: Config loaded for {len(configs)} servers: {list(configs.keys())}") # Use logger.info
            return configs
        except FileNotFoundError:
            logger.error(f"Coordinator ERROR: Config file not found at {self.config_path}") # Use logger.error
            raise
        except json.JSONDecodeError as e:
            logger.error(f"Coordinator ERROR: Failed to parse JSON in config file {self.config_path}: {e}") # Use logger.error
            raise ValueError(f"Invalid JSON in MCP configuration file: {e}")
        except Exception as e:
            logger.error(f"Coordinator ERROR: Failed to load or validate config: {e}", exc_info=True) # Use logger.error
            raise ValueError(f"Failed to load MCP configuration from {self.config_path}: {e}")

    async def _discover_tools_for_client(self, server_id: str, client: ClientSession, server_config: ServerConfig):
        """Discover and register tools for a specific client."""
        logger.debug(f"[{server_id}] Discovering tools...") # Use logger.debug
        try:
            tools_result = await client.list_tools()
            tools = tools_result.tools if hasattr(tools_result, 'tools') else []
            count = 0
            for tool in tools:
                qualified_name = f"{server_id}:{tool.name}"
                self.tool_registry[qualified_name] = ToolRegistryEntry(
                    qualified_name=qualified_name,
                    definition=tool,
                    server_id=server_id,
                    client=client, # The client session managed by this task
                    transport_type=server_config.transport,
                    reliability={"success_count": 0, "failure_count": 0, "circuit_open": False, "last_failure": None},
                    performance={"avg_response_time_ms": 0, "call_count": 0, "last_used": None}
                )
                count += 1
            if count > 0:
                logger.info(f"[{server_id}] Registered {count} tools.") # Use logger.info
            else:
                logger.info(f"[{server_id}] No tools discovered.") # Use logger.info
        except Exception as e:
            logger.error(f"[{server_id}] ERROR during tool discovery: {e}", exc_info=True) # Use logger.error

    async def _manage_server_connection(self, server_id: str, server_config: ServerConfig, setup_event: asyncio.Event):
        """Dedicated task managing a single server connection lifecycle."""
        client_session_ref = None
        task_failed = False
        logger.info(f"[{server_id}] Starting management task (Transport: {server_config.transport})") # Use logger.info
        try:
            if server_config.transport == "stdio":
                if not server_config.command:
                    raise ValueError(f"Missing command for stdio server {server_id}")

                # --- Environment Variable Substitution ---
                command = server_config.command
                args = server_config.args or []
                processed_args = []
                try:
                    for arg in args:
                        if isinstance(arg, str) and arg.startswith("${") and arg.endswith("}"):
                            var_name = arg[2:-1]
                            value = os.environ.get(var_name)
                            if value is None:
                                logger.warning(f"[{server_id}] Env var '{var_name}' not found for arg '{arg}'. Using empty string.") # Use logger.warning
                                processed_args.append("")
                            else:
                                logger.debug(f"[{server_id}] Substituted '{arg}' with value from '{var_name}'.") # Use logger.debug
                                processed_args.append(value)
                        else:
                            processed_args.append(arg)
                    logger.info(f"[{server_id}] Launching stdio: '{command}' with args: {processed_args}") # Use logger.info
                except Exception as e:
                    logger.error(f"[{server_id}] Error processing arguments for env var substitution: {e}", exc_info=True) # Use logger.error
                    task_failed = True
                    setup_event.set() # Signal setup is 'done' (failed)
                    return # Don't proceed if args processing failed
                # --- End Substitution ---

                server_params = StdioServerParameters(
                    command=command,            # Pass command separately
                    args=processed_args,        # Pass processed args separately
                    env=server_config.env or {},
                )

                # Add logging for environment variables
                logger.debug(f"[{server_id}] Launching with env: {server_params.env}")

                async with stdio_client(server_params) as (read, write):
                    logger.info(f"[{server_id}] Stdio client process started.") # Use logger.info
                    async with ClientSession(read, write) as session:
                        logger.info(f"[{server_id}] ClientSession established.") # Use logger.info
                        client_session_ref = session
                        await session.initialize()
                        self.clients[server_id] = session
                        await self._discover_tools_for_client(server_id, session, server_config)
                        setup_event.set()
                        logger.info(f"[{server_id}] Setup complete, waiting for shutdown.") # Use logger.info
                        await self._shutdown_event.wait()
                        logger.info(f"[{server_id}] Shutdown signal received.") # Use logger.info
            
            elif server_config.transport == "sse":
                logger.warning(f"[{server_id}] Warning: Unsupported transport 'sse'.") # Use logger.warning
                task_failed = True
                setup_event.set()
            
            else:
                 logger.warning(f"[{server_id}] Warning: Unknown transport '{server_config.transport}'.") # Use logger.warning
                 task_failed = True
                 setup_event.set()

        except asyncio.CancelledError:
            logger.info(f"[{server_id}] Management task cancelled.") # Use logger.info
        except Exception as e:
            logger.error(f"[{server_id}] ERROR in server management task: {e}", exc_info=True) # Use logger.error
            task_failed = True
            if not setup_event.is_set():
                setup_event.set()
        finally:
            logger.info(f"[{server_id}] Management task ending.") # Use logger.info
            if server_id in self.clients and self.clients.get(server_id) is client_session_ref:
                logger.debug(f"[{server_id}] Removing client session.") # Use logger.debug
                del self.clients[server_id]
            keys_to_delete = [key for key in self.tool_registry if key.startswith(f"{server_id}:")]
            if keys_to_delete:
                 logger.debug(f"[{server_id}] Removing {len(keys_to_delete)} tools from registry.") # Use logger.debug
                 for key in keys_to_delete:
                     if key in self.tool_registry: del self.tool_registry[key]

    async def initialize(self) -> None:
        """Initialize by launching server tasks and waiting for their setup."""
        logger.info("Coordinator: Initializing...") # Use logger.info
        config = await self._load_config()

        if not config:
            logger.warning("Coordinator: No servers defined in configuration.") # Use logger.warning
            return

        self._shutdown_event.clear()
        self._server_tasks.clear()
        setup_events: Dict[str, asyncio.Event] = {}
        wait_tasks_map: Dict[asyncio.Task, str] = {}

        if self._tg is None:
            # This should be caught earlier by __aenter__ check
            logger.critical("TaskGroup not initialized. MCPCoordinator must be used with 'async with'.") # Use logger.critical
            raise RuntimeError("TaskGroup not initialized.")

        logger.info(f"Coordinator: Launching {len(config)} server tasks...") # Use logger.info
        for server_id, server_config in config.items():
            setup_event = asyncio.Event()
            setup_events[server_id] = setup_event
            server_task = self._tg.create_task(
                self._manage_server_connection(server_id, server_config, setup_event),
                name=f"mcp_manage_{server_id}" # Name the task
            )
            self._server_tasks[server_id] = server_task
            wait_task = asyncio.create_task(setup_event.wait(), name=f"mcp_wait_setup_{server_id}")
            wait_tasks_map[wait_task] = server_id

        setup_timeout = 120.0 # Increased timeout to 120 seconds
        
        if not wait_tasks_map:
             logger.info("Coordinator: No server setup tasks to wait for.") # Use logger.info
             return

        done, pending = await asyncio.wait(wait_tasks_map.keys(), timeout=setup_timeout)

        failed_servers = []
        successful_servers = []

        if pending:
            logger.warning(f"Coordinator WARNING: Server setup timed out for {len(pending)} server(s):") # Use logger.warning
            for wait_task in pending:
                server_id = wait_tasks_map.get(wait_task, "unknown_timeout")
                logger.warning(f"  - {server_id}") # Use logger.warning
                wait_task.cancel()
                failed_servers.append(server_id)
                if server_id != "unknown_timeout" and server_id in self._server_tasks:
                    logger.warning(f"    Cancelling management task for {server_id} due to timeout.") # Use logger.warning
                    self._server_tasks[server_id].cancel()
            
        for wait_task in done:
             server_id = wait_tasks_map.get(wait_task, "unknown_done")
             if wait_task.cancelled():
                 if server_id not in failed_servers: failed_servers.append(server_id)
             elif wait_task.exception():
                  exc = wait_task.exception()
                  logger.error(f"Coordinator ERROR: Waiting for {server_id} setup failed: {exc}", exc_info=True) # Use logger.error
                  if server_id not in failed_servers: failed_servers.append(server_id)
                  if server_id in self._server_tasks:
                      logger.error(f"    Cancelling management task for {server_id} due to setup error.") # Use logger.error
                      self._server_tasks[server_id].cancel()
             else:
                  successful_servers.append(server_id)

        logger.info("\n--- Coordinator Initialization Summary ---") # Use logger.info
        if successful_servers:
            logger.info("Successfully Initialized Servers:") # Use logger.info
            for server_id in sorted(successful_servers):
                 logger.info(f"  - {server_id}")
        if failed_servers:
            logger.warning("Failed/Timeout Servers:") # Use logger.warning
            for server_id in sorted(failed_servers):
                 logger.warning(f"  - {server_id}")
        if not successful_servers and not failed_servers and config:
            logger.warning("No servers initialized successfully or failed (check config?).") # Use logger.warning
        logger.info("--- End Coordinator Initialization Summary ---") # Use logger.info

    async def call_tool(self, qualified_tool_name: str, arguments: Dict[str, Any]) -> Any:
        """Calls a registered tool by its qualified name."""
        logger.info(f"Coordinator: Received request to call tool: {qualified_tool_name}") # Use logger.info
        logger.debug(f"Coordinator: Tool arguments: {arguments}") # Use logger.debug
        if qualified_tool_name not in self.tool_registry:
            logger.error(f"Tool '{qualified_tool_name}' not found in registry.") # Use logger.error
            raise ValueError(f"Tool '{qualified_tool_name}' not found in registry.")
        
        tool_entry = self.tool_registry[qualified_tool_name]
        client = tool_entry.client
        tool_name = tool_entry.definition.name
        
        try:
            result = await client.call_tool(tool_name, arguments=arguments)
            logger.info(f"Coordinator: Tool '{qualified_tool_name}' executed successfully.") # Use logger.info
            # logger.debug(f"Coordinator: Tool result: {result}") # Optional: log result data at DEBUG
            return result
        except Exception as e:
            logger.error(f"Coordinator ERROR: Calling tool '{qualified_tool_name}' failed: {e}", exc_info=True) # Use logger.error
            raise

    # --- Context Manager Implementation ---
    async def __aenter__(self):
        """Enter the coordinator context, creating task group and initializing."""
        logger.info("Coordinator: Entering async context (__aenter__)...") # Use logger.info
        # Check if TaskGroup already exists? Could happen if reused incorrectly.
        if self._tg is not None:
             logger.warning("Coordinator: TaskGroup already exists on __aenter__. Possible misuse.") # Use logger.warning
             # Decide how to handle: raise error or reuse? For now, let's reuse but log warning.
        else:
             self._tg = asyncio.TaskGroup()
             await self._tg.__aenter__() # Enter the TaskGroup context
        await self.initialize()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Exit the coordinator context, cleaning up resources."""
        logger.info("Coordinator: Exiting async context (__aexit__)...") # Use logger.info
        self._shutdown_event.set()
        logger.info(f"Coordinator: Signalled shutdown to {len(self._server_tasks)} server tasks.") # Use logger.info

        exit_result = False
        try:
            exit_result = await self._tg.__aexit__(exc_type, exc_val, exc_tb)
        except Exception as tg_exit_exc:
             logger.error(f"Coordinator ERROR: Exception during TaskGroup exit: {tg_exit_exc}")

        self.clients.clear()
        self.tool_registry.clear()
        self._server_tasks.clear()
        self._tg = None

        logger.info("Coordinator: Async context exited.") # Use logger.info

            