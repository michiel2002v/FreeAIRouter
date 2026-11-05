"""
clean_request.py
LiteLLM proxy custom callback to:
1. Sanitize request parameters (remove root/tool-level 'strict' rejected by Groq/Mistral/Gonka).
2. Dynamic Output Token Clamping (pre_call_deployment_hook): Automatically detect each
   deployment's maximum output token ceiling (e.g. Cohere: 4096, Groq Qwen: 16384) and clamp
   excessive incoming max_tokens (like 32000 from coding agents) to prevent HTTP 400 Bad Request errors.
3. Autonomous Context Compactor & Summarization Engine:
   Monitors conversation token and character footprint. When conversation history grows beyond
   safe thresholds (or approaches a deployment's context limit), it automatically condenses
   older dialogue turns and bulky tool outputs into an executive summary block while preserving
   the system instructions and recent conversation turns intact.
4. Strict Chat Message Sanitizer:
   Reasoning models (like Nemotron, Qwen, DeepSeek) return non-standard keys like 'reasoning_content',
   'reasoning_details', and 'provider_specific_fields'. When coding agents re-send these in chat history,
   strict non-reasoning providers (Mistral, Cohere) reject them with:
     - "extra_forbidden: Extra inputs are not permitted" (Mistral HTTP 422)
     - "thinking content is not allowed in chat history for non-reasoning models" (Cohere HTTP 400)
   The sanitizer cleanses all message objects to strict OpenAI schema across all turns and fallbacks.
5. Log and export the exact routed upstream model so users can see which model answered in real time.
6. Auto-Quarantine: Automatically detect 404/NotFoundError/model retirement on upstream providers,
   quarantine the broken model in real-time, filter it out of candidate deployments, and notify
   the host auto-healer daemon to permanently update configs.
"""

import json
import os
import re
import time
from typing import Any, Mapping
import litellm
from litellm.integrations.custom_logger import CustomLogger
from litellm.types.utils import CallTypes

QUARANTINE_FILE = "/app/state/quarantined_models.json"
LAST_MODEL_FILE = "/tmp/litellm_last_model.txt"

# Known model output token limits for providers/models that don't auto-truncate or reject on overflow
KNOWN_MODEL_OUTPUT_LIMITS: dict[str, int] = {
    "command-r-plus": 4096,
    "command-r": 4096,
    "north-mini-code": 4096,
    "qwen3.8-27b": 16384,
    "qwen/qwen": 16384,
    "gpt-oss-20b": 8192,
    "DeepSeek-V4-Flash": 8192,
    "aion-2.0": 8192,
    "glm-4.7-flash": 8192,
    "pennytree": 8192,
    "llama-3.3-70b": 8192,
}

# Dynamic cache learned at runtime from any provider output token limit errors
DYNAMIC_MODEL_LIMITS: dict[str, int] = {}

# Context Compaction Thresholds
COMPACTION_CHAR_THRESHOLD = 180_000  # ~45,000 tokens; safely accommodated by 128K-1M frontier models
MAX_TOOL_OUTPUT_CHARS = 1000       # Historical tool outputs truncated to preserve context

# Standard permitted keys in OpenAI ChatCompletion messages
ALLOWED_MESSAGE_KEYS = {"role", "content", "name", "tool_calls", "tool_call_id"}

SHELL_COMMAND_PREFIXES = (
    "cd ", "git ", "rm ", "mkdir ", "ls ", "make ", "docker ", "python", "pip",
    "npm ", "pnpm ", "curl ", "wget ", "cat ", "grep ", "find ", "echo ", "touch ",
    "source ", "export ", "tar ", "unzip ", "bash ", "sh "
)


def is_shell_command(cmd_str: str) -> bool:
    """Detect if a string is a bash shell command rather than JavaScript code."""
    if not isinstance(cmd_str, str):
        return False
    s = cmd_str.strip()
    if any(s.startswith(p) for p in SHELL_COMMAND_PREFIXES):
        return True
    if " && " in s or " || " in s or " | " in s or s.startswith("./") or s.startswith("/"):
        return True
    return False


