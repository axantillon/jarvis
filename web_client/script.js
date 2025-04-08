// web_client/script.js
// --- WebSocket Client Logic for Web Interface ---
// Changes:
// - Point wsUri to the gateway server's /ws endpoint
// - Use relative path for WebSocket connection based on window location
// - Removed separate login form, handling login via chat input ("email password").

// const wsUri = "ws://localhost:8000/ws"; // Old: Hardcoded gateway address

// --- HTML Element References ---
// Remove Login Form elements
// const loginForm = document.getElementById("loginForm");
// const emailInput = document.getElementById("emailInput");
// const passwordInput = document.getElementById("passwordInput");
// const connectButton = document.getElementById("connectButton");
// const loginError = document.getElementById("loginError");
const chatbox = document.getElementById("chatbox"); // Keep chatbox ref

const messageInput = document.getElementById("messageInput");
const sendButton = document.getElementById("sendButton");
const messagesDiv = document.getElementById("messages");
const promptSpan = document.getElementById("prompt");
const statusIndicator = document.getElementById("status-indicator");

let websocket;
let currentJarvisMessageDiv = null; // To append streaming text
let isAuthenticated = false; // Track authentication state

// Determine WebSocket protocol (ws/wss) based on page protocol (http/https)
const wsProtocol = window.location.protocol === "https:" ? "wss:" : "ws:";
let wsHost = window.location.host; // Default to the host the page was loaded from

// --- Example: Explicit check for local development ---
// If you KNOW your local gateway runs on a different port (e.g., 8000)
// and your dev server runs on another (e.g., 8080), you might do this:
if (
  window.location.hostname === "localhost" ||
  window.location.hostname === "127.0.0.1"
) {
  // Option 1: Assume gateway is on a specific port locally
  // wsHost = "localhost:8000"; // Or whatever your local gateway port is
  // Option 2: Keep using relative if your dev server proxies /ws correctly (RECOMMENDED)
  console.log("Local development detected, using relative host:", wsHost);
} else {
  console.log("Production environment detected, using relative host:", wsHost);
}

const wsUri = `${wsProtocol}//${wsHost}/ws`;

console.log("WebSocket URI configured to:", wsUri); // Add log for debugging

function initWebSocket() {
  // Connect automatically on load
  if (
    websocket &&
    (websocket.readyState === WebSocket.CONNECTING ||
      websocket.readyState === WebSocket.OPEN)
  ) {
    console.log("WebSocket connection already in progress or open.");
    return;
  }

  console.log(`Attempting to connect to Gateway WebSocket: ${wsUri}`);
  addSystemMessage("Connecting to gateway...", "status");
  setPromptState("disconnected");
  websocket = new WebSocket(wsUri);

  websocket.onopen = function (evt) {
    // Don't pass credentials anymore
    onOpen(evt);
  };
  websocket.onclose = function (evt) {
    onClose(evt);
  };
  websocket.onmessage = function (evt) {
    onMessage(evt);
  };
  websocket.onerror = function (evt) {
    onError(evt);
  };
}

function onOpen(evt) {
  console.log("GATEWAY CONNECTED");
  // Don't send auth immediately
  // Instead, prompt user and enable input
  addSystemMessage(
    "Connected. Please log in by typing: your-email@example.com yourpassword",
    "connection"
  );
  // Enable input, but don't set isAuthenticated yet
  messageInput.disabled = false;
  sendButton.disabled = false;
  setPromptState("ready"); // Or a custom "login" state?
  messageInput.focus();
}

function onClose(evt) {
  console.log("DISCONNECTED");
  addSystemMessage(
    `Disconnected: ${evt.reason || "No reason provided"} (Code: ${evt.code})`,
    "error"
  );
  // Reset state regardless of previous auth status
  disableInput("disconnected");
  isAuthenticated = false; // Reset auth state
  websocket = null; // Clear websocket reference
  // Optional: Add reconnect logic here if desired
}

