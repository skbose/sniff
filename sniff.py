"""
sniff.py — LLM Call Sniffer for Claude Code

Intercepts and logs all Anthropic API calls made by Claude Code.
Run with:
    python sniff.py
Then in another terminal:
    ANTHROPIC_BASE_URL=http://localhost:8080 claude
"""

import asyncio
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import AsyncIterator
from uuid import uuid4

from dotenv import load_dotenv

load_dotenv()

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import Response, StreamingResponse

# Langfuse is optional — imported only when keys are present
try:
    from langfuse import Langfuse, propagate_attributes as lf_propagate

    _langfuse_available = True
except ImportError:
    _langfuse_available = False

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

PORT = int(os.getenv("SNIFF_PORT", "8080"))
LOG_DIR = Path(os.getenv("SNIFF_LOG_DIR", "./sniff-logs"))
UPSTREAM = os.getenv("SNIFF_UPSTREAM", "https://api.anthropic.com")
SESSION_ID = datetime.utcnow().strftime("%Y%m%d_%H%M%S") + "_" + uuid4().hex[:6]

app = FastAPI(title="sniff", docs_url=None, redoc_url=None)

# Langfuse client — initialised in startup() if keys are present
lf: "Langfuse | None" = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def sanitize_headers(headers: dict) -> dict:
    """Mask sensitive header values for logging."""
    sanitized = {}
    for k, v in headers.items():
        if k.lower() == "x-api-key" and v:
            sanitized[k] = v[:8] + "****" if len(v) > 8 else "****"
        else:
            sanitized[k] = v
    return sanitized


def parse_sse_events(raw_chunks: list[bytes]) -> list[dict]:
    """Parse raw SSE byte chunks into a list of event dicts."""
    events = []
    buffer = ""
    for chunk in raw_chunks:
        buffer += chunk.decode("utf-8", errors="replace")

    for block in buffer.split("\n\n"):
        block = block.strip()
        if not block:
            continue
        event_type = None
        data_lines = []
        for line in block.splitlines():
            if line.startswith("event:"):
                event_type = line[len("event:"):].strip()
            elif line.startswith("data:"):
                data_lines.append(line[len("data:"):].strip())
        if data_lines:
            raw_data = "\n".join(data_lines)
            try:
                parsed_data = json.loads(raw_data)
            except json.JSONDecodeError:
                parsed_data = raw_data
            events.append({"event": event_type, "data": parsed_data})

    return events


def reconstruct_response(sse_events: list[dict]) -> dict:
    """
    Reconstruct a synthetic full-response object from SSE events.
    Handles text blocks and tool_use blocks.
    """
    content_blocks = {}  # index -> block dict
    usage = {}
    model = None
    stop_reason = None
    message_id = None

    for ev in sse_events:
        data = ev.get("data")
        if not isinstance(data, dict):
            continue
        event_type = ev.get("event") or data.get("type", "")

        if event_type == "message_start":
            msg = data.get("message", {})
            model = msg.get("model")
            message_id = msg.get("id")
            usage = msg.get("usage", {})

        elif event_type == "content_block_start":
            idx = data.get("index", 0)
            content_blocks[idx] = data.get("content_block", {})

        elif event_type == "content_block_delta":
            idx = data.get("index", 0)
            delta = data.get("delta", {})
            block = content_blocks.get(idx, {})
            delta_type = delta.get("type")

            if delta_type == "text_delta":
                existing = block.get("text", "")
                block["text"] = existing + delta.get("text", "")
                content_blocks[idx] = block

            elif delta_type == "input_json_delta":
                existing = block.get("_partial_json", "")
                block["_partial_json"] = existing + delta.get("partial_json", "")
                content_blocks[idx] = block

        elif event_type == "content_block_stop":
            idx = data.get("index", 0)
            block = content_blocks.get(idx, {})
            # Finalize tool_use input from accumulated JSON
            if block.get("type") == "tool_use" and "_partial_json" in block:
                try:
                    block["input"] = json.loads(block.pop("_partial_json"))
                except json.JSONDecodeError:
                    block["input"] = block.pop("_partial_json")
                content_blocks[idx] = block

        elif event_type == "message_delta":
            delta = data.get("delta", {})
            stop_reason = delta.get("stop_reason", stop_reason)
            usage.update(data.get("usage", {}))

    ordered_blocks = [content_blocks[i] for i in sorted(content_blocks)]

    return {
        "id": message_id,
        "type": "message",
        "model": model,
        "stop_reason": stop_reason,
        "content": ordered_blocks,
        "usage": usage,
    }


