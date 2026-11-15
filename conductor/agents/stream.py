"""Parse Claude Code stream-json lines into conductor events. Pure: no I/O.

Each stdout line of ``claude -p --output-format stream-json --verbose`` is one
JSON object. The ones we care about:

    system/init               {session_id, cwd, model, tools}
    assistant                 message.content[]: text | tool_use | thinking
    user                      message.content[]: tool_result {is_error, content}
    system/task_summary       {detail}            short "what it's doing"
    system/post_turn_summary  {status_detail}
    result                    {subtype, is_error, result, total_cost_usd, num_turns,
                               session_id, duration_ms}

Everything else (rate_limit_event, unknown types, non-JSON garbage) is ignored.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from .base import extract_question

ACTIVITY_MAX = 120
TEXT_MAX = 4000
ERROR_MAX = 300
INPUT_MAX = 2048


@dataclass
class Parsed:
    events: list[tuple[str, str, Any]] = field(default_factory=list)
    activity: str | None = None
    session_id: str | None = None
    result: dict | None = None


def _one_line(s: Any, limit: int = ACTIVITY_MAX) -> str:
    s = " ".join(str(s or "").split())
    if len(s) > limit:
        s = s[: limit - 1].rstrip() + "…"
    return s


def _short_path(p: Any, keep: int = 3) -> str:
    p = str(p or "").replace("\\", "/").rstrip("/")
    parts = [x for x in p.split("/") if x]
    if len(parts) > keep:
        return "…/" + "/".join(parts[-keep:])
    return p or "?"


def _first_line(s: Any) -> str:
    for line in str(s or "").splitlines():
        if line.strip():
            return line.strip()
    return ""


def describe_tool(name: str, input: Any) -> str:
    """Human phrasing of a tool call, e.g. ``Reading src/app.py``."""
    inp = input if isinstance(input, dict) else {}
    n = name or "tool"
    path = inp.get("file_path") or inp.get("path") or inp.get("notebook_path")
    if n == "Read":
        out = f"Reading {_short_path(path)}"
    elif n in ("Edit", "MultiEdit", "NotebookEdit"):
        out = f"Editing {_short_path(path)}"
    elif n == "Write":
        out = f"Writing {_short_path(path)}"
    elif n == "Bash":
        what = inp.get("description") or _first_line(inp.get("command"))
        out = f"Running: {_one_line(what, 100)}"
    elif n in ("Grep", "Glob"):
        out = f"Searching for {_one_line(inp.get('pattern'), 90)}"
        if n == "Grep" and inp.get("path"):
            out += f" in {_short_path(inp.get('path'), 2)}"
    elif n == "WebFetch":
        out = f"Fetching {_one_line(inp.get('url'), 100)}"
    elif n == "WebSearch":
        out = f"Searching the web for {_one_line(inp.get('query'), 90)}"
    elif n in ("Task", "Agent"):
        out = f"Delegating: {_one_line(inp.get('description') or inp.get('prompt'), 100)}"
    elif n == "TodoWrite":
        todos = inp.get("todos") if isinstance(inp.get("todos"), list) else []
        cur = next((t for t in todos if isinstance(t, dict)
                    and t.get("status") == "in_progress"), None)
        if cur:
            out = "Updating plan: " + _one_line(cur.get("activeForm") or cur.get("content"), 100)
        else:
            out = "Updating plan"
    else:
        out = f"Using {n}"
    return _one_line(out)


def activity_from_text(text: str) -> str:
    """First non-empty line of assistant text, single line, ≤ 120 chars."""
    return _one_line(_first_line(text))


def _small(value: Any, limit: int = INPUT_MAX) -> Any:
    """A JSON-safe copy of ``value`` whose serialization is ≤ ``limit`` bytes."""
    try:
        s = json.dumps(value, default=str)
    except (TypeError, ValueError):
        return {"_repr": str(value)[:200]}
    if len(s) <= limit:
        return json.loads(s)
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        budget = limit - 50
        per = max(80, budget // max(1, len(value)))
        for k, v in value.items():
            if isinstance(v, str):
                v2: Any = v if len(v) <= per else v[:per] + "…"
            elif isinstance(v, (int, float, bool)) or v is None:
                v2 = v
            else:
                vs = json.dumps(v, default=str)
                v2 = v if len(vs) <= per else vs[:per] + "…"
            out[str(k)] = v2
            if len(json.dumps(out, default=str)) > budget:
                out.pop(str(k))
                out["_truncated"] = True
                break
        return out
    return {"_truncated": True, "_repr": s[: limit - 60]}


def _tool_result_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for c in content:
            if isinstance(c, dict) and c.get("type") == "text":
                parts.append(str(c.get("text") or ""))
            elif isinstance(c, str):
                parts.append(c)
        return "\n".join(parts)
    return str(content or "")


def _num(v: Any) -> float | None:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def parse_line(line: str) -> Parsed:
    p = Parsed()
    line = (line or "").strip()
    if not line or line[0] != "{":
        return p
    try:
        obj = json.loads(line)
    except (ValueError, RecursionError):
        return p
    if not isinstance(obj, dict):
        return p
    typ = obj.get("type")
    sub = obj.get("subtype")
    sid = obj.get("session_id")
    if isinstance(sid, str) and sid:
        p.session_id = sid

    try:
        if typ == "system":
            if sub == "init":
                model = obj.get("model")
                p.events.append(("info", "Agent started" + (f" ({model})" if model else ""),
                                 {"model": model, "cwd": obj.get("cwd")}))
                p.activity = "Starting"
            elif sub == "task_summary":
                d = obj.get("detail")
                if isinstance(d, str) and d.strip():
                    p.activity = _one_line(d)
            elif sub == "post_turn_summary":
                d = obj.get("status_detail")
                if isinstance(d, str) and d.strip():
                    p.events.append(("info", _one_line(d, 300), None))
        elif typ == "assistant":
            content = (obj.get("message") or {}).get("content") or []
            if isinstance(content, str):
                content = [{"type": "text", "text": content}]
            for item in content if isinstance(content, list) else []:
                if not isinstance(item, dict):
                    continue
                it = item.get("type")
                if it == "tool_use":
                    name = str(item.get("name") or "tool")
                    desc = describe_tool(name, item.get("input"))
                    p.events.append(("tool", desc, {"name": name,
                                                    "input": _small(item.get("input") or {})}))
                    p.activity = desc
                elif it == "text":
                    text = str(item.get("text") or "").strip()
                    if not text:
                        continue
                    t = text if len(text) <= TEXT_MAX else text[:TEXT_MAX] + "…"
                    p.events.append(("text", t, None))
                    first = _first_line(text)
                    if len(first) <= 200:
                        p.activity = activity_from_text(text)
                # thinking / redacted_thinking / others: ignored
        elif typ == "user":
            content = (obj.get("message") or {}).get("content") or []
            for item in content if isinstance(content, list) else []:
                if (isinstance(item, dict) and item.get("type") == "tool_result"
                        and item.get("is_error")):
                    msg = _one_line(_first_line(_tool_result_text(item.get("content"))),
                                    ERROR_MAX) or "Tool error"
                    p.events.append(("error", msg, None))
        elif typ == "result":
            text = obj.get("result")
            text = text if isinstance(text, str) else ""
            is_error = bool(obj.get("is_error")) or (
                isinstance(sub, str) and sub != "success")
            res = {
                "subtype": sub or ("error" if is_error else "success"),
                "is_error": is_error,
                "text": text,
                "cost_usd": _num(obj.get("total_cost_usd")),
                "num_turns": obj.get("num_turns"),
                "duration_ms": obj.get("duration_ms"),
            }
            p.result = res
            if is_error:
                p.activity = _one_line(f"Failed: {res['subtype']}")
                short = f"Failed ({res['subtype']})"
                if text:
                    short += ": " + _one_line(_first_line(text), 200)
            elif extract_question(text):
                p.activity = "Asked a question"
                short = "Asked: " + _one_line(extract_question(text), 200)
            else:
                p.activity = "Finished"
                short = "Finished"
            if res["cost_usd"] is not None:
                short += f" (${res['cost_usd']:.2f})"
            p.events.append(("result", short, res))
    except Exception:  # never let one odd line break ingestion
        return p
    return p
