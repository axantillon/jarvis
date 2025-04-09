# src/cli_client.py
# --- Simple Async WebSocket CLI Client ---
# Purpose: Connects to the Laserfocus Host WebSocket server (direct backend connection),
#          prompts for email, sends an identification message, and allows interaction.
# Changes:
# - Fixed color rendering and proper handling of end signals
# - Improved prompt and output formatting to ensure clear separation
# - Restored indented JARVIS response style
# - Fixed initial input prompt alignment
# - Made status messages more subtle for fluidity
# - Added processing indicator (with delay)
# - MODIFIED: Now prompts for email and sends an 'identify' message upon connection.
# - Fixed initial '>>>' prompt timing.
# - Added --debug flag for printing raw request/response JSON + prompts + status.
# - Suppressed formatted server responses and logs in debug mode.
# - Re-enabled processing status indicator in debug mode.
# - Added pretty-printing and syntax highlighting for JSON in debug mode.

import asyncio
import websockets
import json
import threading
import queue
from rich.console import Console
from rich.text import Text
from rich.status import Status
from rich.syntax import Syntax
from typing import Optional
import logging
import argparse # Added for command-line arguments
# import sys # No longer needed for direct stdin reading

# Setup basic logging for the client
# We configure basicConfig but will disable specific loggers more aggressively
logging.basicConfig(level=logging.WARNING, format='%(asctime)s - %(levelname)s - %(message)s')
# --- Aggressively disable websockets logger ---
logging.getLogger("websockets").propagate = False
logging.getLogger("websockets").addHandler(logging.NullHandler())
# --- End aggressive disable ---

# Server configuration
SERVER_HOST = "localhost"
SERVER_PORT = 8765
SERVER_URI = f"ws://{SERVER_HOST}:{SERVER_PORT}"
PROCESSING_INDICATOR_DELAY = 1.75 # Seconds before showing indicator

# Shared variables
input_queue = queue.Queue()
exit_flag = threading.Event()
console = Console()
is_processing = False # Flag to track if LLM is thinking (request sent, no response yet)
processing_status: Optional[Status] = None # Holds the *visible* rich status object
indicator_timer_task: Optional[asyncio.Task] = None # Task for delayed indicator start
debug_mode = False # Global flag for debug mode

# --- Helper: Pretty print JSON ---
def pretty_print_json(prefix: str, json_string: str):
    """Attempts to parse, pretty-print, and syntax-highlight JSON with word wrap."""
    try:
        data = json.loads(json_string)
        pretty_string = json.dumps(data, indent=4)
        syntax = Syntax(
            pretty_string,
            "json",
            theme="friendly",
            line_numbers=False,
            word_wrap=True
        )
        console.print(prefix, syntax)
    except json.JSONDecodeError:
        # Fallback for non-JSON messages
        console.print(f"{prefix} [dim](Not JSON)[/dim] {json_string}")
    except Exception as e:
        # Fallback for other errors
        console.print(f"{prefix} [red](Error formatting JSON: {e})[/] {json_string}")

# --- Helper to start the visible indicator ---
async def show_processing_indicator():
    global processing_status, is_processing, debug_mode
    # --- Indicator now shows in both modes ---

    # Only show if we are still waiting for a response
    if is_processing:
        # Ensure no duplicate status exists
        if processing_status:
            processing_status.stop()
        # Use regular console; status should appear where expected
        processing_status = console.status("[bold yellow]Processing...[/]", spinner="dots")
        processing_status.start()

