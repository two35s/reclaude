"""Text-based tool-call emulation for the OpenAI-compatible proxy.

Claude.ai exposes a native client-tool channel, but it is a side channel on
an already-open stream: the completion stream stays open waiting for
``POST .../chat_conversations/{id}/tool_result``. A client that only acts on
a finished response (OpenCode) deadlocks -- it cannot run the tool until the
response ends, and the response cannot end until the tool result is posted.

So tools are emulated in text. The proxy describes the tools in the prompt,
asks the model to emit fenced ``tool_call`` blocks, and converts those blocks
into real OpenAI ``tool_calls``. The client runs the tools and returns the
results on the next request, where they are folded back into the prompt as
``[Tool result]`` turns.
"""

import json
import uuid

FENCE_START = "```tool_call"
FENCE_END = "```"

# The chatgpt.com surface has no tool channel and its chat persona wins against
# a prompt that reads like pasted instructions: it answers "I can't access that
# workspace" instead of emitting a fence. Leading with the harness contract (and
# saying the machine is remote on purpose) makes it emit calls.
_HARNESS_PREAMBLE = """[Tool protocol]
You are the model behind OpenCode, a coding harness that owns a remote machine.
That machine's files and shell are not visible to you directly: the only way to
read, edit, or run anything is to emit tool calls. The harness runs each call
and returns the output as a [Tool result] turn in the next request.

"""

# Kept next to the parser so the instructed format and the parsed format
# cannot drift apart. {specs} is filled by render_tool_prompt; JSON braces
# are doubled because this goes through str.format.
_PROTOCOL = """[Available tools]
{specs}

[Tool calling]
To call a tool, emit a fenced block containing exactly one JSON object:

```tool_call
{{"name": "<name from the list above>", "arguments": {{<object matching that tool's parameters>}}}}
```

Rules:
- To act, emit a tool_call block. Never reply that a workspace, file, or tool is
  inaccessible; call a tool instead — the harness has access to it.
- `arguments` must be an object matching that tool's parameter schema.
- To call several tools at once, emit several separate blocks.
- Only use tool names listed above.
- Put any explanation in normal text outside the blocks.
- After results arrive as [Tool result] turns, continue your work.
"""


def render_tool_prompt(tools, harness=False):
    """Render OpenAI ``tools`` into the spec + protocol block for the prompt.

    ``harness`` selects the OpenAI-compatible framing used for chatgpt.com,
    whose chat surface otherwise refuses to act; the claude backend keeps the
    plainer block.

    Returns "" when no usable tool is present so callers can skip appending.
    """
    specs = []
    for tool in tools or []:
        fn = tool.get("function") if isinstance(tool, dict) else None
        if not isinstance(fn, dict):
            continue
        name = fn.get("name")
        if not name:
            continue
        params = fn.get("parameters") or {"type": "object", "properties": {}}
        specs.append(
            f"- {name}: {fn.get('description') or '(no description)'}\n"
            f"  parameters: {json.dumps(params)}"
        )
    if not specs:
        return ""
    template = (_HARNESS_PREAMBLE if harness else "") + _PROTOCOL
    return template.format(specs="\n".join(specs))


def _parse_block(body):
    """Parse one fence body into an OpenAI tool_call dict, or None."""
    try:
        obj = json.loads(body.strip())
    except ValueError:
        return None
    if not isinstance(obj, dict):
        return None
    name = obj.get("name")
    if not isinstance(name, str) or not name:
        return None
    args = obj.get("arguments", {})
    if not isinstance(args, dict):
        args = {"value": args}
    return {
        "id": f"call_{uuid.uuid4().hex[:24]}",
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    }


def _hold_back(buf, marker):
    """Length of the longest suffix of buf that is a proper prefix of marker.

    A start marker split across two network chunks must not be emitted.
    """
    for k in range(min(len(marker) - 1, len(buf)), 0, -1):
        if buf.endswith(marker[:k]):
            return k
    return 0


class ToolCallParser:
    """Incrementally split streamed text into plain text and tool calls.

    ``feed`` returns only text that is safe to emit as assistant content now;
    everything from a fence start onward is buffered until the fence closes.
    ``finish`` flushes the remainder.
    """

    def __init__(self):
        self._buf = ""
        self._in_fence = False
        self.calls = []

    def feed(self, text):
        self._buf += text
        out = []
        while True:
            if self._in_fence:
                end = self._buf.find(FENCE_END)
                if end < 0:
                    break
                call = _parse_block(self._buf[:end])
                if call is not None:
                    self.calls.append(call)
                else:
                    # Malformed fence: surface it as text rather than drop it.
                    out.append(FENCE_START + self._buf[:end] + FENCE_END)
                self._buf = self._buf[end + len(FENCE_END) :]
                self._in_fence = False
            else:
                start = self._buf.find(FENCE_START)
                if start < 0:
                    keep = _hold_back(self._buf, FENCE_START)
                    out.append(self._buf[:-keep] if keep else self._buf)
                    self._buf = self._buf[-keep:] if keep else ""
                    break
                out.append(self._buf[:start])
                self._buf = self._buf[start + len(FENCE_START) :]
                self._in_fence = True
        return "".join(out)

    def finish(self):
        """Flush the buffered tail; returns (text, calls).

        An unterminated fence is emitted as plain text so nothing is lost.
        """
        tail = self._buf
        if self._in_fence:
            tail = FENCE_START + tail
            self._in_fence = False
        self._buf = ""
        return tail, self.calls


def parse_tool_calls(text):
    """One-shot parse of a complete response; returns (plain_text, calls)."""
    parser = ToolCallParser()
    head = parser.feed(text)
    tail, calls = parser.finish()
    return head + tail, calls


def render_assistant_calls(tool_calls):
    """Re-render OpenAI ``tool_calls`` back into a fence for the transcript.

    Keeps the model's own prior calls in the same syntax it is asked to emit.
    """
    out = []
    for tc in tool_calls or []:
        fn = tc.get("function") or {}
        raw = fn.get("arguments") or "{}"
        try:
            args = json.loads(raw) if isinstance(raw, str) else raw
        except ValueError:
            args = {"_raw": raw}
        if not isinstance(args, dict):
            args = {"value": args}
        body = json.dumps({"name": fn.get("name", ""), "arguments": args})
        out.append(f"{FENCE_START}\n{body}\n{FENCE_END}")
    return "\n".join(out)
