# sniff

A transparent proxy that intercepts and logs every LLM API call made by Claude Code to local JSON files.

## How it works

Claude Code respects the `ANTHROPIC_BASE_URL` environment variable. `sniff` exploits this to sit between Claude Code and `api.anthropic.com`, logging each request/response pair before forwarding transparently.

## Setup

```bash
uv run sniff.py
```

In a separate terminal:

```bash
ANTHROPIC_BASE_URL=http://localhost:8080 claude
```

That's it. Every API call will be logged to `./sniff-logs/` as a JSON file.

## Langfuse integration

Set these env vars before starting the proxy and every call will be sent as a generation to Langfuse:

```bash
export LANGFUSE_PUBLIC_KEY=pk-lf-...
export LANGFUSE_SECRET_KEY=sk-lf-...
export LANGFUSE_HOST=https://cloud.langfuse.com  # optional, this is the default
uv run sniff.py
```

Each intercepted API call becomes a **generation** in Langfuse, grouped under the same **session** (one per proxy process). The trace URL is printed to stdout after each call.

## Configuration

| Env var               | Default                      | Description                              |
|-----------------------|------------------------------|------------------------------------------|
| `SNIFF_PORT`          | `8080`                       | Port to listen on                        |
| `SNIFF_LOG_DIR`       | `./sniff-logs`               | Directory to write log files             |
| `SNIFF_UPSTREAM`      | `https://api.anthropic.com`  | Upstream API to forward calls to         |
| `LANGFUSE_PUBLIC_KEY` | —                            | Langfuse public key (enables tracing)    |
| `LANGFUSE_SECRET_KEY` | —                            | Langfuse secret key                      |
| `LANGFUSE_HOST`       | `https://cloud.langfuse.com` | Langfuse host (for self-hosted installs) |

## Log format

Each call produces one JSON file named `{session_id}_{call_id}.json`:

```json
{
  "schema_version": "1.0",
  "session_id": "20240101_120000_abc123",
  "call_id": "def456789012",
  "timestamp": "2024-01-01T12:00:00.000000Z",
  "duration_ms": 1234.5,
  "streaming": true,
  "request": {
    "model": "claude-opus-4-6",
    "system": "...",
    "messages": [...],
    "tools": [...],
    "headers": { "x-api-key": "sk-ant-****" }
  },
  "response": {
    "reconstructed": {
      "content": [...],
      "usage": { "input_tokens": 100, "output_tokens": 50 }
    },
    "sse_events": [...]
  }
}
```

Notes:
- `x-api-key` is masked in logs (first 8 chars + `****`); the real key is forwarded upstream unchanged.
- For streaming responses, `sse_events` contains the raw parsed SSE events and `reconstructed` is the synthesized full response (text + tool_use blocks).
- For non-streaming responses, only `reconstructed` is present (it is the verbatim API response).

## Health check

```bash
curl http://localhost:8080/health
```
