# GEMINI.md — Free LLM Router & OpenCode Agent Manual

> **Workspace**: `/home/michiel/ai`  
> **Primary Components**: OpenCode CLI (v2.2.2) + LiteLLM Docker Compose Router (:5111)  
> **Virtual Model**: `auto-free/free-coder` (aliased to `free-coder`)

---

## 1. System Architecture

This repository hosts a multi-provider, zero-cost AI coding hub. OpenCode connects to a locally managed LiteLLM proxy running in Docker Compose on port `5111`. The proxy orchestrates 6 strong free-tier LLM deployments with `least-busy` concurrency-aware load balancing, 3 hierarchical priority tiers, automated 429 rate-limit cooldowns, dead-model auto-quarantine, output token clamping, and in-flight context window compaction.

```mermaid
flowchart TD
    User["Developer / Agent"] -->|opencode (Parallel Sessions)| OC["OpenCode CLI"]
    OC -->|Default: 'auto-free/free-coder'| LiteLLM["LiteLLM Router (Docker :5111)"]

    subgraph RouterCore ["Router Intelligence & Safety Layer"]
        LiteLLM --> Compactor["Context Compactor (In-Flight Summarizer)"]
        Compactor --> Clamper["Output Token Clamper (4096 / 16K safety)"]
        Clamper --> Balancer["Multi-Session Least-Busy Balancer + 120s Cooldown"]
        Balancer --> ToolGuard["Tool Integrity Guard (execute->shell normalization)"]
    end

    subgraph Deployments ["Hierarchical 3-Tier Failover & Concurrency Pool"]
        ToolGuard --> Frontier["Tier 1 (order: 1): Frontier Models (Codestral 256K, Gemini 3.8 Flash 1M, Groq GPT-OSS 120B, Nemotron 550B, Llama 3.3 70B)"]
        ToolGuard --> Coding["Tier 2 (order: 2): Dedicated Coding & Math (Command R+, Groq Qwen 27B, Nemotron Lightning, North Mini Code, DeepSeek V4)"]
        ToolGuard --> Fallbacks["Tier 3 (order: 3): Resilient Fallbacks (Gemini 3.5 Flash/Lite, Kilo, Aion, GLM 4.7, Requesty)"]
    end
```

---

## 2. Active Free Model Roster (Ranked by Intelligence)

All models are accessed via free-tier keys loaded from `~/notepad/free-llms/.env`. The router automatically picks the fastest healthy candidate from models with ample context windows (32K to 1M+):

| # | Provider | Model ID | Intelligence & Context Strengths |
| :-: | :--- | :--- | :--- |
| **1** | **Mistral AI** | `mistral/codestral-latest` | Premier 256K dedicated code model; Fill-In-The-Middle (FIM) & SOTA code benchmarks |
| **2** | **Google Gemini** | `gemini/gemini-3.8-flash` | Frontier multimodal reasoning with 1,000,000+ token context window |
| **3** | **OpenRouter** | `openrouter/nvidia/nemotron-3-ultra-550b-a55b:free` | **550 Billion parameter** ultra reasoning frontier model; 1,000,000 context |
| **4** | **Groq** | `groq/openai/gpt-oss-120b` | 120B parameter reasoning model running at ultra-low latency (~200ms on Groq LPUs; 128K context) |
| **5** | **Cloudflare** | `openai/@cf/meta/llama-3.3-70b-instruct-fp8-fast` | 70B parameter frontier open-weights model; 128K context (~500ms via Workers AI) |
| **6** | **Cohere** | `openai/command-r-plus-08-2024` | 104B parameter frontier Command R+ tool model; 128K context (clamped to 4096 output) |
| **7** | **Groq** | `groq/qwen/qwen3.8-27b` | Top-tier algorithmic coding and math reasoning (clamped to 16K output; dynamic bypass on >22K chars due to 7K TPM limit) |
| **8** | **OpenRouter** | `openrouter/nvidia/nemotron-3.5-lightning:free` | 1,000,000 context fast reasoning model |
| **9** | **OpenRouter** | `openrouter/cohere/north-mini-code:free` | Dedicated 256K context free coding model with reasoning support |
| **10** | **Kilo Code** | `openai/kilo-auto/free` | Reflection Beam & community free coding models (32K context) |
| **11** | **Gonka Broker**| `openai/deepseek-ai/DeepSeek-V4-Flash-0731` | 400K context DeepSeek V4 reasoning (~170ms) |
| **12** | **Google Gemini** | `gemini/gemini-3.5-flash` | Secondary 1,000,000 context frontier fallback |
| **13** | **Google Gemini** | `gemini/gemini-3.5-flash-lite` | Tertiary high-availability context fallback |
| **14** | **Aion Labs** | `openai/aion-labs/aion-2.0` | 128K context reasoning |
| **15** | **Zhipu AI** | `openai/glm-4.7-flash` | 128K context GLM-4.7 Flash reasoning |
| **16** | **Requesty** | `openai/policy/pennytree-free` | Zero-cost policy reasoning model (32K context) |