function onMessage(evt) {
  console.log("MESSAGE RECEIVED:", evt.data);
  hideProcessingIndicator(); // Hide indicator on any message
  try {
    const message = JSON.parse(evt.data);
    const msg_type = message.type;
    const payload = message.payload || {};

    // --- Handle Authentication Responses ---
    if (msg_type === "auth_success") {
      console.log("Auth Success: Received auth_success message.");
      isAuthenticated = true;
      // hideLoginError(); // Removed
      // showChatInterface(); // Removed - already in chat interface
      addSystemMessage(
        `Authentication successful! Session ID: ${payload.sessionId}`,
        "connection"
      );
      enableInput(); // Ensure input is enabled
      messageInput.placeholder = "Type your message..."; // Change placeholder
      return; // Stop processing this message further
    }
    if (msg_type === "auth_failed") {
      console.log("Auth Failed: Received auth_failed message.");
      isAuthenticated = false;
      // showLoginError(payload.message || "Authentication failed."); // Show in chat
      addSystemMessage(
        `Login Failed: ${
          payload.message || "Please try again."
        } Use format: email password`,
        "error"
      );
      // Don't close connection, allow retry
      // if (websocket) {
      //      websocket.close(1000, "Authentication Failed Client Side");
      // }
      // showLoginForm(); // Removed
      enableInput(); // Ensure input is enabled for retry
      return; // Stop processing this message further
    }

    // --- If already authenticated, handle regular chat messages ---
    if (!isAuthenticated) {
      // This case should ideally not happen if server behaves correctly
      // (i.e., doesn't send chat messages before successful auth)
      console.warn("Received non-auth message before authentication:", message);
      addSystemMessage(
        "Received unexpected message before login. Please login first.",
        "error"
      );
      return;
    }

    // Log authenticated message types
    console.log(`Authenticated message received: Type=${msg_type}`);

    switch (msg_type) {
      case "text":
        handleTextMessage(payload.content || "");
        break;
      case "status":
        currentJarvisMessageDiv = null; // End potential streaming
        addSystemMessage(
          `STATUS: ${payload.message}${
            payload.tool ? `\n  Tool: ${payload.tool}` : ""
          }`,
          "status"
        );
        break;
      case "error":
        currentJarvisMessageDiv = null; // End potential streaming
        addSystemMessage(
          `ERROR: ${payload.message || "Unknown error"}`,
          "error"
        );
        enableInput(); // Re-enable input after server error
        break;
      case "end":
        currentJarvisMessageDiv = null; // End potential streaming
        enableInput(); // Re-enable input when server signals end
        break;
      case "connection": // This might be redundant now with auth_success
        currentJarvisMessageDiv = null; // End potential streaming
        // We already handled the main connection confirmation in auth_success
        console.log("Received legacy connection message (ignoring):");
        // addSystemMessage(`Connected. Session ID: ${payload.sessionId}`, "connection");
        // enableInput(); // Ensure input is enabled after connection confirmation
        break;
      default:
        console.warn("Received unknown message type:", msg_type);
        addSystemMessage(`Received unknown data: ${evt.data}`, "status");
    }
  } catch (e) {
    console.error("Error parsing message or processing:", e);
    addSystemMessage(`Error processing server message: ${e.message}`, "error");
    currentJarvisMessageDiv = null; // End potential streaming
    // Re-enable input cautiously
    enableInput();
  }
  scrollToBottom();
}

function onError(evt) {
  console.error("WebSocket Error:", evt);
  addSystemMessage("WebSocket error occurred. Check console.", "error");
  hideProcessingIndicator();
  disableInput("disconnected"); // Disable input on error
  isAuthenticated = false; // Reset auth state
  currentJarvisMessageDiv = null; // End potential streaming
  websocket = null; // Clear websocket reference
  // onClose will likely be called after this
}

// --- Updated sendMessage to handle login attempt ---
function sendMessage() {
  const messageText = messageInput.value.trim();
  if (!messageText || !websocket || websocket.readyState !== WebSocket.OPEN) {
    console.warn("Cannot send. WebSocket not open or message empty.");
    if (!websocket || websocket.readyState !== WebSocket.OPEN) {
      addSystemMessage("Not connected. Please wait or refresh.", "error");
    }
    return;
  }

  if (!isAuthenticated) {
    // Attempting login
    const parts = messageText.split(/\s+/); // Split by whitespace
    if (parts.length >= 2) {
      const email = parts[0];
      const password = parts.slice(1).join(" "); // Handle passwords with spaces

      const authMessage = {
        type: "auth",
        email: email,
        password: password,
      };
      console.log("SENDING AUTH:", JSON.stringify(authMessage));
      websocket.send(JSON.stringify(authMessage));

      addUserMessage(messageText); // Show what user typed
      addSystemMessage("Attempting login...", "status");
      messageInput.value = "";
      disableInput("processing"); // Disable input during auth attempt
      showProcessingIndicator();
    } else {
      // Invalid login format
      addSystemMessage(
        "Invalid login format. Use: your-email@example.com yourpassword",
        "error"
      );
      messageInput.value = ""; // Clear input
    }
  } else {
    // Already authenticated, send regular message
    if (messageText.toLowerCase() === "quit") {
      addSystemMessage("Disconnecting...", "status");
      websocket.close();
      disableInput("disconnected");
      return;
    }

    const messageToSend = {
      type: "message",
      payload: { text: messageText },
    };
    console.log("SENDING:", JSON.stringify(messageToSend));
    websocket.send(JSON.stringify(messageToSend));

    addUserMessage(messageText);
    messageInput.value = "";
    disableInput("processing");
    showProcessingIndicator();
    currentJarvisMessageDiv = null;
  }
  scrollToBottom();
}

