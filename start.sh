#!/bin/bash
# start.sh
# Script to run both the main backend and the web gateway concurrently

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
uvicorn web_gateway:app --host 0.0.0.0 --port $PORT --no-access-log &
GATEWAY_PID=$!
echo "Web gateway server PID: $GATEWAY_PID"

# Wait for either process to exit, report which one exited
wait -n $MAIN_PID $GATEWAY_PID
EXIT_STATUS=$?
echo "One of the processes exited with status $EXIT_STATUS"

# Gracefully terminate the other process if it's still running
if kill -0 $MAIN_PID 2>/dev/null; then
    echo "Terminating main backend server (PID: $MAIN_PID)..."
    kill $MAIN_PID
    wait $MAIN_PID
elif kill -0 $GATEWAY_PID 2>/dev/null; then
    echo "Terminating web gateway server (PID: $GATEWAY_PID)..."
    kill $GATEWAY_PID
    wait $GATEWAY_PID
fi

echo "Exiting start script."
exit $EXIT_STATUS 