def clean_chat_message(msg: dict) -> dict:
    """
    Sanitize an individual message to strict OpenAI specification:
    - Retains only allowed keys: role, content, name, tool_calls, tool_call_id
    - Strips 'reasoning_content', 'reasoning_details', 'thinking', 'thought',
      'provider_specific_fields', 'refusal', and any vendor-specific metadata
    - If content is empty but reasoning_content exists, rescues the text into content
    - Normalizes list content (converts thinking parts to [Thought: ...])
    - Cleans tool_calls (strips 'strict')
    """
    if not isinstance(msg, dict):
        return msg

    clean: dict[str, Any] = {
        "role": msg.get("role", "user")
    }

    # Rescue reasoning_content if content is missing/empty
    content = msg.get("content")
    reasoning = msg.get("reasoning_content") or msg.get("reasoning") or msg.get("thinking")
    if (content is None or str(content).strip() == "") and reasoning:
        content = str(reasoning)

    # Normalize content
    if isinstance(content, list):
        text_parts = []
        thought_parts = []
        has_multimodal = False
        for part in content:
            if isinstance(part, dict):
                p_type = part.get("type")
                if p_type == "text":
                    text_parts.append(part.get("text", ""))
                elif p_type in ("thinking", "reasoning"):
                    thought = part.get("thinking") or part.get("text") or part.get("reasoning") or ""
                    if thought:
                        thought_parts.append(thought)
                elif "text" in part:
                    text_parts.append(part["text"])
                elif "image_url" in part:
                    has_multimodal = True
            elif isinstance(part, str):
                text_parts.append(part)

        if not has_multimodal:
            joined = "\n".join(filter(None, text_parts)).strip()
            # Only rescue thought if there is NO text output at all
            if not joined and thought_parts:
                joined = "\n".join(thought_parts).strip()
            clean["content"] = joined if joined else " "
        else:
            clean_list = []
            has_valid_text = any(
                isinstance(p, dict) and p.get("type") == "text" and bool(p.get("text", "").strip())
                for p in content
            )
            for part in content:
                if isinstance(part, dict) and part.get("type") in ("thinking", "reasoning"):
                    # Only rescue thought into multimodal payload if there is no other text
                    if not has_valid_text:
                        thought = part.get("thinking") or part.get("text") or part.get("reasoning") or ""
                        if thought:
                            clean_list.append({"type": "text", "text": thought})
                else:
                    clean_list.append(part)
            clean["content"] = clean_list
    elif content is None or str(content).strip() == "":
        clean["content"] = " " if not msg.get("tool_calls") else ""
    else:
        clean["content"] = str(content)

    # Sanitize tool responses that contain past JavaScript syntax errors from hallucinated execute
    if msg.get("role") == "tool":
        c_str = str(clean.get("content", ""))
        if "Code must be plain JavaScript" in c_str or "Unknown tool 'shell'" in c_str:
            clean["content"] = "[Tool execution completed]"

    # Sanitize and normalize tool_calls
    tool_calls = msg.get("tool_calls")
    if tool_calls and isinstance(tool_calls, list) and len(tool_calls) > 0:
        clean_tools = []
        for tc in tool_calls:
            if isinstance(tc, dict):
                t = dict(tc)
                t.pop("strict", None)
                fn = t.get("function")
                if isinstance(fn, dict):
                    fn = dict(fn)
                    fn.pop("strict", None)
                    fn_name = fn.get("name", "")
                    # Normalize hallucinated execute / bash / terminal tools to shell
                    if fn_name in ("execute", "bash", "sh", "terminal", "run_command"):
                        args_raw = fn.get("arguments", "{}")
                        try:
                            args_obj = json.loads(args_raw) if isinstance(args_raw, str) else args_raw
                        except Exception:
                            args_obj = {}
                        if isinstance(args_obj, dict):
                            cmd = args_obj.get("code") or args_obj.get("command") or args_obj.get("CommandLine") or ""
                            if cmd and is_shell_command(str(cmd)):
                                fn["name"] = "shell"
                                fn["arguments"] = json.dumps({"command": str(cmd)})
                    t["function"] = fn
                elif t.get("name") in ("execute", "bash", "sh", "terminal", "run_command"):
                    t_name = t.get("name", "")
                    args_raw = t.get("arguments") or t.get("input") or {}
                    try:
                        args_obj = json.loads(args_raw) if isinstance(args_raw, str) else args_raw
                    except Exception:
                        args_obj = {}
                    if isinstance(args_obj, dict):
                        cmd = args_obj.get("code") or args_obj.get("command") or ""
                        if cmd and is_shell_command(str(cmd)):
                            t["name"] = "shell"
                            t["arguments"] = json.dumps({"command": str(cmd)})
                clean_tools.append(t)
        if clean_tools:
            clean["tool_calls"] = clean_tools

    if msg.get("tool_call_id"):
        clean["tool_call_id"] = msg["tool_call_id"]

    if msg.get("name"):
        clean["name"] = msg["name"]

    return clean


