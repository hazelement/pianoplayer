# Use Python 3.9 as base image
FROM python:3.9-slim

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

# Install pianoplayer in development mode
RUN pip install -e .

# Create output directory for generated files
RUN mkdir -p /app/output

# Set environment variable to prevent Python from buffering output
ENV PYTHONUNBUFFERED=1

# Default command - show help
CMD ["pianoplayer", "--help"]
