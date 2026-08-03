# 🏗️ Proxify Dockerfile
# Production-grade image with headless browser support.
#
# BUILD CONTEXT: the `proxify/` directory (../ from this file) so the sibling
# `Lib++` package can be COPY'd in. docker-compose.yml sets context: ..
#
# Container-fingerprint hardening included:
#   - fonts installed (an empty font list is an instant container giveaway)
#   - PLAYWRIGHT_BROWSERS_PATH pinned so browsers land in a known location

FROM python:3.10-slim-bullseye

# 1. Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    NODE_PATH=/usr/lib/node_modules \
    TZ=UTC

# 2. Install system dependencies for Playwright/Scrapling/Puppeteer
# These are required to run Chromium in a headless environment
RUN apt-get update && apt-get install -y --no-install-recommends \
    libnss3 \
    libnspr4 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libxkbcommon0 \
    libxcomposite1 \
    libxdamage1 \
    libxext6 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libasound2 \
    ca-certificates \
    curl \
    gnupg \
    tzdata \
    fonts-liberation \
    fonts-dejavu-core \
    fonts-dejavu-extra \
    fonts-noto-core \
    && rm -rf /var/lib/apt/lists/*

# 3. Install Node.js 20 LTS (required for Puppeteer strategy)
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

# 4. Install Puppeteer npm packages globally (NODE_PATH above ensures require() resolves)
RUN npm install -g puppeteer puppeteer-extra puppeteer-extra-plugin-stealth puppeteer-extra-plugin-user-preferences puppeteer-extra-plugin-user-data-dir

# 5. Set working directory
WORKDIR /app

# 6. Install Python dependencies
COPY proxy-orchestrator/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 7. Install Playwright browsers (Chromium only to save space)
RUN playwright install chromium
RUN playwright install-deps chromium

# 8. Copy application code (orchestrator at /app root, Lib++ as sibling package)
COPY proxy-orchestrator /app
COPY Lib++ /app/Lib++

# 9. Install Lib++ as a Python package
#    - Upgrade pip/setuptools first: old pip uses legacy egg-link editable
#      installs which fail for the package_dir rename (Lib++ -> Lib_plus_plus).
#    - Symlink fallbacks make the import name work regardless (main.py adds
#      /app to sys.path).
RUN pip install --no-cache-dir --upgrade pip setuptools wheel && \
    pip install --no-cache-dir -e /app/Lib++ && \
    ln -sfn /app/Lib++ /app/Lib_plus_plus && \
    ln -sfn /app/Lib++ /app/Lib__

# 10. Create certs directory (will be populated on startup if missing)
RUN mkdir -p .certs

# 11. Expose ports (Proxy: 8888, API: 8080)
EXPOSE 8888 8080

# 12. Healthcheck
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8080/health || exit 1

# 13. Run the application
CMD ["python", "main.py"]
