FROM python:3.11-slim

# Avoid writing .pyc files and enable unbuffered output
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Install minimal system dependencies for image/document processing
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libmagic1 \
    && rm -rf /var/lib/apt/lists/*

# Install python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source code
COPY . .

# Ensure storage directories exist
RUN mkdir -p storage/uploads storage/hosted

# Default port for local testing, overridden dynamically by cloud platform
ENV PORT=8000
EXPOSE 8000

# Launch server binding to $PORT dynamically
CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT:-8000}"]