# --- Input Thread (Needs slight adjustment for status interaction) ---
def input_thread_function():
    global processing_status, debug_mode
    try:
        while not exit_flag.is_set():
            # If the visible status is active, briefly stop it for input echo
            if processing_status:
                processing_status.stop()

            # Use input() directly - let the main loop/receiver print '>>> '
            user_input = input() # Reads line after prompt (which is printed elsewhere)

            # If the visible status was stopped, restart it
            if processing_status:
                processing_status.start()

            input_queue.put(user_input)
            if user_input.lower() == 'quit':
                break
    except EOFError: # Handle Ctrl+D during input
        if not debug_mode:
             console.print("[yellow]Input stream closed.[/]")
        input_queue.put('quit') # Treat EOF as quit signal
    except Exception as e:
        if processing_status: processing_status.stop() # Stop status on error
        if not debug_mode:
             console.print(f"[bold red]Input error: {e}[/]")
    finally:
        if not exit_flag.is_set(): input_queue.put(None)

# --- WebSocket Message Receiver ---
async def receive_websocket_messages(websocket):
    global is_processing, processing_status, indicator_timer_task, debug_mode
    try:
        currently_speaking = False

        while True:
            message_str = await websocket.recv()

            # Stop status indicator as soon as *any* message is received
            if indicator_timer_task and not indicator_timer_task.done():
                indicator_timer_task.cancel()
                indicator_timer_task = None
            if processing_status:
                processing_status.stop()
                processing_status = None
            is_processing = False # Mark processing as complete

            # --- DEBUG: Pretty print raw received message ---
            if debug_mode:
                 # Use the helper function for pretty printing
                 pretty_print_json("RAW RECV <<<", message_str)

                 # --- Parse JSON again (needed for prompt logic) ---
                 try:
                     message = json.loads(message_str)
                     msg_type = message.get("type")
                     if msg_type in ["end", "identify_success"]:
                         console.print(Text(">>> ", style="bold green"), end="")
                     elif msg_type == "identify_fail":
                         # Error message printed by pretty_print_json, just don't print prompt
                         pass
                 except json.JSONDecodeError:
                     pass # Ignore non-JSON
                 except Exception:
                     pass # Ignore other errors
                 # --- Skip ALL other processing/printing in debug mode ---
                 continue
            # --- END DEBUG ---

            # --- The following only runs if NOT in debug mode ---

            try: # Add try-except for JSON decoding
                 message = json.loads(message_str)
            except json.JSONDecodeError:
                 console.print(f"[bold red]Received invalid JSON: {message_str}[/]")
                 continue # Skip processing this message

            msg_type = message.get("type")
            payload = message.get("payload", {})

            # --- Identify handling ---
            if msg_type == "identify_success":
                 logging.info(f"CLIENT: Identification successful. Session ID: {payload.get('sessionId')}")
                 console.print()
                 console.print(Text(">>> ", style="bold green"), end="") # Show prompt
                 currently_speaking = False
                 continue
            elif msg_type == "identify_fail":
                 if currently_speaking: console.print(); currently_speaking = False
                 reason = payload.get("message", "Unknown reason.")
                 console.print(f"\nIdentification Failed: {reason}", style="bold red")
                 exit_flag.set()
                 await websocket.close()
                 break
            # --- End identify handling ---

            elif msg_type == "text":
                if not currently_speaking:
                    console.print()
                    console.print(Text("JARVIS:", style="bold cyan"))
                    currently_speaking = True
                console.print(Text(f"  {payload.get('content', '')}", style="cyan"), end="")

            elif msg_type == "status":
                if currently_speaking: console.print(); currently_speaking = False
                status_text = Text()
                status_text.append("\nSTATUS: ", style="bold yellow")
                status_text.append(payload.get('message', ''), style="yellow")
                console.print(status_text)
                if payload.get('tool'):
                     tool_text = Text()
                     tool_text.append("  Tool: ", style="bold yellow")
                     tool_text.append(payload.get('tool'), style="yellow")
                     console.print(tool_text)

            elif msg_type == "error":
                if currently_speaking: console.print(); currently_speaking = False
                error_text = Text()
                error_text.append("\nERROR: ", style="bold red")
                error_text.append(payload.get('message', 'Unknown error'), style="red")
                console.print(error_text)

            elif msg_type == "end":
                if currently_speaking: console.print(); currently_speaking = False
                console.print() # Print newline for spacing
                console.print(Text(">>> ", style="bold green"), end="") # Print prompt after JARVIS response

    # --- Exception Handling ---
    except websockets.exceptions.ConnectionClosedOK:
        if processing_status: processing_status.stop()
        if not exit_flag.is_set():
             console.print("\nConnection closed.", style="bold yellow")
        exit_flag.set()
    except websockets.exceptions.ConnectionClosedError as e:
        if processing_status: processing_status.stop()
        console.print(f"\nConnection closed with error: {e}", style="bold red")
        exit_flag.set()
    except Exception as e:
        if processing_status: processing_status.stop()
        console.print(f"\nError receiving messages: {e}", style="bold red")
        exit_flag.set()