def extract_tool_pairs(messages: list[dict]) -> list[dict]:
    """
    Walk the message history and return completed tool call pairs.

    Each assistant turn may contain tool_use blocks; the following user turn
    carries the matching tool_result blocks.  We collect both so we can emit
    a proper tool span (input + output) in Langfuse.
    """
    pairs: list[dict] = []
    pending: dict[str, dict] = {}  # tool_use_id -> {name, input}

    for msg in messages:
        role = msg.get("role")
        content = msg.get("content", [])
        if isinstance(content, str):
            continue

        if role == "assistant":
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    pending[block["id"]] = {
                        "name": block.get("name", "unknown_tool"),
                        "input": block.get("input", {}),
                    }

        elif role == "user":
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    uid = block.get("tool_use_id", "")
                    if uid not in pending:
                        continue
                    tool_info = pending.pop(uid)
                    result = block.get("content", "")
                    # content can be a list of text blocks or a plain string
                    if isinstance(result, list):
                        result = "\n".join(
                            b.get("text", "")
                            for b in result
                            if isinstance(b, dict) and b.get("type") == "text"
                        )
                    pairs.append(
                        {
                            "tool_use_id": uid,
                            "name": tool_info["name"],
                            "input": tool_info["input"],
                            "output": result,
                        }
                    )

    return pairs


async def send_to_langfuse(
    call_id: str,
    duration_ms: float,
    streaming: bool,
    req_body: dict,
    response_obj: dict,
) -> None:
    """Send a captured call to Langfuse as a generation with nested tool spans."""
    if lf is None:
        return

    model = req_body.get("model", "unknown")
    system = req_body.get("system")
    messages = req_body.get("messages", [])
    content = response_obj.get("content", [])
    usage = response_obj.get("usage", {})

    input_payload: list[dict] = []
    if system:
        input_payload.append({"role": "system", "content": system})
    input_payload.extend(messages)

    # Simplify single-text-block output to a plain string
    if len(content) == 1 and content[0].get("type") == "text":
        output_payload: object = content[0].get("text", "")
    else:
        output_payload = content

    model_params = {
        k: v
        for k, v in {
            "max_tokens": req_body.get("max_tokens"),
            "temperature": req_body.get("temperature"),
        }.items()
        if v is not None
    }

    tool_pairs = extract_tool_pairs(messages)

    try:
        with lf_propagate(session_id=SESSION_ID):
            with lf.start_as_current_observation(
                name="anthropic/messages",
                as_type="generation",
                model=model,
                input=input_payload,
                model_parameters=model_params or None,
                usage_details={
                    "input": usage.get("input_tokens", 0),
                    "output": usage.get("output_tokens", 0),
                },
                output=output_payload,
                metadata={
                    "call_id": call_id,
                    "streaming": streaming,
                    "stop_reason": response_obj.get("stop_reason"),
                    "duration_ms": round(duration_ms, 2),
                },
            ):
                for tool in tool_pairs:
                    with lf.start_as_current_observation(
                        name=tool["name"],
                        as_type="tool",
                        input=tool["input"],
                        output=tool["output"],
                        metadata={"tool_use_id": tool["tool_use_id"]},
                    ):
                        pass
                    print(f"  [sniff] langfuse tool → {tool['name']}", flush=True)

                trace_url = lf.get_trace_url()
                print(f"  [sniff] langfuse → {trace_url}", flush=True)
    except Exception as exc:
        print(f"  [sniff] langfuse error: {exc}", flush=True)


