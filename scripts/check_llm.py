"""Verify the configured LLM backend end to end, before a race depends on it.

The rest of this repo is offline by construction; this script is the deliberate
exception. It makes exactly one tiny real API call so that a wrong base URL, a
wrong credential or a wrong model id fails **here**, with the backend's own error
message, rather than as a silent quiet radio on lap 5.

    python scripts/check_llm.py
    python scripts/check_llm.py --model kimi-for-coding   # try an id without editing .env

It is not part of the pytest suite and nothing imports it.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from pydantic import BaseModel

from rtv import llm


class _Probe(BaseModel):
    """Deliberately trivial: this checks the transport, not the model's judgement."""

    ok: bool
    one_word: str


def _print_config() -> dict:
    payload = llm.describe()
    print("Backend configuration")
    print("---------------------")
    for key, value in payload.items():
        print(f"  {key:<20} {value}")
    return payload


async def _probe(model: str) -> None:
    from rtv.pitwall.provider import KimiProvider

    provider = KimiProvider()
    response = await provider.complete(
        agent="check_llm",
        model=model,
        system=[{"type": "text", "text": "You are a connectivity probe."}],
        messages=[
            {
                "role": "user",
                "content": "Reply with ok=true and one_word set to the word 'green'.",
            }
        ],
        tools=(),
        output_model=_Probe,
        max_tokens=200,
    )
    if response.output is None:
        print()
        print("REACHED THE BACKEND, but it did not return a valid structured answer.")
        print(f"  raw text: {response.text[:400]!r}")
        print("  The model did not call the forced respond tool with a valid payload.")
        raise SystemExit(2)
    print()
    print(f"OK - {model} answered: {response.output.model_dump()}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default="", help="Override the model id for this probe.")
    args = parser.parse_args()

    try:
        payload = _print_config()
    except llm.LLMConfigError as exc:
        print(f"Configuration error: {exc}")
        return 1

    if not payload["key_present"]:
        print()
        print(f"No credential found in {payload['key_env']}. Nothing to probe.")
        print("The deterministic engine, replay, UI and tests are unaffected by this.")
        return 1

    model = llm.model("reasoning", args.model)
    print()
    print(f"Probing model {model!r}...")

    try:
        asyncio.run(_probe(model))
    except SystemExit:
        raise
    except Exception as exc:  # the backend's own message is the useful part
        print()
        print(f"FAILED: {type(exc).__name__}: {exc}")
        print()
        print("Common causes:")
        print("  - wrong model id for this endpoint (try --model with another id)")
        print("  - wrong base URL: the Kimi Code subscription is https://api.kimi.com/coding")
        print(f"  - credential in {payload['key_env']} not valid for that endpoint")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