async def send_identification(websocket, email):
    """Sends the identification message (type: identify) to the server."""
    global debug_mode
    identification_message = {
        "type": "identify",
        "email": email
    }
    message_str = json.dumps(identification_message)
    # --- DEBUG: Pretty print raw sent message ---
    if debug_mode:
        # Use the helper function for pretty printing
        pretty_print_json("RAW SEND >>>", message_str)
    # --- END DEBUG ---
    await websocket.send(message_str)
    # Show "Sent identification" in both modes
    print(f"Sent identification for {email}.")

# --- Main Application Logic ---
async def main(debug=False): # Accept debug flag
    global is_processing, processing_status, indicator_timer_task, debug_mode
    debug_mode = debug # Set global debug flag

    # --- Show initial connection message in both modes ---
    console.print(f"Connecting to {SERVER_URI}... ", style="bold")
    # --- End show ---

    # Get user email for identification
    try:
        # --- Show prompt in both modes ---
        prompt_text = "[bold yellow]Enter your email for identification: [/]"
        user_email = console.input(prompt_text)
        # --- End show ---
        if not user_email:
            # --- Show print in both modes ---
            console.print("Email cannot be empty. Exiting.", style="bold red")
            # --- End show ---
            return
    except EOFError:
        # --- Show print in both modes ---
        console.print("\nInput cancelled. Exiting.", style="bold yellow")
        # --- End show ---
        return
    except KeyboardInterrupt: # Catch Ctrl+C during email input
        # --- Show print in both modes ---
        console.print("\nOperation cancelled by user.", style="bold yellow")
        # --- End show ---
        return

    try:
        async with websockets.connect(SERVER_URI) as websocket:
            # --- Show print in both modes ---
            console.print("Connected! Identifying...", style="bold green")
            # --- End show ---

            # Send the identify message (no password)
            await send_identification(websocket, user_email)

            # Initial prompt '>>> ' is printed by receiver on identify_success or end

            input_thread = threading.Thread(target=input_thread_function)
            input_thread.daemon = True
            input_thread.start()

            receive_task = asyncio.create_task(receive_websocket_messages(websocket))

            while not exit_flag.is_set():
                try:
                    # Print prompt before waiting for input (relevant for first turn after connect)
                    # The receiver handles printing prompt after subsequent server messages.
                    # This needs to be printed only once initially after identification success.
                    # Let's rely on the receiver printing it after identify_success.

                    try:
                        user_input = input_queue.get(block=False)
                    except queue.Empty:
                        await asyncio.sleep(0.1)
                        continue

                    if user_input is None: break # Should be triggered by input thread exit
                    if user_input.lower() == 'quit':
                        # --- Show print in both modes ---
                        console.print("Disconnecting...", style="bold yellow")
                        # --- End show ---
                        break

                    # --- Start processing (request sent) & timer ---
                    is_processing = True # Set flag for indicator logic
                    # Cancel previous timer if exists
                    if indicator_timer_task and not indicator_timer_task.done():
                        indicator_timer_task.cancel()
                    # Start new timer to show indicator after delay (indicator func shows in both modes now)
                    indicator_timer_task = asyncio.create_task(
                        asyncio.sleep(PROCESSING_INDICATOR_DELAY),
                        name="IndicatorDelay" # Name for easier debugging if needed
                    )
                    # Add callback to show indicator IF timer completes successfully
                    indicator_timer_task.add_done_callback(
                        lambda task: asyncio.create_task(show_processing_indicator()) if not task.cancelled() else None
                    )
                    # --- Timer started ---

                    message_to_send = {"type": "message", "payload": {"text": user_input}}
                    message_str = json.dumps(message_to_send)
                    # --- DEBUG: Pretty print raw sent message ---
                    if debug_mode:
                        # Use the helper function for pretty printing
                        pretty_print_json("RAW SEND >>>", message_str)
                    # --- END DEBUG ---
                    await websocket.send(message_str)

                except websockets.exceptions.ConnectionClosed:
                     # Server closed connection while we were trying to send/process input
                     # Show error in both modes
                     console.print("\nConnection lost.", style="bold red")
                     exit_flag.set() # Ensure we exit the loop
                     break # Exit send loop
                except Exception as e:
                    # Stop timer/status on send error
                    if indicator_timer_task: indicator_timer_task.cancel()
                    if processing_status: processing_status.stop()
                    is_processing = False
                    # --- Show error in both modes ---
                    console.print(f"\nError sending message: {e}", style="bold red")
                    # Print the prompt again (works for both modes)
                    console.print(Text(">>> ", style="bold green"), end="")

    except websockets.exceptions.InvalidURI:
        # Show error in both modes
        console.print(f"Invalid WebSocket URI: {SERVER_URI}", style="bold red")
    except ConnectionRefusedError:
        # Show error in both modes
        console.print(f"Connection refused by server at {SERVER_URI}. Is it running?", style="bold red")
    except Exception as e:
        # Catch errors during connection itself
        # Show error in both modes
        console.print(f"\nFailed to connect or run client: {e}", style="bold red")
        logging.error("Client connection/runtime error", exc_info=True) # Log details always
    finally:
        # Signal exit flag definitively
        exit_flag.set()
        # Stop status indicator if it's somehow still running
        if processing_status: processing_status.stop()
        # Cancel timer task if it's still pending
        if indicator_timer_task and not indicator_timer_task.done(): indicator_timer_task.cancel()

        # Ensure the receive task is awaited/cancelled properly on exit
        # Check if receive_task was successfully created before trying to cancel
        if 'receive_task' in locals() and receive_task:
            receive_task.cancel()
            try:
                await receive_task
            except asyncio.CancelledError:
                 pass # Expected if cancelled
                 # logging.info("Receive task cancelled.") # Suppress log in debug
            except Exception as e_recv:
                 # Log unexpected errors during receive task cleanup
                 logging.error(f"Error during receive task cleanup: {e_recv}", exc_info=True)

        # Ensure input thread is joined
        input_queue.put(None) # Unblock the input queue if thread is waiting
        if 'input_thread' in locals() and input_thread and input_thread.is_alive():
             input_thread.join(timeout=1.0) # Wait briefly for thread cleanup
             if input_thread.is_alive():
                 logging.warning("Input thread did not exit cleanly.") # Log always

    console.print("Disconnected.", style="bold yellow")

# --- Entry Point ---
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="CLI Client for Laserfocus Host. In debug mode, prints pretty JSON, prompts, and status.")
    parser.add_argument("-d", "--debug", action="store_true", help="Enable debug mode (pretty print JSON + prompts + status)")
    args = parser.parse_args()

    try:
        asyncio.run(main(debug=args.debug)) # Pass debug flag to main
    except KeyboardInterrupt:
        # Show manual stop message in both modes
        print("\nClient stopped manually.") # Use plain print here
    except Exception as e:
        # Catch errors during asyncio.run itself
        # Show fatal error message in both modes
        print(f"\nFatal error during client execution: {e}") # Use plain print
        logging.error("Fatal error during client execution", exc_info=True) # Log detailed traceback anyway
