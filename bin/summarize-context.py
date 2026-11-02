#!/usr/bin/env python3
"""
summarize-context.py
Autonomous Summarization & Context Compaction CLI tool for Free LLM Hub.

Usage:
  cat long_transcript.txt | summarize-context.py
  summarize-context.py path/to/file.log
  summarize-context.py "Some very long context to summarize..."
  summarize-context.py --save summary.md < input.txt
"""

import sys
import os
import argparse
import urllib.request
import urllib.error
import json

ROUTER_URL = "http://127.0.0.1:5111/v1/chat/completions"
DEFAULT_MODEL = "auto-free/free-coder"

SUMMARY_PROMPT = """You are an expert software engineer and technical summarizer.
Summarize the following conversation history, log, or code transcript into a concise, high-density executive technical summary.

Your summary must extract and structure:
1. **Core Objectives & User Intent**: What is being built or solved?
2. **Key Decisions & Technical Approach**: What design or architectural choices were made?
3. **Files & Components Affected**: Which files, functions, or configurations were modified?
4. **Current State & Unresolved Tasks**: What is working, and what remains to be done?

Be strictly factual, preserve exact file paths and identifier names, and eliminate all fluff.

Content to summarize:
---
{content}
---
"""

def summarize_text(text: str, model: str = DEFAULT_MODEL) -> str:
    if not text.strip():
        return "Error: Empty input provided."

    payload = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": SUMMARY_PROMPT.format(content=text)
            }
        ],
        "temperature": 0.2,
        "max_tokens": 4096
    }

    req = urllib.request.Request(
        ROUTER_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer sk-free-llm"
        },
        method="POST"
    )

    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data["choices"][0]["message"]["content"].strip()
    except urllib.error.HTTPError as e:
        err = e.read().decode("utf-8")
        return f"Router HTTP error ({e.code}): {err}"
    except Exception as e:
        return f"Error connecting to LiteLLM router: {e}"

def main():
    parser = argparse.ArgumentParser(description="Summarize context using the Free LLM Router.")
    parser.add_argument("source", nargs="?", help="File path or text string to summarize (or read from stdin)")
    parser.add_argument("--model", "-m", default=DEFAULT_MODEL, help="Model or router group to use")
    parser.add_argument("--save", "-s", help="Save summary output to a file")
    args = parser.parse_args()

    content = ""
    if args.source:
        if os.path.exists(args.source):
            with open(args.source, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
        else:
            content = args.source
    elif not sys.stdin.isatty():
        content = sys.stdin.read()
    else:
        parser.print_help()
        sys.exit(1)

    summary = summarize_text(content, model=args.model)
    if args.save:
        with open(args.save, "w", encoding="utf-8") as f:
            f.write(summary + "\n")
        print(f"Summary saved to {args.save}")
    else:
        print(summary)

if __name__ == "__main__":
    main()
