"""
Per-task OpenAI model configuration, shared with the iOS app.

Rows live in the Supabase table `public.ai_model_config` (task, model,
reasoning_effort, verbosity, fallback_model) and are edited from the Supabase
Table Editor. The same rows drive the `openai-proxy` Edge Function used by the
iOS app, so one edit switches both on-device and server-side meetings.

The table is cached for CONFIG_TTL_SECONDS; if it can't be read, the last
known config (or DEFAULT_CONFIG) is used so summaries never fail on config.
"""

import time
from typing import Any, Dict, Optional

from openai import BadRequestError, NotFoundError, OpenAI

from supabase_client import supabase

CONFIG_TTL_SECONDS = 60

# Used when the table is unreachable or has no row for a task
DEFAULT_CONFIG: Dict[str, Any] = {
    "model": "gpt-6-luna",
    "reasoning_effort": "low",
    "verbosity": None,
    "fallback_model": "gpt-6-luna",
}

_cache: Dict[str, Dict[str, Any]] = {}
_cache_loaded_at: float = 0.0


def get_task_config(task: str) -> Dict[str, Any]:
    """Return the config row for `task`, merged over DEFAULT_CONFIG."""
    global _cache, _cache_loaded_at

    if time.monotonic() - _cache_loaded_at > CONFIG_TTL_SECONDS:
        try:
            result = supabase.table("ai_model_config").select(
                "task, model, reasoning_effort, verbosity, fallback_model"
            ).execute()
            _cache = {row["task"]: row for row in result.data or []}
        except Exception as e:
            print(f"⚠️ Failed to load ai_model_config, using cached/default config: {e}")
        # Also throttles retries while the table is unreachable
        _cache_loaded_at = time.monotonic()

    row = _cache.get(task, {})
    return {**DEFAULT_CONFIG, **{k: v for k, v in row.items() if v is not None or k == "fallback_model"}}


def create_response(
    client: OpenAI,
    task: str,
    input: list,
    default_verbosity: Optional[str] = None,
):
    """
    Call the Responses API with the model/effort/verbosity configured for `task`.

    `default_verbosity` is used when the task's row leaves verbosity NULL.
    Retries once on `fallback_model` if OpenAI rejects the request (400/404).
    """
    config = get_task_config(task)

    def call(model: str):
        kwargs: Dict[str, Any] = {"model": model, "input": input}
        if config["reasoning_effort"]:
            kwargs["reasoning"] = {"effort": config["reasoning_effort"]}
        verbosity = config["verbosity"] or default_verbosity
        if verbosity:
            kwargs["text"] = {"verbosity": verbosity}
        return client.responses.create(**kwargs)

    model = config["model"]
    try:
        response = call(model)
    except (BadRequestError, NotFoundError) as e:
        fallback = config["fallback_model"]
        if not fallback or fallback == model:
            raise
        print(f"   ⚠️ {task}: model {model} rejected ({e}); retrying with {fallback}")
        model = fallback
        response = call(model)

    print(f"   🤖 {task}: model={model} effort={config['reasoning_effort']}")
    return response