async def write_log(
    call_id: str,
    timestamp: str,
    duration_ms: float,
    streaming: bool,
    req_body: dict,
    req_headers: dict,
    response_obj: dict,
    sse_events: list[dict] | None = None,
) -> None:
    """Write a single call log to disk as JSON."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_entry = {
        "schema_version": "1.0",
        "session_id": SESSION_ID,
        "call_id": call_id,
        "timestamp": timestamp,
        "duration_ms": round(duration_ms, 2),
        "streaming": streaming,
        "request": {
            "model": req_body.get("model"),
            "system": req_body.get("system"),
            "messages": req_body.get("messages", []),
            "tools": req_body.get("tools", []),
            "tool_choice": req_body.get("tool_choice"),
            "max_tokens": req_body.get("max_tokens"),
            "temperature": req_body.get("temperature"),
            "headers": sanitize_headers(req_headers),
        },
        "response": {
            "reconstructed": response_obj,
            **({"sse_events": sse_events} if sse_events is not None else {}),
        },
    }
    filename = LOG_DIR / f"{SESSION_ID}_{call_id}.json"
    filename.write_text(json.dumps(log_entry, indent=2, default=str))
    print(f"  [sniff] logged → {filename}", flush=True)

    await send_to_langfuse(
        call_id=call_id,
        duration_ms=duration_ms,
        streaming=streaming,
        req_body=req_body,
        response_obj=response_obj,
    )


# ---------------------------------------------------------------------------
# Request handlers
# ---------------------------------------------------------------------------

async def handle_non_streaming(
    req_body: dict,
    req_headers: dict,
    body_bytes: bytes,
    call_id: str,
    timestamp: str,
    t_start: float,
) -> Response:
    """Forward a non-streaming request and log the response."""
    STRIP_REQ = {"host", "content-length", "accept-encoding"}
    fwd_headers = {k: v for k, v in req_headers.items() if k.lower() not in STRIP_REQ}

    async with httpx.AsyncClient(timeout=120.0) as client:
        upstream_resp = await client.post(
            f"{UPSTREAM}/v1/messages",
            content=body_bytes,
            headers=fwd_headers,
        )

    duration_ms = (asyncio.get_event_loop().time() - t_start) * 1000
    resp_bytes = upstream_resp.content

    try:
        resp_json = json.loads(resp_bytes)
    except json.JSONDecodeError:
        resp_json = {"raw": resp_bytes.decode("utf-8", errors="replace")}

    asyncio.create_task(
        write_log(
            call_id=call_id,
            timestamp=timestamp,
            duration_ms=duration_ms,
            streaming=False,
            req_body=req_body,
            req_headers=req_headers,
            response_obj=resp_json,
        )
    )

    # httpx decompresses the body automatically; strip encoding/length headers
    # so the client doesn't try to decompress already-decompressed bytes.
    STRIP_RESP = {"content-encoding", "content-length", "transfer-encoding"}
    resp_headers = {k: v for k, v in upstream_resp.headers.items() if k.lower() not in STRIP_RESP}

    return Response(
        content=resp_bytes,
        status_code=upstream_resp.status_code,
        headers=resp_headers,
        media_type=upstream_resp.headers.get("content-type", "application/json"),
    )


async def handle_streaming(
    req_body: dict,
    req_headers: dict,
    body_bytes: bytes,
    call_id: str,
    timestamp: str,
    t_start: float,
) -> StreamingResponse:
    """Forward a streaming request, capture SSE, and log after completion."""
    STRIP_REQ = {"host", "content-length", "accept-encoding"}
    fwd_headers = {k: v for k, v in req_headers.items() if k.lower() not in STRIP_REQ}

    async def stream_and_capture() -> AsyncIterator[bytes]:
        sse_chunks: list[bytes] = []
        async with httpx.AsyncClient(timeout=120.0) as client:
            async with client.stream(
                "POST",
                f"{UPSTREAM}/v1/messages",
                content=body_bytes,
                headers=fwd_headers,
            ) as upstream:
                async for chunk in upstream.aiter_bytes(chunk_size=None):
                    sse_chunks.append(chunk)
                    yield chunk

        duration_ms = (asyncio.get_event_loop().time() - t_start) * 1000
        sse_events = parse_sse_events(sse_chunks)
        reconstructed = reconstruct_response(sse_events)

        asyncio.create_task(
            write_log(
                call_id=call_id,
                timestamp=timestamp,
                duration_ms=duration_ms,
                streaming=True,
                req_body=req_body,
                req_headers=req_headers,
                response_obj=reconstructed,
                sse_events=sse_events,
            )
        )

    return StreamingResponse(
        stream_and_capture(),
        media_type="text/event-stream",
        headers={"cache-control": "no-cache", "x-accel-buffering": "no"},
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.post("/v1/messages")
async def proxy_messages(request: Request) -> Response:
    body_bytes = await request.body()
    req_body = json.loads(body_bytes)
    is_stream = req_body.get("stream", False)
    req_headers = dict(request.headers)

    call_id = uuid4().hex[:12]
    timestamp = datetime.utcnow().isoformat() + "Z"
    t_start = asyncio.get_event_loop().time()

    model = req_body.get("model", "?")
    n_msgs = len(req_body.get("messages", []))
    print(
        f"[sniff] {timestamp}  call_id={call_id}  model={model}  "
        f"messages={n_msgs}  stream={is_stream}",
        flush=True,
    )

    if is_stream:
        return await handle_streaming(req_body, req_headers, body_bytes, call_id, timestamp, t_start)
    else:
        return await handle_non_streaming(req_body, req_headers, body_bytes, call_id, timestamp, t_start)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "session_id": SESSION_ID}


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

@app.on_event("startup")
async def startup() -> None:
    global lf
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    # Initialise Langfuse if credentials are present
    if _langfuse_available:
        pk = os.getenv("LANGFUSE_PUBLIC_KEY")
        sk = os.getenv("LANGFUSE_SECRET_KEY")
        if pk and sk:
            lf = Langfuse(
                public_key=pk,
                secret_key=sk,
                host=os.getenv("LANGFUSE_HOST", "https://cloud.langfuse.com"),
            )

    print(f"[sniff] session_id : {SESSION_ID}", flush=True)
    print(f"[sniff] listening  : http://localhost:{PORT}", flush=True)
    print(f"[sniff] upstream   : {UPSTREAM}", flush=True)
    print(f"[sniff] log dir    : {LOG_DIR.resolve()}", flush=True)
    print(f"[sniff] langfuse   : {'enabled' if lf else 'disabled (set LANGFUSE_PUBLIC_KEY + LANGFUSE_SECRET_KEY)'}", flush=True)
    print(f"", flush=True)
    print(f"  To use with Claude Code:", flush=True)
    print(f"    ANTHROPIC_BASE_URL=http://localhost:{PORT} claude", flush=True)
    print(f"", flush=True)


@app.on_event("shutdown")
async def shutdown() -> None:
    if lf is not None:
        lf.flush()


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "sniff:app",
        host="0.0.0.0",
        port=PORT,
        log_level="warning",
    )
