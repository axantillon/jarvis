#!/bin/bash
# start.sh
# Entrypoint script for the Docker container
# Added verification for memory.json readability and exported path
# Removed ps check (ps not available in slim image)

# Exit immediately if a command exits with a non-zero status.
set -e

# --- Verify memory.json accessibility right before app start ---
echo "--- [start.sh] Verifying /app/memory.json ---"
if [ -f "/app/memory.json" ]; then
    echo "--- [start.sh] /app/memory.json exists. ---"
    echo "--- [start.sh] Permissions: $(ls -l /app/memory.json) ---"
    echo "--- [start.sh] Attempting to read first few lines: ---"
    head -n 5 /app/memory.json
    echo "--- [start.sh] Attempting to read full file via cat (check for errors): ---"
    cat /app/memory.json > /dev/null # Check readability, discard output
    echo "--- [start.sh] /app/memory.json seems readable. ---"
else
    echo "--- [start.sh] CRITICAL: /app/memory.json NOT FOUND! ---"
fi
echo "--- [start.sh] Explicitly exporting MEMORY_FILE_PATH ---"
export MEMORY_FILE_PATH="/app/memory.json"
echo "--- [start.sh] MEMORY_FILE_PATH is set to: $MEMORY_FILE_PATH ---"

# Define the log file path
LOG_FILE="/app/data/main_backend.log"

# Ensure the data directory exists (where logs might go)
echo "Ensuring /app/data directory exists..."
mkdir -p /app/data

# Start the main backend server (src/main.py) in the background
# Redirect stdout and stderr to the log file
echo "Starting main backend server (src/main.py) in background..."
# Ensure PYTHONPATH includes the src directory if needed, although WORKDIR /app usually suffices
# export PYTHONPATH=/app:$PYTHONPATH
python src/main.py > "${LOG_FILE}" 2>&1 &
BACKEND_PID=$!
echo "Main backend server PID: ${BACKEND_PID}"

# Wait a few seconds to allow the backend server to initialize
# Adjust sleep duration if needed
echo "Waiting briefly for backend server..."
sleep 5

# Removed ps check block - ps command not available in slim image
# If backend fails, it should log to ${LOG_FILE}

# Start the web gateway server (web_gateway.py) in the foreground
echo "Starting web gateway server (web_gateway.py) in foreground..."
# The PORT variable is usually set by Render. Default to 8000 if not set.
# Uvicorn binds to 0.0.0.0 to be accessible outside the container.
exec uvicorn web_gateway:app --host 0.0.0.0 --port ${PORT:-3000} --reload

# Note: --reload is typically for development. Consider removing it for production deployments. 