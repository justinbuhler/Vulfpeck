#!/usr/bin/env python3
"""Block a turn that ends by announcing work it has not done (AGENTS.md "Turn completion").

    Stop   read the final assistant message (Claude and Codex `last_assistant_message`,
           Gemini AfterAgent `prompt_response`, which scripts/hook_adapter.py renames;
           Claude's transcript is the fallback). If its last paragraph announces unstarted
           work, block ONCE with "do it or name the blocker". A second stop, a
           stop_hook_active continuation, or the same message passes.

ANNOUNCE is the one list of announcing phrases; tests/test_loop_guards.py holds its
false-positive cases. Fenced code, quoted text, negations, questions and a paragraph
that names a blocker never match.

Registered as a gate: a crash exits 2 (harness.toml [[hook]]).
"""
import hashlib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import guard_lib as g  # noqa: E402

SUBJECT = r"(?:i|we)"
ANNOUNCE = (
    rf"\bnext,? {SUBJECT}(?:'ll| will)\b",
    rf"\b{SUBJECT}(?:'ll| will) now\b",
    rf"\bnow,? {SUBJECT}(?:'ll| will)\b",
    r"\blet me now\b",
    r"\blet me (?:run|check|do|start|try|fix|update|write|look|add|create|implement|verify|read|test|go)\b",
    rf"\b(?:{SUBJECT} am|{SUBJECT}'m|we are|we're) going to\b",
    rf"\b{SUBJECT}(?:'ll| will) (?:start|begin|proceed)\b",
    rf"\b{SUBJECT}(?:'ll| will)\b[^.!?]{{0,60}}\bnext[.!]?\s*$",
)
# Conditional or waiting phrasings and plans: the work is deliberately not started.
WAITING = re.compile(
    r"\b(?:after|if|once|when|until|unless|before) you\b|\bwait(?:ing)? for (?:your|you)\b"
    r"|\b(?:i|we)(?:'ll| will) now stop\b|\b(?:i|we)'?m going to need\b|\bgoing to need\b",
    re.I,
)
NUMBERED = re.compile(r"(?m)^\s*\d+[.)]\s")
NEGATED = re.compile(r"\b(?:will not|won't|'ll not|not going to|isn't going to|aren't going to)\b|n't going to", re.I)
BLOCKER = re.compile(r"\bblock(?:er|ed|ing)\b", re.I)
FENCE = re.compile(r"```.*?```", re.S)
QUOTED = re.compile(r"`[^`\n]*`|\"[^\"\n]*\"|“[^”\n]*”")
REASON = ("turn_guard: AGENTS.md: " + g.RULE_TURN + " Your last message announces work you have not started "
          "({phrase!r}). Do it now, or say what blocks you.")


def last_message(payload):
    """Return the final assistant message of the turn, or ""."""
    text = payload.get("last_assistant_message")
    if isinstance(text, str) and text:
        return text
    path = payload.get("transcript_path")
    try:
        for line in reversed(Path(path).read_text(encoding="utf-8").splitlines()):
            entry = json.loads(line)
            content = (entry.get("message") or {}).get("content")
            if entry.get("type") == "assistant" and isinstance(content, list):
                parts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
                if any(parts):
                    return "\n".join(parts)
    except (OSError, ValueError, TypeError):
        pass
    return ""


def last_paragraph(text):
    """Return the last non-empty paragraph with fenced code and blockquote lines removed."""
    text = FENCE.sub("", text)
    text = "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith(">"))
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    return paragraphs[-1] if paragraphs else ""


def announced(text):
    """Return the announcing phrase found in the last paragraph of text, or None."""
    paragraph = last_paragraph(text)
    if BLOCKER.search(paragraph) or NUMBERED.search(paragraph):
        return None
    for sentence in re.split(r"(?<=[.!?:])\s+|\n", QUOTED.sub("", paragraph)):
        if sentence.rstrip().endswith("?") or NEGATED.search(sentence) or WAITING.search(sentence):
            continue
        for pattern in ANNOUNCE:
            found = re.search(pattern, sentence, re.I)
            if found:
                return found.group(0)
    return None


def main():
    payload = g.read_payload()
    if not payload or payload.get("hook_event_name") != "Stop" or payload.get("stop_hook_active"):
        return 0
    message = last_message(payload)
    phrase = announced(message)
    if phrase is None:
        return 0
    path = g.loop_path(payload)
    digest = hashlib.sha1(message.encode()).hexdigest()
    state, _ = g.load_state(path, g.fresh_loop) if path else (g.fresh_loop(), None)
    if state["turn_blocked"] == digest:
        return 0
    state["turn_blocked"] = digest
    if path:
        g.save_state(path, state)
    g.record("turn_guard", "block", "announced-work", phrase, payload)
    print(json.dumps({"decision": "block", "reason": REASON.format(phrase=phrase)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
