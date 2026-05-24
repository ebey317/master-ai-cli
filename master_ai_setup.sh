#!/bin/bash

# Master AI Setup Script
# Based on ~/scripts/howwework.txt architecture

echo "Starting Master AI setup..."

# Install dependencies
sudo apt update
sudo apt install -y git curl python3 python3-pip tmux

# Install Ollama
curl -fsSL https://ollama.ai/install.sh | sh

# Configure Ollama to limit loaded models
sudo mkdir -p /etc/systemd/system/ollama.service.d
sudo tee /etc/systemd/system/ollama.service.d/keep-alive.conf <<EOF
[Service]
Environment="OLLAMA_MAX_LOADED_MODELS=2"
EOF

# Pull the required models
ollama pull qwen2.5:3b
ollama pull qwen2.5:7b
ollama pull llava:latest

# Rebuild master-ai model with Sensei persona
if [ -f ~/scripts/Modelfile-master-ai ]; then
    ollama create master-ai -f ~/scripts/Modelfile-master-ai
else
    echo "Warning: Modelfile-master-ai not found. Skipping master-ai model rebuild."
fi

# Create necessary directories
mkdir -p ~/scripts
mkdir -p ~/Desktop/AI_CONTEXT
mkdir -p ~/.master_ai_chats
touch ~/Desktop/AI_CONTEXT/context_latest.txt

# Set up TTS server (if not already present)
if [ ! -f ~/scripts/stt_server.py ]; then
    echo "TTS server script not found. Please add stt_server.py to ~/scripts/"
fi

# Set up UI services
if [ ! -f ~/scripts/sensei_bridge.py ]; then
    echo "Sensei script not found. Please add sensei_bridge.py to ~/scripts/"
fi

if [ ! -f ~/scripts/pupil.html ]; then
    echo "Pupil UI not found. Please add pupil.html to ~/scripts/"
fi

# Restart Ollama to apply changes
sudo systemctl daemon-reload
sudo systemctl restart ollama

echo "Master AI setup complete. Please check ~/scripts/ for missing files and configure services as needed."
echo "To start, run: bash ~/scripts/launch_master_ai.sh"