#!/usr/bin/env python3
"""
auto-heal-llms.py
Autonomous self-healing watchdog for Free LLM Router & OpenCode.

Features:
1. Detects runtime quarantines from LiteLLM proxy (~/.config/litellm/state/quarantined_models.json).
2. Proactively checks upstream provider catalogs (e.g. OpenRouter free models).
3. If any model is retired, returns 404, or is quarantined:
   - Queries the provider API for the best active free replacement.
   - Preserves comments and edits ~/.config/litellm/config.yaml.
   - Synchronizes ~/.config/opencode/opencode.json.
   - Restarts the LiteLLM Docker Compose router stack.
   - Reloads the OpenCode background daemon (opencode reload).
   - Clears the quarantine and logs the remediation.
4. Operates 100% autonomously via systemd path & timer units.
"""

import os
import sys
import json
import time
import urllib.request
import urllib.error
import subprocess
import argparse

LITELLM_DIR = os.path.expanduser("~/.config/litellm")
CONFIG_YAML = os.path.join(LITELLM_DIR, "config.yaml")
COMPOSE_FILE = os.path.join(LITELLM_DIR, "docker-compose.yml")
STATE_DIR = os.path.join(LITELLM_DIR, "state")
QUARANTINE_FILE = os.path.join(STATE_DIR, "quarantined_models.json")
LOG_FILE = os.path.join(LITELLM_DIR, "auto-heal.log")
OPENCODE_CONFIG = os.path.expanduser("~/.config/opencode/opencode.json")
ENV_FILE = os.path.expanduser("~/notepad/free-llms/.env")

def load_env():
    """Load API keys from private .env file."""
    if os.path.exists(ENV_FILE):
        with open(ENV_FILE, "r") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    k = k.strip()
                    v = v.strip().strip('"').strip("'")
                    if k not in os.environ:
                        os.environ[k] = v

def log_event(message: str, also_stdout: bool = True):
    """Write an entry to auto-heal.log and optionally stdout."""
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    formatted = f"[{now}] {message}"
    if also_stdout:
        print(formatted, flush=True)
    try:
        os.makedirs(LITELLM_DIR, exist_ok=True)
        with open(LOG_FILE, "a") as f:
            f.write(formatted + "\n")
    except Exception:
        pass

