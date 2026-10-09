"""Export a supplied Codex session's visible dialogue/tools, with secrets redacted.

Private reasoning, system instructions and runtime metadata are not chat content
and are intentionally excluded. Never searches or exports other user sessions.
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


def redact(text: str) -> str:
    text = re.sub(r"sk-(?:ant-)?[A-Za-z0-9_-]{20,}|eyJ[A-Za-z0-9_.-]{30,}", "[REDACTED_CREDENTIAL]", text)
    text = re.sub(r"(?i)(Bearer\s+)[A-Za-z0-9_.-]{20,}", r"\1[REDACTED_CREDENTIAL]", text)
    return re.sub(r"(https?://)[^\s/@]+:[^\s/@]+@", r"\1[REDACTED_CREDENTIAL]@", text)


def export(source: Path, destination: Path) -> int:
    source = source.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Snapshot before opening output; the active session may keep growing.
    lines = source.read_text().splitlines()
    records = []
    for line in lines:
        event = json.loads(line)
        payload = event.get("payload", {})
        if event.get("type") == "event_msg" and payload.get("type") in ("user_message", "agent_message"):
            records.append({"timestamp": event.get("timestamp"), "type": payload["type"],
                            "text": payload.get("message", "")})
        elif event.get("type") == "response_item" and payload.get("type") in ("function_call", "function_call_output", "custom_tool_call", "custom_tool_call_output"):
            records.append({"timestamp": event.get("timestamp"), "tool_event": payload})
    destination.write_text("\n".join(redact(json.dumps(record, ensure_ascii=False)) for record in records) + "\n")
    return len(records)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    print(f"Exported {export(args.source, args.destination)} visible dialogue/tool records.")
