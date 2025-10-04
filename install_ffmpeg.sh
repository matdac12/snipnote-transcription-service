#!/bin/bash
# Install ffmpeg for audio processing with PyDub
# This script is run during Render deployment

echo "📦 Installing ffmpeg..."
apt-get update -qq
apt-get install -y ffmpeg

# Verify installation
if command -v ffmpeg &> /dev/null; then
    echo "✅ ffmpeg installed successfully"
    ffmpeg -version | head -n 1
else
    echo "❌ ffmpeg installation failed"
    exit 1
fi
