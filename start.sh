#!/bin/bash
# start.sh
# Script to run both the main backend and the web gateway concurrently
# Changes: Run gateway in foreground, remove wait/cleanup logic.

echo "Starting main backend server (src/main.py) in background..."
# Assuming src/main.py runs without arguments and listens on 8765
# Use python -u for unbuffered output to see logs immediately
python -u src/main.py &
MAIN_PID=$!
echo "Main backend server PID: $MAIN_PID"

echo "Starting web gateway server (web_gateway.py) in foreground..."
# The gateway needs to be accessible from outside the container.
# Render typically expects the service to listen on port $PORT, often 10000.
# We'll run uvicorn bound to 0.0.0.0 and port $PORT (or default 8000 if $PORT isn't set).
# Ensure web_gateway.py's BACKEND_WS_URI points to ws://localhost:8765 (correct inside container)
PORT=${PORT:-8000} # Use Render's PORT env var, default to 8000
# Use --no-access-log for cleaner logs unless debugging needed
# Run uvicorn in the FOREGROUND (removed trailing &)
uvicorn web_gateway:app --host 0.0.0.0 --port $PORT --no-access-log

# Exit with the status of the gateway process
GATEWAY_EXIT_STATUS=$?
echo "Web gateway server exited with status $GATEWAY_EXIT_STATUS"

# Optionally attempt to gracefully stop the background backend if needed
if kill -0 $MAIN_PID 2>/dev/null; then
    echo "Terminating main backend server (PID: $MAIN_PID)..."
    kill $MAIN_PID
    wait $MAIN_PID # Wait for it to actually terminate
fi

echo "Exiting start script."
exit $GATEWAY_EXIT_STATUS 