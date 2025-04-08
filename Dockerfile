# Dockerfile
# Build environment for the laserfocus-host application
# Includes both the main backend and the web gateway

# Use an official Python runtime as a parent image
FROM python:3.11-slim

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE 1 # Prevents python from writing .pyc files
ENV PYTHONUNBUFFERED 1      # Force stdout/stderr streams to be unbuffered
# Set default paths for MCP tools inside the container
ENV MCP_FS_ROOT=/app/data
ENV MCP_GIT_REPO=/app

# Set the working directory in the container
WORKDIR /app

# Install system dependencies (including Node.js for MCP tools using npx)
# Update apt, install curl & gnupg, add NodeSource repo, install Node.js 20.x, git, clean up
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    # Base tools
    curl gnupg ca-certificates git \
    # Node.js
    nodejs \
    # Add Node build tools (might be needed by npx packages)
    build-essential python3 \
    # Font dependencies (sometimes needed for rendering)
    # fonts-liberation \
    && \
    # Node.js setup (moved after initial installs)
    mkdir -p /etc/apt/keyrings && \
    curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg && \
    NODE_MAJOR=20 && \
    echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_$NODE_MAJOR.x nodistro main" | tee /etc/apt/sources.list.d/nodesource.list && \
    apt-get update && \
    # Install specific Node.js version from NodeSource repo
    apt-get install nodejs -y && \
    # Clean up apt cache to reduce image size
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Verify installations (optional)
RUN node --version && npm --version && git --version

# Create a directory for the filesystem tool if needed
RUN mkdir /app/data

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
COPY system_prompt.txt .

# Ensure the app directory is owned by a non-root user if needed,
# but for simplicity now, just ensure permissions.
# RUN chown -R someuser:somegroup /app 
# Set permissions explicitly
RUN chmod -R 755 /app

# Make the start script executable
RUN chmod +x ./start.sh

# Expose the port the web gateway will listen on
# Render uses the PORT env var, but exposing helps document/local runs.
# Defaulting to 8000 if PORT isn't set in start.sh
EXPOSE 8000

# Command to run verification then the application using the startup script
CMD bash -c "echo '--- Verifying memory file ---' && ls -la /app/memory.json && echo '--- Checking memory file content ---' && head -n 10 /app/memory.json || echo '!!! memory.json not found or empty !!!' && echo '--- Verification complete ---' && exec ./start.sh" 