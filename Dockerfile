# Use Python 3.12 as base image
FROM python:3.12-slim

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    # Audio and display libraries for music21
    libasound2-dev \
    libgl1 \
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    libxrender-dev \
    libgomp1 \
    # Build tools
    git \
    curl \
    unzip \
    xz-utils \
    # Java JRE for Audiveris OMR
    default-jre-headless \
    # Fonts for PDF rendering
    fonts-liberation \
    fonts-urw-base35 \
    # MuseScore CLI for PDF export and format conversion
    musescore3 \
    && rm -rf /var/lib/apt/lists/*

# Download Audiveris .deb and extract JAR for OMR (PDF to MusicXML)
RUN mkdir -p /opt/audiveris \
    && curl -fsSL "https://github.com/Audiveris/audiveris/releases/download/5.10.2/Audiveris-5.10.2-ubuntu22.04-x86_64.deb" \
    -o /tmp/audiveris.deb \
    && dpkg-deb -x /tmp/audiveris.deb /tmp/audiveris-extracted \
    && cp /tmp/audiveris-extracted/opt/Audiveris/Audiveris.jar /opt/audiveris/audiveris.jar \
    && rm -rf /tmp/audiveris.deb /tmp/audiveris-extracted

# Set working directory
WORKDIR /app

# Copy requirements and install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir gunicorn

# Copy the entire project
COPY . .

# Set environment variables
ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1
ENV AUDIVERIS_JAR_PATH=/opt/audiveris/audiveris.jar
ENV MUSESCORE_PATH=musescore3
ENV AUDIVERIS_JAVA_HEAP=2G
ENV OMR_TIMEOUT=600
ENV PDF_EXPORT_TIMEOUT=120
ENV QT_QPA_PLATFORM=offscreen

# Expose port
EXPOSE 5000

# Default command - production with gunicorn (extended timeout for PDF pipeline)
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--timeout", "900", "--workers", "2", "app:app"]