// --- DOM Manipulation Helpers ---

function addMessageToLog(content, className, senderName = null) {
  const messageDiv = document.createElement("div");
  messageDiv.classList.add("message", className);

  if (senderName) {
    const senderSpan = document.createElement("span");
    senderSpan.classList.add("sender");
    senderSpan.textContent = senderName;
    messageDiv.appendChild(senderSpan);
  }

  const contentNode = document.createTextNode(content); // Use text node for safety
  messageDiv.appendChild(contentNode);

  messagesDiv.appendChild(messageDiv);
  return messageDiv; // Return the created div if needed
}

function addUserMessage(text) {
  addMessageToLog(text, "user");
}

function handleTextMessage(text) {
  // If this is the start of a Jarvis message or a continuation
  if (!currentJarvisMessageDiv) {
    // Start a new message block with the sender name
    currentJarvisMessageDiv = addMessageToLog("", "jarvis", "JARVIS:");
  }
  // Append text content (handle potential HTML entities safely)
  // currentJarvisMessageDiv.appendChild(document.createTextNode(text)); // Old way

  // --- Render Markdown --- Use innerHTML carefully
  // marked.parseInline() returns HTML. We append it incrementally.
  // Ensure the server doesn't send malicious markdown. For basic use, this is okay.
  // For more security, consider a sanitizer like DOMPurify after marked.parseInline().
  try {
    // Use parseInline which is better for streaming/appending parts of markdown
    // Switch to marked.parse() to handle block-level elements (headings, lists, code blocks)
    const renderedHtml = marked.parse(text);
    currentJarvisMessageDiv.innerHTML += renderedHtml;
  } catch (e) {
    console.error("Markdown parsing error:", e);
    // Fallback to plain text if markdown parsing fails
    currentJarvisMessageDiv.appendChild(document.createTextNode(text));
  }
  // --- End Render Markdown ---
}

function addSystemMessage(text, type) {
  // Types: 'status', 'error', 'connection'
  addMessageToLog(text, type);
}

function enableInput() {
  // Only enable if authenticated
  if (!isAuthenticated) return;
  messageInput.disabled = false;
  sendButton.disabled = false;
  setPromptState("connected");
  // Maybe focus input?
  // messageInput.focus();
}

function disableInput(reason = "processing") {
  // 'processing' or 'disconnected'
  messageInput.disabled = true;
  sendButton.disabled = true;
  setPromptState(reason);
}

function setPromptState(state) {
  // 'ready', 'processing', 'disconnected'
  promptSpan.className = ""; // Clear existing classes
  promptSpan.classList.add(`prompt-${state}`);
  promptSpan.textContent =
    state === "ready" ? ">>> " : state === "processing" ? "... " : "XXX ";
}

function showProcessingIndicator() {
  statusIndicator.classList.remove("status-hidden");
}

function hideProcessingIndicator() {
  statusIndicator.classList.add("status-hidden");
}

function scrollToBottom() {
  // Use requestAnimationFrame for smoother scrolling after DOM updates
  requestAnimationFrame(() => {
    messagesDiv.scrollTop = messagesDiv.scrollHeight;
  });
}

// --- Event Listeners ---
sendButton.addEventListener("click", sendMessage);
messageInput.addEventListener("keypress", function (e) {
  if (e.key === "Enter") {
    sendMessage();
  }
});

// Remove listener for the connect button
// if (connectButton) { ... }

// --- Initialization ---
document.addEventListener("DOMContentLoaded", () => {
  // Connect automatically now
  initWebSocket();
  // Don't need to manage login form visibility
  // showLoginForm();
  disableInput("disconnected"); // Start with input disabled until connected
});