def validate_and_repair_tool_history(messages: list) -> list:
    """
    Ensure strict OpenAI/Mistral/Gemini tool-call ordering:
    1. A 'tool' message MUST directly follow either an assistant message with matching tool_call_id,
       or another valid 'tool' message from the same preceding assistant batch.
    2. Any orphaned 'tool' message (whose parent assistant tool_call was lost, summarized away,
       or sliced by context compaction) is safely converted to a user message:
       {'role': 'user', 'content': f"[Previous Tool Result ({name})]:\n{content}"}
       This completely eliminates HTTP 400 schema errors:
         - Mistral: "Unexpected tool call id ... in tool results"
         - Gemini: "Missing corresponding tool call for tool response message"
    3. Any assistant message with 'tool_calls' that is NOT followed by tool responses before the
       next user or assistant turn has clean synthetic tool responses injected so the parent
       tool call is never left dangling.
    """
    if not isinstance(messages, list):
        return messages

    repaired = []
    pending_tool_call_ids = set()
    last_assistant_idx = None

    for m in messages:
        if not isinstance(m, dict):
            continue
        role = m.get("role")

        if role == "assistant":
            # If previous assistant had unanswered tool calls, close them out first
            if pending_tool_call_ids and last_assistant_idx is not None:
                for missing_id in list(pending_tool_call_ids):
                    repaired.append({
                        "role": "tool",
                        "tool_call_id": missing_id,
                        "content": "[Tool execution completed]"
                    })
                pending_tool_call_ids.clear()

            tc_list = m.get("tool_calls") or []
            pending_tool_call_ids = {
                tc.get("id") for tc in tc_list if isinstance(tc, dict) and tc.get("id")
            }
            last_assistant_idx = len(repaired)
            repaired.append(m)

        elif role == "tool":
            call_id = m.get("tool_call_id")
            if call_id and call_id in pending_tool_call_ids:
                pending_tool_call_ids.discard(call_id)
                repaired.append(m)
            else:
                # Orphaned tool response: convert to harmless user message retaining full content
                t_name = m.get("name") or "tool"
                content_str = str(m.get("content", ""))
                repaired.append({
                    "role": "user",
                    "content": f"[Previous Tool Result ({t_name})]:\n{content_str}"
                })

        else:
            # role is 'user', 'system', or 'developer'
            if pending_tool_call_ids and last_assistant_idx is not None:
                for missing_id in list(pending_tool_call_ids):
                    repaired.append({
                        "role": "tool",
                        "tool_call_id": missing_id,
                        "content": "[Tool execution completed]"
                    })
                pending_tool_call_ids.clear()
            repaired.append(m)

    return repaired


def sanitize_messages_list(messages: list) -> list:
    """Sanitize and repair all messages in a conversation history list."""
    if not isinstance(messages, list):
        return messages
    cleaned = [clean_chat_message(m) for m in messages if isinstance(m, dict)]
    return validate_and_repair_tool_history(cleaned)