*(Note: Unvetted wildcards like `openrouter/openrouter/free` and 8K-context models like NVIDIA NIM `gpt-oss-20b` are excluded from the main coding pool to ensure agents never hit premature context ceilings or hallucinate non-existent tools).*

---

## 3. Dynamic Output Token Clamping

### Problem
Coding agents (including OpenCode) default to sending `max_tokens: 32000` (or `max_completion_tokens: 32000`). Strict providers like Cohere enforce a hard output ceiling of 4096 tokens, failing with `400 BadRequestError: max tokens must be less than or equal to 4096`.

### Solution
In [`~/.config/litellm/clean_request.py`](file:///home/michiel/.config/litellm/clean_request.py), `async_pre_call_deployment_hook` inspects each deployment right before sending the request:
- If a target model has an output ceiling (e.g., 4096 for Cohere, 16384 for Groq Qwen), `max_tokens` is transparently clamped.
- Frontier models with large output capacities (Gemini Flash, Codestral) retain their full 32K+ generation capacity.
- The failure hook dynamically learns and caches output limits from any unrecognized provider errors at runtime.

---

## 4. Autonomous Context Compactor & Dynamic Headroom Routing

### 1. In-Flight Autonomous Compactor (`clean_request.py`)
- Automatically engages in `async_pre_call_hook` when conversation history exceeds **180,000 characters** (~45,000 tokens).
- **Lossless Priority**:
  - Strictly preserves **System Instructions** (`messages[0]`).
  - Slices cleanly at **User Turn boundaries** so assistant tool-call / tool-response pairs are never severed or broken.
  - Summarizes and condenses older middle turns and bulky command outputs into a structured executive markdown summary block:
    ```markdown
    ### [AUTOMATED CONTEXT COMPACTION SUMMARY]
    - USER: Requested LiteLLM setup and Docker configuration...
    - ASSISTANT: Initialized docker-compose, verified ports...
    ### [END CONTEXT COMPACTION SUMMARY]
    ```
- **Context-Aware Headroom Routing**:
  - Automatically bypasses models with low rate-limits or small context as conversation grows:
    - >22,000 chars: Excludes `groq/qwen/qwen3.8-27b` (protecting against Groq's 7,000 TPM limit).
    - >100,000 chars: Excludes 32K/65K models (`kilo-auto`, `pennytree`, `north-mini-code`), routing exclusively to 128K-1M frontier giants (Codestral 256K, Gemini 1M, Nemotron 1M, DeepSeek 400K, Llama 3.3 128K, GPT-OSS 120B 128K).
- Configured with `ContextWindowExceededErrorRetries: 3` in `retry_policy`.

### 2. Standalone Summarization CLI (`free-ai summarize`)
For compacting files, transcripts, or long logs on demand:
```bash
# Summarize a file or transcript
free-ai summarize path/to/large_transcript.txt

# Summarize from stdin pipe
cat debug.log | free-ai summarize

# Save directly to a summary markdown file
free-ai summarize --save context_summary.md < transcript.txt
```
Powered by [`~/.local/bin/summarize-context.py`](file:///home/michiel/.local/bin/summarize-context.py), routed through the fastest available frontier model in ~2 seconds.

---

## 5. Strict Message Sanitization & Tool Call Integrity Guard

### Problem
1. When prior turns were generated by reasoning models (DeepSeek, Nemotron, Kilo Code, Groq Qwen), assistant responses contain vendor-specific keys like `reasoning_content`, `reasoning_details`, or `provider_specific_fields`. When retransmitted in conversation history, strict non-reasoning providers fail (`400/422`).
2. If internal thoughts (e.g. "I cannot run audio processing in this environment") leak into the conversation history, subsequent models inherit that negative bias and refuse tool execution.
3. If conversation history is compacted or sliced midway through a tool execution turn, orphaned `role: tool` messages break strict OpenAI, Mistral, and Gemini schemas with:
   - `400 BadRequestError: Unexpected tool call id ... in tool results`
   - `Missing corresponding tool call for tool response message`

### Solution
In [`clean_request.py`](file:///home/michiel/.config/litellm/clean_request.py):
- `clean_chat_message`: Sanitizes every message to strict OpenAI schema, stripping non-standard reasoning blocks and rescuing thought text into `content` **only** if the message would otherwise be completely empty.
- `validate_and_repair_tool_history`: Enforces strict OpenAI tool-call ordering. Safely converts any orphaned `tool` results into user messages, and ensures every assistant `tool_calls` has its required responses.
- `Tool Hallucination Guard`: Automatically normalizes hallucinated tool calls (e.g. models calling `execute`, `bash`, `sh`, `run_command` with bash commands) into OpenCode's native `shell` tool (`command: "..."`). Also cleans JavaScript SyntaxError remnants from message history.
- `Transient Error Cooldown Engine`: Automatically detects transient 429 (RateLimit), 503 (UNAVAILABLE/High Demand), and 408 (Timeout) spikes, placing affected deployments on a 180s cooldown to prevent cascading mid-stream failures in coding agents.
- Router timeout is set to 60s to prevent premature read timeouts on heavy fallback payloads.

---

## 6. Autonomous 24/7 Self-Healing Watchdog

When upstream providers retire or alter free models (e.g. HTTP 404):
1. **Tier 1 (Instant In-Flight Failover)**: `NotFoundErrorRetries: 3` ensures the active OpenCode prompt switches to another model in milliseconds without crashing.
2. **Tier 2 (Real-Time In-Memory Quarantine)**: `clean_request.py` quarantines defunct models in `~/.config/litellm/state/quarantined_models.json` and excludes them from future routing.
3. **Tier 3 (Catalog Reconciler & Systemd Watchdog)**:
   - Systemd user path unit `auto-heal-llms.path` triggers instantly upon quarantine modification.
   - Systemd user timer `auto-heal-llms.timer` runs every 30 minutes to check provider catalogs.
   - Script [`~/.local/bin/auto-heal-llms.py`](file:///home/michiel/.local/bin/auto-heal-llms.py) queries provider APIs, selects the highest-ranking replacement, updates `config.yaml` and `opencode.json`, and reloads the stack automatically.

---

## 7. Multi-Session Concurrency & Parallel Execution

The router and OpenCode are fully optimized to run **multiple parallel sessions simultaneously without cross-talk, resource contention, or rate-limit lockouts**:

### 1. In-Flight `least-busy` Load Balancing
- Instead of dumping all requests into the single fastest endpoint (which causes 429 rate limits when 2+ sessions run simultaneously), LiteLLM uses `routing_strategy: least-busy`.
- When Session 1 is generating code with Codestral, its in-flight counter increments (`active_calls = 1`).
- When Session 2 sends a concurrent prompt, the router detects Codestral is active and immediately dispatches Session 2 to another idle Tier 1 model (e.g. Gemini 3.8 Flash, Groq GPT-OSS 120B, Cloudflare Llama 3.3, or Nemotron 550B).
- Both sessions generate code simultaneously in parallel at peak throughput.

### 2. Hierarchical Priority Tiering (`order: 1 / 2 / 3`)
- Deployments are partitioned into strict priority orders:
  - **Tier 1 (`order: 1`)**: Frontier models (Codestral, Gemini 3.8 Flash, Groq 120B, Nemotron 550B, Llama 3.3 70B).
  - **Tier 2 (`order: 2`)**: Dedicated coding models (Command R+, Groq Qwen 27B, Cohere North Mini Code, Nemotron Lightning, DeepSeek V4).
  - **Tier 3 (`order: 3`)**: High-availability fallbacks (Gemini 3.5 Flash/Lite, Kilo Code, Aion, GLM 4.7 Flash, Requesty).
- The router always balances among Tier 1 endpoints first. If all Tier 1 models are occupied or on transient cooldown, traffic spills over to Tier 2, and then Tier 3.

### 3. Concurrency Protection (`max_parallel_requests` & `rpm`)
- Strict providers have explicit concurrency ceilings (e.g. Groq: 2, Mistral: 2, Gemini: 3, Cloudflare: 4).
- When a provider reaches its concurrency limit, additional parallel requests are dynamically redirected to alternative providers rather than failing.

### 4. OpenCode Parallel Workflows
- **Separate Projects**: Running `opencode` in different project directories connects to the daemon with completely independent session IDs, histories, and git watchers.
- **Same Project Concurrent Sessions**: To run two independent sessions within the same project directory, pass `--session ses_<unique_name>` (e.g., `opencode --session ses_refactor`), or run in standalone mode with `free-ai standalone` / `opencode --standalone`.

### 5. Multi-Session Logging & Real-Time Observability
- All completions across all concurrent sessions are recorded in `/app/state/recent_completions.log`.
- Run `free-ai last` to see the last routed model and the recent parallel multi-session queue.
- Run `free-ai watch` to stream live multi-session routing decisions as both sessions generate responses.

---

## 8. Multi-User Architecture & System-Wide Sharing

OpenCode and `free-ai` are configured for multi-user access across all accounts on this Ubuntu system:
- **Centralized Proxy**: The LiteLLM Docker container (`free-llm-router`) listens on port `5111` (`127.0.0.1:5111`), allowing any local user to utilize the 18 free-tier LLM failover pool without exposing raw provider API keys.
- **Independent Daemon Ports**: OpenCode launches a background service per user. To prevent daemon port collisions (default `49374`), ports are deterministically partitioned by UID (`48000 + UID`) in `~/.config/opencode/service.json` (e.g. `michiel`: 49374/49000, `pennytree-dev`: 49375, `sprintforge-dev`: 49376).
- **Installed Accounts**: `pennytree-dev` and `sprintforge-dev` have `opencode`, `free-ai`, and `summarize-context.py` ready in their `$HOME/.local/bin` and configured with `~/.config/opencode/opencode.json`.
- **System-Wide Installer**: Running `sudo /home/michiel/ai/install-systemwide.sh` installs the launcher to `/usr/local/bin/opencode` (with automatic UID port allocation), `/usr/local/bin/free-ai`, `/etc/opencode/opencode.json` (global config), and `/etc/profile.d/free-ai.sh` for universal availability across all present and future users.

---

## 9. JetBrains Junie CLI Integration

JetBrains Junie (`~/.local/bin/junie`) connects directly to the FreeAIRouter LiteLLM proxy via its BYOK (Bring Your Own Key) LiteLLM provider:

### Configuration (`~/.junie/config.json`)
```json
{
  "provider": "litellm",
  "model": "gemini-3.8-flash",
  "byok": {
    "litellm": {
      "baseUrl": "http://127.0.0.1:5111",
      "apiKey": "sk-free-llm"
    }
  }
}
```

### Junie-Specific Adaptations
1. **Model Catalog Aliasing**: Junie queries `/v1/model/info` and validates model IDs against an internal catalog enum. In `config.yaml`, `router_settings.model_group_alias` and `litellm_settings.model_alias` expose aliases (`gemini-3.8-flash`, `gemini-3.5-flash-lite`, `gpt`, `claude-sonnet-4-6`) that map directly to the `free-coder` load-balanced pool.
2. **Dynamic Tool Normalization**:
   - OpenCode registers its terminal execution tool as `shell`.
   - Junie registers its terminal execution tool as `bash`.
   - In `clean_request.py`, `get_target_shell_tool` inspects the client's registered tools in the request:
     - If the client registered `bash` and not `shell` (Junie), any hallucinated or mismatched tool calls (`shell`, `execute`, `sh`, `terminal`, `run_command`) normalize to `bash`.
     - If the client registered `shell` (OpenCode), tool calls normalize to `shell`.
   - Tool arguments remain standard (`{"command": "..."}`), enabling both Junie and OpenCode to operate concurrently without conflicts.
4. **Response Repair (Junie strict parsing)**: `async_post_call_success_hook` repairs malformed tool calls (empty `tool_calls`, missing IDs, invalid-JSON arguments, mismatched `finish_reason`, empty content), and the streaming hook strips non-standard chunk fields (`stop_reason`) and fills missing `type`/`index`, so Junie never reports "unexpected response format".

---

## 10. Essential Commands Reference

| Command | Action |
| :--- | :--- |
| `opencode` | Launch interactive OpenCode TUI with auto-failover |
| `free-ai standalone` | Launch OpenCode in isolated `--standalone` mode (private server) |
| `junie` | Launch JetBrains Junie CLI with FreeAIRouter |
| `free-ai status` | Show status of OpenCode, LiteLLM container, and provider keys |
| `free-ai test` | Run live completion tests across all configured providers |
| `free-ai last` | Show the exact upstream model that handled your last prompt and recent queue |
| `free-ai watch` | Stream real-time routing decisions as you code across all sessions |
| `free-ai summarize <file>` | Generate an executive technical summary using a frontier model |
| `free-ai auto-heal` | Manually run the self-healing watchdog and inspect logs |
| `free-ai router restart` | Restart the Docker Compose router stack |
| `free-ai router logs` | Tail recent LiteLLM Docker container logs |

---

## 11. Guidelines for AI Agents Working Here

1. **Environment Secrets**: Never commit or expose `~/notepad/free-llms/.env`. Use `os.environ` or Docker Compose environment mapping.
2. **Keep Documentation in Sync**: When modifying models, routes, or hooks, update [`SETUP.md`](file:///home/michiel/ai/SETUP.md), [`GEMINI.md`](file:///home/michiel/ai/GEMINI.md), and [~/.config/opencode/opencode.json](file:///home/michiel/.config/opencode/opencode.json).
3. **Verify After Changes**: After updating router configuration or Python hooks, execute `free-ai router restart` followed by a live test completion.
4. **Preserve Clamping & Compaction**: Maintain the pre-call deployment hooks in [`clean_request.py`](file:///home/michiel/.config/litellm/clean_request.py) so downstream coding tools never trigger 400 token overflows or context blowups.
5. **Always Commit & Push to GitHub**: Whenever you make fixes, enhancements, or configuration updates in this workspace, always commit your changes with a clear message and push them to GitHub (`git push origin main`) so the remote repository is always up to date.

---

## 12. Pool Pruning (agentic quality)

The `free-coder` pool (shared by OpenCode and Junie) contains mainly models that handle agentic tool use and exact-match edits reliably: Codestral, Gemini 3.8 Flash, GPT-OSS 120B, Nemotron 3 Ultra 550B (tier 1), DeepSeek V4 Flash (tier 2), Gemini 3.5 Flash and Llama 3.3 70B (tier 3 last-resort fallbacks). Removed from the pool (still selectable directly by model name): Command R+ 2024, Qwen 27B, North Mini Code, Nemotron Lightning, Gemini 3.5 Flash Lite, Kilo, Aion, GLM 4.7 Flash, Pennytree — they hallucinated edit patterns and tool calls. The roster table in section 2 lists the original 16; only the six above are in the pool.
