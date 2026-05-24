#!/bin/bash

apt-get update -y
apt-get install nodejs npm -y
# # Install the main tool
npm i -g opencode-ai
opencode plugin @tarquinen/opencode-dcp@latest --global

# uv for python formatting and linting
wget -qO- https://astral.sh/uv/install.sh | sh
uv tool install ruff@latest

# setup my customizations
git clone git@github.com:hazelement/dot_files.git ~/dot_files
cd ~/dot_files
./install.sh

# agent browser
# npm install -g agent-browser
# agent-browser install --with-deps  # Download Chrome from Chrome for Testing (first time only)
# npx skills add vercel-labs/agent-browser -y