def get_model_max_output_tokens(model_name: str, model_info: dict | None = None) -> int | None:
    """Resolve the maximum allowed output generation tokens for a given model."""
    if not model_name:
        return None

    # 1. Check explicit model_info configured in config.yaml
    if isinstance(model_info, dict):
        limit = model_info.get("max_output_tokens") or model_info.get("max_tokens")
        if isinstance(limit, int) and limit > 0:
            return limit

    # 2. Check dynamic cache (learned from upstream error responses)
    if model_name in DYNAMIC_MODEL_LIMITS:
        return DYNAMIC_MODEL_LIMITS[model_name]

    # 3. Check known substring patterns
    name_lower = model_name.lower()
    for pattern, limit in KNOWN_MODEL_OUTPUT_LIMITS.items():
        if pattern.lower() in name_lower:
            return limit

    # 4. Ask litellm registry
    try:
        limit = litellm.get_max_tokens(model_name)
        if isinstance(limit, int) and limit > 0:
            return limit
    except Exception:
        pass

    return None


def extract_token_limit_from_error(exc: Exception) -> int | None:
    """Extract maximum output token limit from provider error strings if present."""
    if exc is None:
        return None
    msg = str(exc)
    patterns = [
        r"max tokens must be less than or equal to (\d+)",
        r"max_tokens.*must be less than or equal to.*?(\d+)",
        r"maximum value for.*max_tokens.*is (\d+)",
        r"max_tokens is too large.*maximum.*?(\d+)",
    ]
    for p in patterns:
        m = re.search(p, msg, re.IGNORECASE)
        if m:
            try:
                return int(m.group(1))
            except ValueError:
                pass
    return None


def is_context_window_error(exc: Exception) -> bool:
    """Check if exception indicates context window length exceeded."""
    if exc is None:
        return False
    cls_name = type(exc).__name__
    if cls_name == "ContextWindowExceededError":
        return True
    msg = str(exc).lower()
    keywords = [
        "context_length_exceeded",
        "maximum context length",
        "context window is too small",
        "prompt is too long",
        "tokens exceeds the context window",
        "too many input tokens",
    ]
    return any(k in msg for k in keywords)


def is_defunct_model_error(exc: Exception) -> bool:
    """Check if an exception indicates a model is missing, decommissioned, or 404."""
    if exc is None:
        return False
    status = getattr(exc, "status_code", None)
    if status in (404, 410):
        return True
    cls_name = type(exc).__name__
    if cls_name in ("NotFoundError", "ModelNotFoundError"):
        return True
    msg = str(exc).lower()
    keywords = [
        "no longer available",
        "no provider serves this model",
        "does not exist",
        "has been decommissioned",
        "model_not_found",
        "is not supported",
        "was not found",
    ]
    return any(k in msg for k in keywords)


def is_transient_error(exc: Exception) -> bool:
    """Check if an exception indicates a transient failure: 429 RateLimit, 503 UNAVAILABLE/High Demand, 408 Timeout."""
    if exc is None:
        return False
    status = getattr(exc, "status_code", None)
    if status in (429, 503, 408, 502, 504):
        return True
    cls_name = type(exc).__name__
    if cls_name in ("RateLimitError", "ServiceUnavailableError", "Timeout", "APIConnectionError"):
        return True
    msg = str(exc).lower()
    keywords = [
        "rate limit",
        "too many requests",
        "experiencing high demand",
        "service unavailable",
        "temporarily unavailable",
        "timed out",
        "timeout",
        "503",
        "429",
        "408",
    ]
    return any(k in msg for k in keywords)


