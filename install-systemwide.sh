#!/usr/bin/env bash
# ==============================================================================
# install-systemwide.sh
# Installs opencode and free-ai system-wide for all users on Ubuntu
# Usage: sudo ./install-systemwide.sh
# ==============================================================================

set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo -e "\033[1;31mError: This script must be run as root (using sudo):\033[0m"
  echo "  sudo $0"
  exit 1
fi

echo -e "\033[1;36m========================================================\033[0m"
echo -e "\033[1;32m  Installing OpenCode & Free-AI CLI System-Wide        \033[0m"
echo -e "\033[1;36m========================================================\033[0m"

SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_SOURCE="$SOURCE_DIR/bin"
MICHIEL_BIN="/home/michiel/.local/bin"
MICHIEL_OPENCODE="/home/michiel/.opencode/bin/opencode"

# 1. Locate the OpenCode binary
OPENCODE_BIN=""
if [ -f "$MICHIEL_OPENCODE" ]; then
  OPENCODE_BIN="$MICHIEL_OPENCODE"
elif [ -f "/home/pennytree-dev/.local/bin/opencode" ]; then
  OPENCODE_BIN="/home/pennytree-dev/.local/bin/opencode"
elif command -v opencode >/dev/null 2>&1; then
  OPENCODE_BIN="$(command -v opencode)"
fi

if [ -z "$OPENCODE_BIN" ]; then
  echo -e "\033[1;31mError: opencode binary not found!\033[0m" >&2
  exit 1
fi

echo "1. Installing OpenCode binary to /usr/local/lib/opencode..."
mkdir -p /usr/local/lib/opencode
cp "$OPENCODE_BIN" /usr/local/lib/opencode/opencode
chmod 755 /usr/local/lib/opencode/opencode

echo "2. Installing OpenCode multi-user wrapper to /usr/local/bin/opencode..."
cat << 'EOF' > /usr/local/bin/opencode
#!/usr/bin/env bash
# Multi-user OpenCode launcher: dynamically configures unique daemon ports
set -e

USER_UID=$(id -u)
# Assign a deterministic, non-colliding service port based on UID
CALC_PORT=$(( 48000 + (USER_UID % 10000) ))

CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/opencode"
SERVICE_JSON="$CONFIG_DIR/service.json"

if [ ! -f "$SERVICE_JSON" ] || ! grep -q '"port"' "$SERVICE_JSON" 2>/dev/null; then
  mkdir -p "$CONFIG_DIR" 2>/dev/null || true
  if [ -f "$SERVICE_JSON" ]; then
    python3 -c "import json, os; p='$SERVICE_JSON'; d=json.load(open(p)) if os.path.exists(p) else {}; d['port']=$CALC_PORT; json.dump(d, open(p, 'w'), indent=2)" 2>/dev/null || true
  else
    echo "{\"port\": $CALC_PORT}" > "$SERVICE_JSON" 2>/dev/null || true
  fi
  chmod 600 "$SERVICE_JSON" 2>/dev/null || true
fi

exec /usr/local/lib/opencode/opencode "$@"
EOF
chmod 755 /usr/local/bin/opencode

echo "3. Installing system-wide OpenCode configuration to /etc/opencode/opencode.json..."
mkdir -p /etc/opencode
cat << 'EOF' > /etc/opencode/opencode.json
{
  "$schema": "https://opencode.ai/config.json",
  "providers": {
    "auto-free": {
      "name": "LiteLLM Router (Auto Rate-Limit Fallback)",
      "package": "@opencode/ai/providers/openai-compatible",
      "env": [
        "AUTO_FREE_API_KEY"
      ],
      "headers": {
        "Authorization": "Bearer sk-free-llm"
      },
      "settings": {
        "baseURL": "http://127.0.0.1:5111/v1"
      },
      "models": {
        "free-coder": {
          "name": "Auto Free Failover Router (All 15 Providers)"
        },
        "mistral/codestral-latest": {
          "name": "Mistral Codestral 256K (Dedicated Coding)"
        },
        "gemini/gemini-3.8-flash": {
          "name": "Google Gemini 3.8 Flash 1M (Multimodal Reasoning)"
        },
        "groq/openai/gpt-oss-120b": {
          "name": "Groq GPT-OSS 120B (Frontier Reasoning ~200ms)"
        },
        "groq/qwen/qwen3.8-27b": {
          "name": "Groq Qwen 27B (Math & Algorithmic Code)"
        },
        "openrouter/nvidia/nemotron-3-ultra-550b-a55b:free": {
          "name": "NVIDIA Nemotron 3 Ultra 550B (1M Context Frontier)"
        },
        "openrouter/cohere/north-mini-code:free": {
          "name": "Cohere North Mini Code (OpenRouter Free)"
        }
      }
    }
  },
  "model": "auto-free/free-coder"
}
EOF
chmod 644 /etc/opencode/opencode.json

echo "4. Installing shared helper scripts to /usr/local/share/free-ai..."
mkdir -p /usr/local/share/free-ai
for f in summarize-context.py verify-keys.js auto-heal-llms.py; do
  src=""
  if [ -f "$BIN_SOURCE/$f" ]; then
    src="$BIN_SOURCE/$f"
  elif [ -f "$MICHIEL_BIN/$f" ]; then
    src="$MICHIEL_BIN/$f"
  fi
  if [ -n "$src" ]; then
    cp "$src" /usr/local/share/free-ai/
    chmod 755 "/usr/local/share/free-ai/$f"
  fi
done

echo "5. Installing free-ai CLI to /usr/local/bin/free-ai..."
FREE_AI_SRC=""
if [ -f "$BIN_SOURCE/free-ai" ]; then
  FREE_AI_SRC="$BIN_SOURCE/free-ai"
elif [ -f "$MICHIEL_BIN/free-ai" ]; then
  FREE_AI_SRC="$MICHIEL_BIN/free-ai"
fi
if [ -n "$FREE_AI_SRC" ]; then
  cp "$FREE_AI_SRC" /usr/local/bin/free-ai
  chmod 755 /usr/local/bin/free-ai
fi

echo "6. Configuring /etc/profile.d/free-ai.sh for universal environment defaults..."
cat << 'EOF' > /etc/profile.d/free-ai.sh
# /etc/profile.d/free-ai.sh
# System-wide Free LLM Router key for OpenCode
export AUTO_FREE_API_KEY="${AUTO_FREE_API_KEY:-sk-free-llm}"
EOF
chmod 644 /etc/profile.d/free-ai.sh

echo ""
echo -e "\033[1;32m✓ System-wide installation complete!\033[0m"
echo "  - OpenCode binary:  /usr/local/bin/opencode"
echo "  - OpenCode config:  /etc/opencode/opencode.json"
echo "  - Free-AI CLI:      /usr/local/bin/free-ai"
echo "  - Shared libraries: /usr/local/share/free-ai/"
echo "  - Environment:      /etc/profile.d/free-ai.sh"
echo ""
echo "All current and newly created users on this machine can now run:"
echo "  opencode"
echo "  free-ai status"
echo "  free-ai test"
echo "  free-ai summarize <file>"
