# Dockerfile
# Build environment for the laserfocus-host application
# Includes both the main backend and the web gateway

# Use an official Python runtime as a parent image
FROM python:3.11-slim

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE 1 # Prevents python from writing .pyc files
ENV PYTHONUNBUFFERED 1      # Force stdout/stderr streams to be unbuffered

# Set the working directory in the container
WORKDIR /app

# Install system dependencies (including Node.js for MCP tools using npx)
# Update apt, install curl & gnupg, add NodeSource repo, install Node.js 20.x, clean up
RUN apt-get update && \
    apt-get install -y --no-install-recommends curl gnupg ca-certificates && \
    mkdir -p /etc/apt/keyrings && \
    curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg && \
    NODE_MAJOR=20 && \
    echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_$NODE_MAJOR.x nodistro main" | tee /etc/apt/sources.list.d/nodesource.list && \
    apt-get update && \
    apt-get install nodejs -y && \
    # Clean up apt cache to reduce image size
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Verify Node.js installation (optional but good practice)
RUN node --version && npm --version

# Copy the requirements file first to leverage Docker cache
COPY requirements.txt .

# Install Python dependencies
# Use --no-cache-dir to reduce image size
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application code
# Ensure all necessary source files and the web_client are copied
COPY src ./src
COPY web_client ./web_client
COPY web_gateway.py .
COPY start.sh .
COPY mcp.json .

# Make the start script executable
RUN chmod +x ./start.sh

# Expose the port the web gateway will listen on
# Render uses the PORT env var, but exposing helps document/local runs.
# Defaulting to 8000 if PORT isn't set in start.sh
EXPOSE 8000

# Command to run the application using the startup script
CMD ["./start.sh"] 