def compact_conversation_history(messages: list, max_chars: int = COMPACTION_CHAR_THRESHOLD) -> tuple[list, bool]:
    """
    Lossless-Priority Context Compactor:
    If conversation history exceeds max_chars, condenses older middle turns
    into an executive summary block while strictly retaining system prompt
    and recent dialogue turns with full fidelity.
    """
    if not isinstance(messages, list) or len(messages) <= 6:
        return messages, False

    total_chars = sum(len(str(m.get("content", ""))) for m in messages if isinstance(m, dict))
    if total_chars <= max_chars:
        return messages, False

    # Separate system prompts (head) and dialog turns
    system_msgs = [m for m in messages[:2] if isinstance(m, dict) and m.get("role") in ("system", "developer")]
    dialog_msgs = [m for m in messages if isinstance(m, dict) and m.get("role") not in ("system", "developer")]

    if len(dialog_msgs) <= 4:
        return messages, False

    # Split into historical middle and recent tail.
    # We find a clean turn boundary starting with a 'user' message to ensure
    # we NEVER split inside an assistant tool-call / tool-response transaction.
    user_indices = [i for i, m in enumerate(dialog_msgs) if isinstance(m, dict) and m.get("role") == "user"]
    if len(user_indices) >= 2:
        split_idx = user_indices[-2] if (len(dialog_msgs) - user_indices[-2]) <= 8 else user_indices[-1]
    elif user_indices:
        split_idx = user_indices[-1]
    else:
        split_idx = max(0, len(dialog_msgs) - 4)

    # Ensure recent_tail does not begin with an orphaned tool message
    while split_idx < len(dialog_msgs) and isinstance(dialog_msgs[split_idx], dict) and dialog_msgs[split_idx].get("role") == "tool":
        split_idx += 1

    history_slice = dialog_msgs[:split_idx]
    recent_tail = dialog_msgs[split_idx:]

    summary_bullets = []
    for m in history_slice:
        role = m.get("role", "user").upper()
        content = m.get("content", "")
        if isinstance(content, list):
            text_parts = [p.get("text", "") for p in content if isinstance(p, dict) and "text" in p]
            text = " ".join(text_parts)
        else:
            text = str(content)

        # Condense bulky tool outputs / logs
        if len(text) > MAX_TOOL_OUTPUT_CHARS:
            lines = text.strip().splitlines()
            snippet = lines[0][:200] if lines else text[:200]
            text = f"{snippet}... [Output truncated: {len(lines)} lines / {len(text)} chars]"

        if len(text) > 400:
            text = text[:380] + "... [summarized]"

        summary_bullets.append(f"- **{role}**: {text}")

    condensed_text = "\n".join(summary_bullets)
    summary_user = {
        "role": "user",
        "content": (
            "### [AUTOMATED CONTEXT COMPACTION SUMMARY]\n"
            "The earlier portion of this conversation has been condensed to optimize context window:\n"
            f"{condensed_text}\n"
            "### [END CONTEXT COMPACTION SUMMARY]"
        )
    }
    summary_asst = {
        "role": "assistant",
        "content": "Acknowledged. I retain full awareness of the condensed conversation history and decisions, and will continue assisting you with the active tasks."
    }

    # Only include summary_asst if recent_tail starts with a user message so we don't produce consecutive assistant turns
    if recent_tail and isinstance(recent_tail[0], dict) and recent_tail[0].get("role") == "user":
        compacted = system_msgs + [summary_user, summary_asst] + recent_tail
    else:
        compacted = system_msgs + [summary_user] + recent_tail

    # Validate and repair tool call integrity across the compacted list
    compacted = validate_and_repair_tool_history(compacted)
    return compacted, True


