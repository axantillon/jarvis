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

# Install system dependencies if needed (e.g., for certain Python packages)
# RUN apt-get update && apt-get install -y --no-install-recommends some-package && rm -rf /var/lib/apt/lists/*

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
# Copy any other necessary files like mcp.json if it's at the root
# COPY mcp.json .

# Make the start script executable
RUN chmod +x ./start.sh

# Expose the port the web gateway will listen on
# Render uses the PORT env var, but exposing helps document/local runs.
# Defaulting to 8000 if PORT isn't set in start.sh
EXPOSE 8000

# Command to run the application using the startup script
CMD ["./start.sh"] 