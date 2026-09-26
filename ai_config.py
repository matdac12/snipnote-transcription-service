"""
Per-task OpenAI model configuration, shared with the iOS app.

Rows live in the Supabase table `public.ai_model_config` (task, model,
reasoning_effort, verbosity, fallback_model) and are edited from the Supabase
Table Editor. The same rows drive the `openai-proxy` Edge Function used by the
iOS app, so one edit switches both on-device and server-side meetings.

The table is the source of truth. It is cached for CONFIG_TTL_SECONDS; if it
can't be read, the last known config (or the defaults below) is used so jobs
never fail on config.
"""

import io
import os
import threading
import time
from typing import Any, Callable, Dict, Optional, TypeVar

from openai import BadRequestError, NotFoundError, OpenAI

from supabase_client import supabase

CONFIG_TTL_SECONDS = 60

CONFIG_FIELDS = ("model", "reasoning_effort", "verbosity", "fallback_model")

# Fallbacks for when the table is unreachable or has no row for a task.
# Keep in sync with the seed rows in the SnipNote migration.
DEFAULT_CONFIG: Dict[str, Any] = {
    "model": "gpt-6-luna",
    "reasoning_effort": "low",
    "verbosity": None,
    "fallback_model": None,
}
TRANSCRIPTION_DEFAULT_CONFIG: Dict[str, Any] = {
    "model": os.getenv("TRANSCRIPTION_MODEL", "gpt-transcribe"),
    "reasoning_effort": None,
    "verbosity": None,
    "fallback_model": "gpt-4o-transcribe",
}

_cache: Dict[str, Dict[str, Any]] = {}
_cache_loaded_at: float = float("-inf")  # monotonic() can be < TTL right after boot
_cache_lock = threading.Lock()

T = TypeVar("T")


def _refresh_cache_if_stale() -> None:
    global _cache, _cache_loaded_at

    # Transcription runs in several threads; only one of them refreshes
    with _cache_lock:
        if time.monotonic() - _cache_loaded_at <= CONFIG_TTL_SECONDS:
            return
        try:
            result = supabase.table("ai_model_config").select("task, " + ", ".join(CONFIG_FIELDS)).execute()
            _cache = {row["task"]: row for row in result.data or []}
        except Exception as e:
            print(f"⚠️ Failed to load ai_model_config, using cached/default config: {e}")
        # Also throttles retries while the table is unreachable
        _cache_loaded_at = time.monotonic()


def get_task_config(task: str, defaults: Dict[str, Any] = DEFAULT_CONFIG) -> Dict[str, Any]:
    """
    Return the config for `task`. A row replaces `defaults` entirely; NULL columns
    mean "not set" (a NULL fallback_model disables the fallback retry).
    """
    _refresh_cache_if_stale()
    row = _cache.get(task)
    if row is None:
        return dict(defaults)
    return {field: row.get(field) for field in CONFIG_FIELDS}


def _call_with_fallback(task: str, config: Dict[str, Any], call: Callable[[str], T]) -> T:
    """Run `call(model)`; if OpenAI rejects it (400/404), retry once on `fallback_model`."""
    model = config["model"]
    try:
        return call(model)
    except (BadRequestError, NotFoundError) as e:
        fallback = config["fallback_model"]
        if not fallback or fallback == model:
            raise
        print(f"   ⚠️ {task}: model {model} rejected ({e}); retrying with {fallback}")
        return call(fallback)


def create_response(
    client: OpenAI,
    task: str,
    input: list,
    default_verbosity: Optional[str] = None,
):
    """
    Call the Responses API with the model/effort/verbosity configured for `task`.

    `default_verbosity` is used when the task's row leaves verbosity NULL.
    A NULL reasoning_effort omits `reasoning` (for models without reasoning support).
    """
    config = get_task_config(task)
    effort = config["reasoning_effort"]
    verbosity = config["verbosity"] or default_verbosity

    def call(model: str):
        kwargs: Dict[str, Any] = {"model": model, "input": input}
        if effort:
            kwargs["reasoning"] = {"effort": effort}
        if verbosity:
            kwargs["text"] = {"verbosity": verbosity}
        print(f"   🤖 {task}: model={model} effort={effort}")
        return client.responses.create(**kwargs)

    return _call_with_fallback(task, config, call)


def create_transcription(client: OpenAI, file: io.BytesIO, language: Optional[str] = None) -> str:
    """Transcribe `file` with the model configured for the 'transcription' task."""
    config = get_task_config("transcription", TRANSCRIPTION_DEFAULT_CONFIG)

    def call(model: str) -> str:
        file.seek(0)
        kwargs: Dict[str, Any] = {"model": model, "file": file}
        if language:
            kwargs["language"] = language
        return client.audio.transcriptions.create(**kwargs).text

    return _call_with_fallback("transcription", config, call)
