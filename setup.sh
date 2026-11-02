#!/usr/bin/env bash
# ==============================================================================
# setup.sh - Turnkey Setup for FreeAIRouter & OpenCode
# ==============================================================================
# Sets up LiteLLM failover router, Docker container, free-ai CLI, and OpenCode
# ==============================================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

BOLD="\033[1m"
GREEN="\033[1;32m"
CYAN="\033[1;36m"
YELLOW="\033[1;33m"
RED="\033[1;31m"
RESET="\033[0m"

echo -e "${CYAN}========================================================${RESET}"
echo -e "${GREEN}   🤖 FreeAIRouter — Turnkey Zero-Cost AI Hub Setup     ${RESET}"
echo -e "${CYAN}========================================================${RESET}"

# 1. Check prerequisites
echo -e "\n${YELLOW}1. Checking prerequisites...${RESET}"

if ! command -v docker >/dev/null 2>&1; then
  echo -e "  ${RED}✗ Docker is required but not installed.${RESET}"
  echo "    Please install Docker: https://docs.docker.com/engine/install/"
  exit 1
fi
echo -e "  ${GREEN}✓ Docker is installed${RESET} ($(docker --version))"

if ! docker compose version >/dev/null 2>&1; then
  echo -e "  ${RED}✗ Docker Compose is required.${RESET}"
  exit 1
fi
echo -e "  ${GREEN}✓ Docker Compose is available${RESET} ($(docker compose version))"

if ! command -v curl >/dev/null 2>&1; then
  echo -e "  ${RED}✗ curl is required.${RESET}"
  exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
  echo -e "  ${RED}✗ python3 is required.${RESET}"
  exit 1
fi

# 2. Check Environment Configuration (.env)
echo -e "\n${YELLOW}2. Checking environment keys (.env)...${RESET}"

if [ ! -f "$SCRIPT_DIR/.env" ]; then
  if [ -f "$HOME/notepad/free-llms/.env" ]; then
    echo -e "  Found existing key file in ~/notepad/free-llms/.env, linking it..."
    cp "$HOME/notepad/free-llms/.env" "$SCRIPT_DIR/.env"
  else
    echo -e "  ${CYAN}Creating .env from .env.example...${RESET}"
    cp "$SCRIPT_DIR/.env.example" "$SCRIPT_DIR/.env"
    echo -e "  ${YELLOW}! Created .env. Please edit .env and add at least one free API key.${RESET}"
    echo -e "    Reference catalog: https://github.com/mnfst/awesome-free-llm-apis"
  fi
else
  echo -e "  ${GREEN}✓ .env file present${RESET}"
fi

# 3. Create state directories
mkdir -p "$SCRIPT_DIR/state"
chmod 777 "$SCRIPT_DIR/state" 2>/dev/null || true

# 4. Start LiteLLM Docker Compose Router
echo -e "\n${YELLOW}3. Launching LiteLLM Router container (:5111)...${RESET}"
docker compose up -d

echo -e "  Waiting for LiteLLM router readiness probe on http://127.0.0.1:5111..."
READY=false
for i in {1..40}; do
  if curl -sf http://127.0.0.1:5111/health/readiness >/dev/null 2>&1; then
    READY=true
    break
  fi
  sleep 0.5
done

if [ "$READY" = true ]; then
  echo -e "  ${GREEN}✓ LiteLLM router is healthy on http://127.0.0.1:5111${RESET}"
else
  echo -e "  ${YELLOW}⚠ Router is starting up. Check status with: docker compose logs -f${RESET}"
fi

# 5. Install free-ai CLI helper
echo -e "\n${YELLOW}4. Installing free-ai CLI into ~/.local/bin...${RESET}"
mkdir -p "$HOME/.local/bin"
cp "$SCRIPT_DIR/bin/free-ai" "$HOME/.local/bin/free-ai"
chmod +x "$HOME/.local/bin/free-ai"

cp "$SCRIPT_DIR/bin/summarize-context.py" "$HOME/.local/bin/summarize-context.py"
chmod +x "$HOME/.local/bin/summarize-context.py"

if [ -f "$SCRIPT_DIR/bin/auto-heal-llms.py" ]; then
  cp "$SCRIPT_DIR/bin/auto-heal-llms.py" "$HOME/.local/bin/auto-heal-llms.py"
  chmod +x "$HOME/.local/bin/auto-heal-llms.py"
fi

if [ -f "$SCRIPT_DIR/bin/verify-keys.js" ]; then
  cp "$SCRIPT_DIR/bin/verify-keys.js" "$HOME/.local/bin/verify-keys.js"
  chmod +x "$HOME/.local/bin/verify-keys.js"
fi

# Ensure ~/.local/bin is in PATH for bash/zsh
for rc in "$HOME/.bashrc" "$HOME/.zshrc"; do
  if [ -f "$rc" ] && ! grep -q '\.local/bin' "$rc"; then
    echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$rc"
  fi
done

echo -e "  ${GREEN}✓ Installed 'free-ai' CLI tool to ~/.local/bin/free-ai${RESET}"

# 6. Configure OpenCode
echo -e "\n${YELLOW}5. Configuring OpenCode CLI...${RESET}"
if command -v opencode >/dev/null 2>&1; then
  echo -e "  ${GREEN}✓ OpenCode is installed${RESET} ($(opencode --version 2>/dev/null || true))"
else
  echo -e "  ${CYAN}OpenCode is not yet installed.${RESET}"
  echo "  You can install it instantly with: curl -fsSL https://opencode.ai/v2/install | bash"
fi

OPENCODE_DIR="$HOME/.config/opencode"
mkdir -p "$OPENCODE_DIR"
if [ ! -f "$OPENCODE_DIR/opencode.json" ]; then
  cp "$SCRIPT_DIR/opencode/opencode.json" "$OPENCODE_DIR/opencode.json"
  echo -e "  ${GREEN}✓ Created ~/.config/opencode/opencode.json defaulting to 'auto-free/free-coder'${RESET}"
else
  echo -e "  ${GREEN}✓ OpenCode config already exists at ~/.config/opencode/opencode.json${RESET}"
fi

# 7. Verification Test
echo -e "\n${YELLOW}6. Running quick router completion test...${RESET}"
"$SCRIPT_DIR/bin/free-ai" test

echo -e "\n${GREEN}========================================================${RESET}"
echo -e "${GREEN}   ✓ FreeAIRouter is setup and ready!                   ${RESET}"
echo -e "${GREEN}========================================================${RESET}"
echo -e "Quick Usage:"
echo -e "  ${BOLD}opencode${RESET}                 Start coding with auto-failover AI"
echo -e "  ${BOLD}free-ai status${RESET}           Show router health and provider status"
echo -e "  ${BOLD}free-ai test${RESET}             Run live completion test"
echo -e "  ${BOLD}free-ai last${RESET}             Check which model answered your last prompt"
echo -e "  ${BOLD}free-ai watch${RESET}            Stream real-time routing decisions"
echo -e "  ${BOLD}free-ai summarize <file>${RESET} Executive summary via fast frontier model"
echo ""
