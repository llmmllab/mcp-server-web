# Web MCP Server - Docker image for Kubernetes deployment
# Build: docker build -t ${REGISTRY:-localhost:31500}/mcp-server-web:${TAG:-latest} .

FROM python:3.12-slim

LABEL maintainer="Scott Long <scott@llmmllab.com>"
LABEL description="Web Search & Fetch MCP Server"
LABEL version="1.0.0"

# Prevent interactive prompts during build
ENV DEBIAN_FRONTEND=noninteractive

# Set working directory
WORKDIR /app

# Install system deps: curl, uv, and Playwright browser dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    # Playwright Chromium dependencies
    libnss3 \
    libnspr4 \
    libdbus-1-3 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libxkbcommon0 \
    libatspi2.0-0 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libpango-1.0-0 \
    libcairo2 \
    libasound2 \
    libx11-xcb1 \
    && curl -LsSf https://astral.sh/uv/install.sh | sh \
    && mv /root/.local/bin/uv /usr/local/bin/uv \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# Copy application code
COPY . .

# Install dependencies
RUN uv pip install --system .

# Install Playwright browsers
RUN playwright install chromium

# Create non-root user and set ownership
RUN useradd -m -s /bin/bash mcp && \
    chown -R mcp:mcp /app

# Switch to non-root user
USER mcp

# Expose port for streaming HTTP transport
EXPOSE 8000

CMD ["uv", "run", "python", "server.py"]
