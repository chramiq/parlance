"""Pair-profile management: the stored document that seeds card generation.

The profile lives in the store meta table and is rendered into the agent
instructions on every run; parlance init creates it with the LLM, this
module lets the user read and refine it directly, without a model in the
loop.
"""

import json

import click

from . import store
from .agent import PairProfile


def show(conn) -> str:
    """The stored profile as pretty JSON, or a hint to create one."""
    profile = store.meta_get(conn, "pair_profile")
    if profile is None:
        return "no pair profile yet; run parlance init to create one"
    return json.dumps(profile, ensure_ascii=False, indent=1)


def edit(conn) -> str:
    """Open the editor on the profile, validate it, store the result.

    Raises:
        ValueError: when the edited text is not JSON or misses profile fields.

    Returns:
        Status line for the CLI.
    """
    profile = store.meta_get(conn, "pair_profile")
    if profile is None:
        raise ValueError("no pair profile yet; run parlance init to create one")
    text = click.edit(json.dumps(profile, ensure_ascii=False, indent=1), extension=".json")
    if text is None:
        return "unchanged (editor closed without a save)"
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(f"not valid JSON: {e}") from e
    PairProfile.model_validate(data)
    store.meta_set(conn, "pair_profile", data)
    return "saved"
