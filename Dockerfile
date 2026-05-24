# Use Python 3.12 as base image
FROM python:3.12-slim

# Install system dependencies for audio support and music21
RUN apt-get update && apt-get install -y \
    libasound2-dev \
    libgl1 \
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    libxrender-dev \
    libgomp1 \
    git \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy requirements and install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the entire project
COPY . .

# Set PYTHONPATH so pianoplayer module is accessible directly from source
ENV PYTHONPATH=/app

# Set environment variable to prevent Python from buffering output
ENV PYTHONUNBUFFERED=1

# Default command - start the web app
CMD ["python", "app.py"]
