"""
ab.py — A/B testing (shadow mode) for sniff

Variant A (primary) is forwarded to Claude Code unchanged.
Variant B (shadow) is fired in parallel with overrides applied,
its response is never returned — only logged and compared.

Configuration lives in ab.toml. If the file does not exist, this
module is a complete no-op.
"""

import asyncio
import copy
import json
import random
import tomllib
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx

# ---------------------------------------------------------------------------
# Experiment dataclass
# ---------------------------------------------------------------------------

@dataclass
class Experiment:
    name: str
    enabled: bool
    sample_rate: float
    filter: dict = field(default_factory=dict)
    variant_b: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Module state
# ---------------------------------------------------------------------------

_experiments: list[Experiment] = []
_upstream: str = "https://api.anthropic.com"   # set by init()


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

def init(path: Path, upstream: str) -> None:
    """Load experiments from ab.toml. Call once at startup."""
    global _upstream
    _upstream = upstream
    _experiments.clear()

    if not path.exists():
        return

    with path.open("rb") as f:
        data = tomllib.load(f)

    for raw in data.get("experiment", []):
        name = raw["name"]
        vb = raw.get("variant_b", {})

        if "system_append" in vb and "system_replace" in vb:
            raise ValueError(
                f"Experiment '{name}': system_append and system_replace are mutually exclusive"
            )

        sample_rate = float(raw.get("sample_rate", 1.0))
        if not 0.0 <= sample_rate <= 1.0:
            raise ValueError(f"Experiment '{name}': sample_rate must be between 0.0 and 1.0")

        _experiments.append(
            Experiment(
                name=name,
                enabled=bool(raw.get("enabled", True)),
                sample_rate=sample_rate,
                filter=raw.get("filter", {}),
                variant_b=vb,
            )
        )


# ---------------------------------------------------------------------------
# Experiment selection
# ---------------------------------------------------------------------------

def select_experiment(req_body: dict) -> Experiment | None:
    """Return the first matching experiment for this request, or None."""
    for exp in _experiments:
        if not exp.enabled:
            continue
        # Apply filter predicates
        if any(req_body.get(k) != v for k, v in exp.filter.items()):
            continue
        # Probabilistic gate
        if random.random() > exp.sample_rate:
            continue
        return exp
    return None


# ---------------------------------------------------------------------------
# Request override
# ---------------------------------------------------------------------------

def apply_overrides(req_body: dict, exp: Experiment) -> dict:
    """Return a deep copy of req_body with variant_b overrides applied."""
    body = copy.deepcopy(req_body)
    vb = exp.variant_b

    if "model" in vb:
        body["model"] = vb["model"]

    if "system_replace" in vb:
        body["system"] = vb["system_replace"]
    elif "system_append" in vb:
        existing = body.get("system", "")
        if isinstance(existing, str):
            body["system"] = existing + vb["system_append"]
        elif isinstance(existing, list):
            body["system"] = existing + [{"type": "text", "text": vb["system_append"]}]
        else:
            body["system"] = vb["system_append"]

    if "temperature" in vb:
        body["temperature"] = vb["temperature"]
    if "max_tokens" in vb:
        body["max_tokens"] = vb["max_tokens"]

    # Shadow is always non-streaming — we never return its response
    body["stream"] = False

    return body


# ---------------------------------------------------------------------------
# Comparison helpers
# ---------------------------------------------------------------------------

def _response_stats(response_obj: dict) -> dict:
    content = response_obj.get("content", [])
    text_parts = [
        b.get("text", "")
        for b in content
        if isinstance(b, dict) and b.get("type") == "text"
    ]
    response_text = "\n".join(text_parts)
    tool_calls = [
        b.get("name", "unknown")
        for b in content
        if isinstance(b, dict) and b.get("type") == "tool_use"
    ]
    usage = response_obj.get("usage", {})
    return {
        "model": response_obj.get("model"),
        "stop_reason": response_obj.get("stop_reason"),
        "input_tokens": usage.get("input_tokens", 0),
        "output_tokens": usage.get("output_tokens", 0),
        "response_length": len(response_text),
        "tool_call_count": len(tool_calls),
        "response_text": response_text,
        "tool_calls": tool_calls,
    }


def _compute_delta(primary: dict, shadow: dict) -> dict:
    keys = ("input_tokens", "output_tokens", "response_length", "tool_call_count")
    return {k: shadow.get(k, 0) - primary.get(k, 0) for k in keys}


def write_comparison(experiment_name: str, record: dict) -> None:
    comp_dir = Path("comparisons")
    comp_dir.mkdir(parents=True, exist_ok=True)
    path = comp_dir / f"{experiment_name}.jsonl"
    with path.open("a") as f:
        f.write(json.dumps(record, default=str) + "\n")


# ---------------------------------------------------------------------------
# Langfuse shadow generation (mirrors send_to_langfuse in sniff.py)
# ---------------------------------------------------------------------------