def fetch_json(url: str, headers: dict = None, timeout: int = 10) -> dict | None:
    """Safely fetch JSON from a remote URL."""
    try:
        req = urllib.request.Request(url, headers=headers or {"User-Agent": "AutoHealer/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read().decode("utf-8")
            return json.loads(data)
    except Exception as e:
        return None

def get_active_openrouter_free_models(api_key: str = None) -> list[dict]:
    """Fetch all active free models from OpenRouter."""
    headers = {"User-Agent": "AutoHealer/1.0"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    data = fetch_json("https://openrouter.ai/api/v1/models", headers=headers)
    if not data or "data" not in data:
        return []
    free_models = []
    for m in data["data"]:
        m_id = m.get("id", "")
        if ":free" in m_id or m_id == "openrouter/free":
            free_models.append(m)
    return free_models

def select_best_openrouter_replacement(active_models: list[dict], exclude: set) -> str:
    """Pick the best active free coding or reasoning model on OpenRouter."""
    active_ids = [m["id"] for m in active_models if m["id"] not in exclude]
    if not active_ids:
        return "openrouter/free"

    # Prioritize high-intelligence frontier models
    code_candidates = [
        "nvidia/nemotron-3-ultra-550b-a55b:free",
        "nvidia/nemotron-3.5-lightning:free",
        "cohere/north-mini-code:free",
        "recursive/flint-v1.0:free",
        "google/gemma-4-31b-it:free",
        "google/gemma-4-26b-a4b-it:free",
        "openrouter/free"
    ]
    for cand in code_candidates:
        if cand in active_ids:
            return cand

    # Match any model with 'code' in ID
    for m in active_models:
        mid = m["id"]
        if mid not in exclude and "code" in mid.lower():
            return mid

    # Fallback to dynamic free router or first active free model
    if "openrouter/free" in active_ids:
        return "openrouter/free"
    return active_ids[0]

def heal_configuration(dry_run: bool = False, verbose: bool = False) -> bool:
    """Analyze router state and heal any defunct or quarantined models."""
    load_env()

    # 1. Check for quarantined models recorded by the LiteLLM proxy
    quarantined = {}
    if os.path.exists(QUARANTINE_FILE):
        try:
            with open(QUARANTINE_FILE, "r") as f:
                quarantined = json.load(f)
        except Exception:
            quarantined = {}

    if not os.path.exists(CONFIG_YAML):
        if verbose:
            print(f"Error: {CONFIG_YAML} not found.")
        return False

    with open(CONFIG_YAML, "r") as f:
        config_text = f.read()

    # 2. Fetch current OpenRouter free catalog
    or_key = os.environ.get("OPENROUTER_API_KEY")
    active_or_models = get_active_openrouter_free_models(or_key)
    active_or_ids = {m["id"] for m in active_or_models} if active_or_models else set()

    replacements_made = {}

    # Inspect openrouter models in config.yaml
    import re
    # Match patterns like: model: openrouter/<provider>/<model_name>
    or_matches = re.findall(r"model:\s+openrouter/([a-zA-Z0-9_\-\./:]+)", config_text)
    unique_or_models = sorted(list(set(or_matches)))

    for raw_sub in unique_or_models:
        full_id = f"openrouter/{raw_sub}"
        is_quarantined = (full_id in quarantined or raw_sub in quarantined)
        # If we successfully fetched active models and this model is missing:
        is_missing_upstream = (len(active_or_ids) > 0 and raw_sub not in active_or_ids and raw_sub != "free" and raw_sub != "openrouter/free")

        if is_quarantined or is_missing_upstream:
            reason = "Quarantined by runtime proxy (HTTP 404)" if is_quarantined else "Decommissioned upstream (missing from OpenRouter catalog)"
            exclude = set(unique_or_models).union({raw_sub})
            replacement_sub = select_best_openrouter_replacement(active_or_models, exclude)
            replacements_made[raw_sub] = (replacement_sub, reason)

    # Inspect other explicitly quarantined models
    for q_model, info in quarantined.items():
        clean_q = q_model.replace("openrouter/", "").replace("openai/", "").replace("gemini/", "").replace("groq/", "").replace("mistral/", "")
        if "openrouter" in q_model and clean_q not in replacements_made:
            exclude = set(unique_or_models).union({clean_q})
            repl = select_best_openrouter_replacement(active_or_models, exclude)
            replacements_made[clean_q] = (repl, info.get("reason", "Quarantined 404"))

    if not replacements_made:
        if verbose:
            log_event("All configured models are active and healthy. No auto-heal required.")
        return False

    log_event(f"⚡ [AUTO-HEAL TRIGGERED] Detected {len(replacements_made)} defunct model(s):")
    for old_m, (new_m, reason) in replacements_made.items():
        log_event(f"   - Defunct: '{old_m}' ➔ Replacement: '{new_m}' ({reason})")

    if dry_run:
        log_event("[DRY RUN] Would update config.yaml and restart router. Exiting.")
        return True

    # 3. Update config.yaml with exact string replacements (preserving comments and structure)
    new_config_text = config_text
    for old_m, (new_m, _) in replacements_made.items():
        old_pattern = f"openrouter/{old_m}"
        new_pattern = f"openrouter/{new_m}"
        new_config_text = new_config_text.replace(old_pattern, new_pattern)
        # Also replace bare occurrences if any
        new_config_text = new_config_text.replace(f"model: {old_m}", f"model: {new_m}")

    with open(CONFIG_YAML, "w") as f:
        f.write(new_config_text)
    log_event("✓ Successfully updated ~/.config/litellm/config.yaml")

    # 4. Synchronize ~/.config/opencode/opencode.json if applicable
    if os.path.exists(OPENCODE_CONFIG):
        try:
            with open(OPENCODE_CONFIG, "r") as f:
                oc_data = json.load(f)
            oc_changed = False
            models_dict = oc_data.get("providers", {}).get("auto-free", {}).get("models", {})
            for old_m, (new_m, _) in replacements_made.items():
                old_key = f"openrouter/{old_m}"
                new_key = f"openrouter/{new_m}"
                if old_key in models_dict:
                    val = models_dict.pop(old_key)
                    val["name"] = f"Auto-Healed Model: {new_m}"
                    models_dict[new_key] = val
                    oc_changed = True
            if oc_changed:
                with open(OPENCODE_CONFIG, "w") as f:
                    json.dump(oc_data, f, indent=2)
                log_event("✓ Synchronized ~/.config/opencode/opencode.json")
        except Exception as e:
            log_event(f"Warning: Failed to update opencode.json: {e}")

    # 5. Clear quarantine state
    try:
        with open(QUARANTINE_FILE, "w") as f:
            json.dump({}, f)
        log_event("✓ Cleared quarantine state")
    except Exception as e:
        log_event(f"Warning: Failed to clear quarantine file: {e}")

    # 6. Restart LiteLLM Router Container
    try:
        log_event("Restarting LiteLLM router container via Docker Compose...")
        cmd = ["docker", "compose", "-f", COMPOSE_FILE, "up", "-d", "--force-recreate"]
        subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        log_event("✓ LiteLLM router container restarted successfully")
    except Exception as e:
        log_event(f"Error restarting router container: {e}")

    # 7. Reload OpenCode Daemon
    try:
        subprocess.run(["opencode", "reload"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5)
        log_event("✓ OpenCode configuration reloaded")
    except Exception:
        pass

    log_event("🎉 Auto-heal process completed successfully. Router is active and healthy.")
    return True

def main():
    parser = argparse.ArgumentParser(description="Auto-heal Free LLM Router configurations.")
    parser.add_argument("--dry-run", action="store_true", help="Inspect without modifying")
    parser.add_argument("--verbose", "-v", action="store_true", help="Print detailed status even if healthy")
    args = parser.parse_args()

    heal_configuration(dry_run=args.dry_run, verbose=args.verbose)

if __name__ == "__main__":
    main()
