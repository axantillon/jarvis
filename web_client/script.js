// web_client/script.js
// --- WebSocket Client Logic for Web Interface ---
// Changes:
// - Initial implementation for web client

const wsUri = "ws://localhost:8765"; // Replace with your server URI if different
const messageInput = document.getElementById("messageInput");
const sendButton = document.getElementById("sendButton");
const messagesDiv = document.getElementById("messages");
const promptSpan = document.getElementById("prompt");
const statusIndicator = document.getElementById("status-indicator");
let websocket;
let currentJarvisMessageDiv = null; // To append streaming text

function initWebSocket() {
  console.log("Attempting to connect to WebSocket...");
  addSystemMessage("Connecting to server...", "status");
  setPromptState("disconnected");
  websocket = new WebSocket(wsUri);

  websocket.onopen = function (evt) {
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
  console.log("CONNECTED");
  addSystemMessage("Connected to WebSocket server.", "connection"); // Clearer message
  enableInput();
}

function onClose(evt) {
  console.log("DISCONNECTED");
  addSystemMessage(
    `Disconnected: ${evt.reason || "No reason provided"} (Code: ${evt.code})`,
    "error"
  );
  disableInput("disconnected");
  // Optional: Try to reconnect after a delay
  // setTimeout(initWebSocket, 5000);
}

function onMessage(evt) {
  console.log("MESSAGE RECEIVED:", evt.data);
  hideProcessingIndicator(); // Hide indicator on any message
  try {
    const message = JSON.parse(evt.data);
    const msg_type = message.type;
    const payload = message.payload || {};

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
        // Optionally re-enable input on server-side error if appropriate
        enableInput(); // Re-enable input after error message
        break;
      case "end":
        currentJarvisMessageDiv = null; // End potential streaming
        enableInput(); // Re-enable input when server signals end
        break;
      case "connection": // Server confirms connection details
        currentJarvisMessageDiv = null; // End potential streaming
        // Update connection message or add session ID if needed
        addSystemMessage(
          `Connected. Session ID: ${payload.sessionId}`,
          "connection"
        );
        enableInput(); // Ensure input is enabled after connection confirmation
        break;
      default:
        console.warn("Received unknown message type:", msg_type);
        addSystemMessage(`Received unknown data: ${evt.data}`, "status");
    }
  } catch (e) {
    console.error("Error parsing message or processing:", e);
    addSystemMessage(`Error processing server message: ${e.message}`, "error");
    currentJarvisMessageDiv = null; // End potential streaming
    enableInput(); // Try to recover input ability
  }
  scrollToBottom();
}

function onError(evt) {
  console.error("WebSocket Error:", evt);
  // The 'onclose' event will usually fire immediately after 'onerror'
  // Add a generic error message here, onClose will handle the disconnected state
  addSystemMessage("WebSocket error occurred. Check console.", "error");
  hideProcessingIndicator();
  disableInput("disconnected"); // Ensure input is disabled on error
  currentJarvisMessageDiv = null; // End potential streaming
}

function sendMessage() {
  const messageText = messageInput.value.trim();
  if (messageText && websocket && websocket.readyState === WebSocket.OPEN) {
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

    // Display user message immediately
    addUserMessage(messageText);
    messageInput.value = "";
    disableInput("processing"); // Disable input while waiting for response
    showProcessingIndicator(); // Show processing indicator
    currentJarvisMessageDiv = null; // Reset for next response
  } else {
    console.warn("Cannot send message. WebSocket not open or message empty.");
    if (!websocket || websocket.readyState !== WebSocket.OPEN) {
      addSystemMessage("Not connected. Please wait or refresh.", "error");
    }
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
  currentJarvisMessageDiv.appendChild(document.createTextNode(text));
}

function addSystemMessage(text, type) {
  // Types: 'status', 'error', 'connection'
  addMessageToLog(text, type);
}

function enableInput() {
  messageInput.disabled = false;
  sendButton.disabled = false;
  setPromptState("ready");
  hideProcessingIndicator();
  messageInput.focus(); // Focus input when ready
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

messageInput.addEventListener("keypress", function (event) {
  // Check if Enter key was pressed (key code 13)
  if (event.key === "Enter" || event.keyCode === 13) {
    event.preventDefault(); // Prevent default form submission/newline
    sendMessage();
  }
});

// --- Initialization ---
document.addEventListener("DOMContentLoaded", initWebSocket); // Start WebSocket connection when the page loads