async def send_shadow_to_langfuse(
    lf: Any,
    lf_propagate: Any,
    session_id: str,
    call_id: str,
    ab_group_id: str,
    experiment_name: str,
    duration_ms: float,
    req_body: dict,
    response_obj: dict,
) -> None:
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

    try:
        with lf_propagate(session_id=session_id):
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
                    "streaming": False,
                    "stop_reason": response_obj.get("stop_reason"),
                    "duration_ms": round(duration_ms, 2),
                    "ab_group_id": ab_group_id,
                    "ab_experiment": experiment_name,
                    "ab_variant": "treatment",
                },
            ):
                pass
    except Exception as exc:
        print(f"  [ab] langfuse error: {exc}", flush=True)


# ---------------------------------------------------------------------------
# Shadow runner
# ---------------------------------------------------------------------------

async def run_shadow(
    primary_call_id: str,
    ab_group_id: str,
    req_body: dict,
    req_headers: dict,
    experiment: Experiment,
    primary_done: "asyncio.Future[dict]",
    lf: Any,
    lf_propagate: Any,
    session_id: str,
) -> None:
    """Fire the shadow request and log the comparison. Never raises."""
    shadow_call_id = uuid4().hex[:12]
    shadow_body = apply_overrides(req_body, experiment)
    shadow_bytes = json.dumps(shadow_body).encode()

    STRIP_REQ = {"host", "content-length", "accept-encoding"}
    fwd_headers = {k: v for k, v in req_headers.items() if k.lower() not in STRIP_REQ}

    loop = asyncio.get_running_loop()
    t_start = loop.time()
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            upstream_resp = await client.post(
                f"{_upstream}/v1/messages",
                content=shadow_bytes,
                headers=fwd_headers,
            )
        shadow_duration_ms = (loop.time() - t_start) * 1000
        shadow_response = upstream_resp.json()
    except Exception as exc:
        print(f"  [ab] shadow HTTP error ({experiment.name}): {exc}", flush=True)
        return

    print(
        f"  [ab] shadow done  experiment={experiment.name}  "
        f"model={shadow_body.get('model')}  duration={shadow_duration_ms:.0f}ms",
        flush=True,
    )

    # Wait for the primary response so we can compare
    try:
        primary_response = await asyncio.wait_for(
            asyncio.shield(primary_done), timeout=30.0
        )
    except (asyncio.TimeoutError, asyncio.CancelledError):
        primary_response = None

    # Build and write comparison
    primary_stats = _response_stats(primary_response) if primary_response else {}
    shadow_stats = _response_stats(shadow_response)
    shadow_stats["duration_ms"] = round(shadow_duration_ms, 2)
    if primary_response:
        primary_stats["duration_ms"] = primary_response.get("_duration_ms", 0)

    delta = _compute_delta(primary_stats, shadow_stats)

    record = {
        "schema_version": "1.0",
        "ts": datetime.utcnow().isoformat() + "Z",
        "session_id": session_id,
        "ab_group_id": ab_group_id,
        "experiment": experiment.name,
        "primary": {"call_id": primary_call_id, **primary_stats},
        "shadow": {"call_id": shadow_call_id, **shadow_stats},
        "delta": delta,
        "overrides_applied": {k: v for k, v in experiment.variant_b.items()
                              if k not in ("system_append", "system_replace")},
    }
    write_comparison(experiment.name, record)

    tok_delta = delta.get("output_tokens", 0)
    lat_delta = delta.get("duration_ms", shadow_stats.get("duration_ms", 0))
    print(
        f"  [ab] comparison   experiment={experiment.name}  "
        f"tok_delta={tok_delta:+d}  lat_delta={lat_delta:+.0f}ms",
        flush=True,
    )

    # Send shadow generation to Langfuse
    await send_shadow_to_langfuse(
        lf=lf,
        lf_propagate=lf_propagate,
        session_id=session_id,
        call_id=shadow_call_id,
        ab_group_id=ab_group_id,
        experiment_name=experiment.name,
        duration_ms=shadow_duration_ms,
        req_body=shadow_body,
        response_obj=shadow_response,
    )


# ---------------------------------------------------------------------------
# Entry point called from proxy_messages
# ---------------------------------------------------------------------------

def maybe_schedule_shadow(
    call_id: str,
    req_body: dict,
    req_headers: dict,
    lf: Any,
    lf_propagate: Any,
    session_id: str,
) -> tuple[str | None, str | None, "asyncio.Future[dict] | None"]:
    """
    If an experiment matches this request, schedule a shadow task and return
    (ab_group_id, experiment_name, primary_done_future).
    Otherwise return (None, None, None).
    """
    exp = select_experiment(req_body)
    if exp is None:
        return None, None, None

    ab_group_id = uuid4().hex[:12]
    primary_done: asyncio.Future[dict] = asyncio.get_running_loop().create_future()

    asyncio.create_task(
        run_shadow(
            primary_call_id=call_id,
            ab_group_id=ab_group_id,
            req_body=req_body,
            req_headers=req_headers,
            experiment=exp,
            primary_done=primary_done,
            lf=lf,
            lf_propagate=lf_propagate,
            session_id=session_id,
        )
    )
    print(
        f"  [ab] shadow scheduled  experiment={exp.name}  group={ab_group_id}",
        flush=True,
    )
    return ab_group_id, exp.name, primary_done
