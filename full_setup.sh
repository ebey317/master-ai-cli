#!/bin/bash

# Install dependencies
sudo apt update
sudo apt install -y git curl python3 python3-pip

# Configure Ollama
sudo mkdir -p /etc/ollama
sudo tee /etc/ollama/ollama.conf <<EOF
max_loaded_models=2
EOF

# Set up models
mkdir -p ~/models
curl -LO https://example.com/qwen2.5-3b.tar.gz
tar -xvf qwen2.5-3b.tar.gz -C ~/models
curl -LO https://example.com/qwen2.5-7b.tar.gz
tar -xvf qwen2.5-7b.tar.gz -C ~/models
curl -LO https://example.com/llava-latest.tar.gz
tar -xvf llava-latest.tar.gz -C ~/models

# Configure services
sudo tee /etc/systemd/system/ollama.service <<EOF
[Unit]
Description=Ollama Service
After=network.target

[Service]
User=elijah
ExecStart=/usr/bin/ollama
Restart=always

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable ollama
sudo systemctl start ollama

# Create directories and files
mkdir -p ~/scripts
mkdir -p ~/Desktop/AI_CONTEXT
touch ~/Desktop/AI_CONTEXT/context_latest.txt

# Configure TTS
sudo apt install -y espeak
sudo tee /etc/espeak.conf <<EOF
voice=default
EOF

# Set up UI
sudo apt install -y xdg-utils
sudo tee ~/scripts/pupil.html <<EOF
<!-- Pupil UI HTML content -->
EOF

# Set up Sensei
sudo tee ~/scripts/sensei.py <<EOF
<!-- Sensei Python code -->
EOF

# Configure dojo gate
sudo tee ~/scripts/dojo_gate.py <<EOF
<!-- Dojo gate Python code -->
EOF

# Finalize setup
echo "Master AI setup complete."
EOF