class ParamSanitizer(CustomLogger):
    def __init__(self):
        super().__init__()
        self.quarantined_models: dict[str, dict] = {}
        self.transient_cooldowns: dict[str, float] = {}
        self._last_quarantine_check = 0.0
        self.load_quarantine()

    def set_transient_cooldown(self, model: str, duration: float = 180.0, reason: str = ""):
        """Temporarily bypass a deployment experiencing rate limits or capacity spikes."""
        now = time.time()
        self.transient_cooldowns[model] = now + duration
        clean_model = model.replace("openai/", "").replace("openrouter/", "").replace("gemini/", "").replace("groq/", "")
        print(f"⏳ [COOLDOWN] Put {clean_model} on {int(duration)}s transient cooldown (Reason: {reason[:60]})", flush=True)

    def is_in_transient_cooldown(self, model: str) -> bool:
        if not model or model not in self.transient_cooldowns:
            return False
        now = time.time()
        if now < self.transient_cooldowns[model]:
            return True
        del self.transient_cooldowns[model]
        return False

    def load_quarantine(self):
        now = time.time()
        # Refresh quarantine from disk at most once every 2 seconds
        if now - self._last_quarantine_check < 2.0 and self.quarantined_models:
            return
        self._last_quarantine_check = now
        if os.path.exists(QUARANTINE_FILE):
            try:
                with open(QUARANTINE_FILE, "r") as f:
                    data = json.load(f)
                    if isinstance(data, dict):
                        self.quarantined_models = data
            except Exception:
                pass

    def quarantine_model(self, model: str, reason: str):
        self.load_quarantine()
        if model not in self.quarantined_models:
            self.quarantined_models[model] = {
                "quarantined_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "reason": reason[:200]
            }
            log_line = f"🛡️ [AUTO-HEAL] Quarantined defunct model: \033[1;31m{model}\033[0m (Reason: {reason[:80]})"
            print(log_line, flush=True)
            try:
                os.makedirs(os.path.dirname(QUARANTINE_FILE), exist_ok=True)
                with open(QUARANTINE_FILE, "w") as f:
                    json.dump(self.quarantined_models, f, indent=2)
            except Exception as e:
                print(f"Error saving quarantine: {e}", flush=True)

    async def async_filter_deployments(
        self,
        model: str,
        healthy_deployments: list,
        messages: list | None = None,
        request_kwargs: dict | None = None,
        parent_otel_span: Any | None = None,
    ) -> list[dict]:
        self.load_quarantine()

        # Calculate approximate character size of conversation
        msg_chars = 0
        msgs = messages
        if msgs is None and isinstance(request_kwargs, dict):
            msgs = request_kwargs.get("messages")
        if isinstance(msgs, list):
            msg_chars = sum(len(str(m.get("content", ""))) for m in msgs if isinstance(m, dict))

        filtered = []
        for dep in healthy_deployments:
            dep_model = dep.get("litellm_params", {}).get("model") or dep.get("model")
            dep_str = str(dep_model)

            # 1. Exclude quarantined models
            if dep_str in self.quarantined_models or str(dep_model) in self.quarantined_models:
                continue

            # 1b. Exclude models on transient cooldown (e.g. 503 high demand spikes or 429 rate limits)
            if self.is_in_transient_cooldown(dep_str) or self.is_in_transient_cooldown(str(dep_model)):
                continue

            # 2. Exclude 8K models (gpt-oss-20b) if conversation exceeds 20,000 chars (~5,000 tokens)
            if "gpt-oss-20b" in dep_str and msg_chars > 20_000:
                continue

            # 3. Exclude Groq Qwen 27B if conversation exceeds its strict 7,000 TPM limit (~22,000 chars)
            if "qwen3.8-27b" in dep_str and msg_chars > 22_000:
                continue

            # 4. Exclude 32K/65K models if conversation exceeds 100,000 chars (~25,000 tokens)
            if msg_chars > 100_000 and any(k in dep_str for k in ("kilo-auto", "pennytree", "north-mini-code")):
                continue

            filtered.append(dep)

        if not filtered and healthy_deployments:
            # Fall back to healthy non-quarantined deployments if transient cooldowns excluded all
            filtered = [
                d for d in healthy_deployments
                if str(d.get("litellm_params", {}).get("model") or d.get("model")) not in self.quarantined_models
            ]

        return filtered if filtered else healthy_deployments

    async def async_pre_call_deployment_hook(
        self, kwargs: dict[str, object], call_type: CallTypes | None
    ) -> dict | None:
        """
        Runs right before dispatching the request to the selected upstream deployment.
        1. Clamps max_tokens and max_completion_tokens to what the specific model supports.
        2. Applies deployment-specific context compaction if context size exceeds model capacity.
        3. Sanitizes messages to strict OpenAI schema, stripping non-standard reasoning fields
           so strict providers (Mistral, Cohere) never fail with 400/422 schema errors.
        """
        try:
            model = kwargs.get("model")
            model_info = kwargs.get("model_info")
            info_dict = model_info if isinstance(model_info, dict) else None
            max_limit = get_model_max_output_tokens(str(model), info_dict)
            if max_limit and max_limit > 0:
                # Clamp max_tokens
                req_max = kwargs.get("max_tokens")
                if isinstance(req_max, int) and req_max > max_limit:
                    log_line = f"⚙️ [CLAMP] Clamped max_tokens for \033[1;33m{model}\033[0m: {req_max} -> {max_limit}"
                    print(log_line, flush=True)
                    kwargs["max_tokens"] = max_limit

                # Clamp max_completion_tokens (used by o1/o3/reasoning models)
                req_comp = kwargs.get("max_completion_tokens")
                if isinstance(req_comp, int) and req_comp > max_limit:
                    kwargs["max_completion_tokens"] = max_limit

            # Clean messages right before deployment dispatch (critical for fallbacks)
            messages = kwargs.get("messages")
            if isinstance(messages, list):
                # Check context window compaction against model input limit
                max_input = None
                if info_dict:
                    max_input = info_dict.get("max_input_tokens") or info_dict.get("context_window")
                if max_input and isinstance(max_input, int) and max_input > 0:
                    char_limit = max_input * 3  # safe character ceiling
                    compacted_msgs, did_compact = compact_conversation_history(messages, max_chars=char_limit)
                    if did_compact:
                        print(f"🗜️ [COMPACT] Model-specific compaction for {model} (limit: {max_input} tokens)", flush=True)
                        messages = compacted_msgs

                kwargs["messages"] = sanitize_messages_list(messages)

        except Exception as e:
            print(f"Error in pre_call_deployment_hook: {e}", flush=True)
        return kwargs

    async def async_post_call_failure_deployment_hook(
        self,
        request_data: Mapping[str, object],
        exception: Exception,
        call_type: CallTypes | None,
        fallback_depth: int | None = None,
    ) -> None:
        try:
            model = request_data.get("model") or request_data.get("litellm_params", {}).get("model")
            if model:
                model_str = str(model)
                # 1. Check if model is defunct (404/retirement)
                if is_defunct_model_error(exception):
                    self.quarantine_model(model_str, reason=str(exception))

                # 2. Check if model failed due to output token limit overflow
                limit = extract_token_limit_from_error(exception)
                if limit and limit > 0:
                    DYNAMIC_MODEL_LIMITS[model_str] = limit
                    print(
                        f"🛡️ [AUTO-HEAL] Learned output token limit for {model_str}: {limit} tokens",
                        flush=True,
                    )

                # 3. Check for context window error
                if is_context_window_error(exception):
                    print(
                        f"⚠️ [CONTEXT-OVERFLOW] Deployment {model_str} exceeded context window. Summarization engine engaged on failover.",
                        flush=True,
                    )

                # 4. Check for transient errors: 429 RateLimit, 503 UNAVAILABLE/Capacity, 408 Timeout
                if is_transient_error(exception):
                    self.set_transient_cooldown(model_str, duration=180.0, reason=str(exception))
        except Exception as e:
            print(f"Error in failure deployment hook: {e}", flush=True)

    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):
        if not isinstance(data, dict):
            return data

        # 0. Handle model aliasing: normalize 'auto-free/' prefix
        model = data.get("model")
        if isinstance(model, str) and model.startswith("auto-free/"):
            data["model"] = model[len("auto-free/"):]

        # 1. Strip top-level 'strict' parameter
        data.pop("strict", None)

        # 2. Tell LiteLLM to return the actual routed model name
        metadata = data.setdefault("metadata", {})
        if isinstance(metadata, dict):
            metadata["_complexity_router_return_raw_model_name"] = True

        # 3. Strip 'strict' from tool function declarations if present
        if "tools" in data and isinstance(data["tools"], list):
            for tool in data["tools"]:
                if isinstance(tool, dict):
                    tool.pop("strict", None)
                    if isinstance(tool.get("function"), dict):
                        tool["function"].pop("strict", None)

        # 4. Sanitize conversation history messages & apply Autonomous Compaction Engine
        messages = data.get("messages")
        if isinstance(messages, list):
            # Check if autonomous context compaction is needed
            orig_chars = sum(len(str(m.get("content", ""))) for m in messages if isinstance(m, dict))
            compacted_msgs, did_compact = compact_conversation_history(messages, max_chars=COMPACTION_CHAR_THRESHOLD)
            if did_compact:
                new_chars = sum(len(str(m.get("content", ""))) for m in compacted_msgs if isinstance(m, dict))
                tokens_saved = (orig_chars - new_chars) // 4
                print(
                    f"🗜️ [COMPACT] Summarization Engine compacted conversation: {orig_chars} -> {new_chars} chars (~{tokens_saved} tokens saved)",
                    flush=True,
                )
                messages = compacted_msgs

            # Clean and sanitize each message to strict OpenAI schema
            data["messages"] = sanitize_messages_list(messages)

        return data

    async def async_post_call_streaming_deployment_hook(
        self,
        request_data: dict,
        response_chunk: object,
        call_type: CallTypes | None,
    ) -> object | None:
        try:
            model = request_data.get("model") or request_data.get("litellm_params", {}).get("model")
            if model and hasattr(response_chunk, "model"):
                clean = str(model).replace("openai/", "").replace("openrouter/", "").replace("gemini/", "").replace("groq/", "").replace("mistral/", "")
                response_chunk.model = f"free-coder [{clean}]"
        except Exception:
            pass
        return response_chunk

    async def async_post_call_success_hook(
        self,
        data: dict,
        user_api_key_dict: Any,
        response: Any,
    ) -> Any:
        try:
            # Normalize any hallucinated execute tool calls in response choices to shell
            choices = getattr(response, "choices", None)
            if choices and isinstance(choices, list):
                for choice in choices:
                    msg = getattr(choice, "message", None)
                    if msg:
                        tc_list = getattr(msg, "tool_calls", None)
                        if tc_list and isinstance(tc_list, list):
                            for tc in tc_list:
                                fn = getattr(tc, "function", None)
                                if fn:
                                    fn_name = getattr(fn, "name", "")
                                    if fn_name in ("execute", "bash", "sh", "terminal", "run_command"):
                                        args_str = getattr(fn, "arguments", "{}")
                                        try:
                                            args_dict = json.loads(args_str) if isinstance(args_str, str) else args_str
                                        except Exception:
                                            args_dict = {}
                                        if isinstance(args_dict, dict):
                                            cmd = args_dict.get("code") or args_dict.get("command") or args_dict.get("CommandLine") or ""
                                            if cmd and is_shell_command(str(cmd)):
                                                fn.name = "shell"
                                                fn.arguments = json.dumps({"command": str(cmd)})

            hidden = getattr(response, "_hidden_params", {}) or {}
            model = hidden.get("litellm_model_name") or hidden.get("provider_response_model") or data.get("model")
            clean = str(model).replace("openai/", "").replace("openrouter/", "").replace("gemini/", "").replace("groq/", "").replace("mistral/", "")
            
            # Write last model info to file for CLI inspection
            now = time.strftime("%H:%M:%S")
            log_line = f"🤖 [ROUTER] Handled via \033[1;36m{model}\033[0m at {now}"
            print(log_line, flush=True)

            for p in [LAST_MODEL_FILE, "/app/state/litellm_last_model.txt"]:
                try:
                    with open(p, "w") as f:
                        f.write(f"{model}|{now}")
                except Exception:
                    pass

            if hasattr(response, "model"):
                response.model = f"free-coder [{clean}]"
        except Exception:
            pass
        return response

    async def async_log_success_event(self, kwargs, response_obj, start_time, end_time):
        try:
            model = kwargs.get("model") or kwargs.get("litellm_params", {}).get("model")
            now = time.strftime("%H:%M:%S")
            duration = 0
            if start_time and end_time:
                try:
                    duration = int((end_time - start_time).total_seconds() * 1000)
                except Exception:
                    duration = int((end_time - start_time) * 1000)
            log_line = f"🤖 [ROUTER] Handled via \033[1;36m{model}\033[0m ({duration}ms) at {now}"
            print(log_line, flush=True)

            for p in [LAST_MODEL_FILE, "/app/state/litellm_last_model.txt"]:
                try:
                    with open(p, "w") as f:
                        f.write(f"{model}|{now}|{duration}ms")
                except Exception:
                    pass
        except Exception:
            pass

sanitizer = ParamSanitizer()
