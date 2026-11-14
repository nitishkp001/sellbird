"""Re-check addresses that were only guesses.

The Scout verifies one candidate address when it finds a person, so an address
found before a verifier was set up (or while the verifier was out of credit)
stays a "guess" and is never sent. This runs the configured verifier over those
addresses again. ``sellbird verify`` is its command.

Only a firm answer is saved (verified, risky or invalid). An inconclusive check
leaves the address as it was, so a re-check can never make one worse.
"""

from __future__ import annotations

import asyncio
from collections import Counter

from sellbird.integrations.email_finder import verify_published_address

# A verdict worth saving. "guess" means the verifier could not say.
FIRM = ("verified", "risky", "invalid")
PARALLEL = 4


def verifier_configured(env) -> bool:
    """True when at least one email verifier has a key."""
    return any(getattr(env, key, "") for key in
               ("reoon_api_key", "zerobounce_api_key", "hunter_api_key"))


async def candidates(state, *, status: str = "guess", include_partnership: bool = False,
                     limit: int = 100) -> list:
    """Prospects to re-check: ``status`` emails on people still in line for cold email.

    Partnership contacts are written to one to one from Partnerships, and a
    suppressed address is never touched."""
    found = []
    for prospect_status in ("new", "queued"):
        for p in await state.get_prospects_by_status(prospect_status):
            if not p.email or (p.email_status or "guess") != status:
                continue
            if not include_partnership and await state.company_in_partnership_track(p.company_id):
                continue
            if await state.is_suppressed(p.email):
                continue
            found.append(p)
    return found[:max(0, limit)]


async def reverify(state, env, prospects, *, verify=verify_published_address) -> Counter:
    """Verify each prospect's address and save a firm verdict. Counts by outcome:
    verified, risky, invalid, guess (still inconclusive) and error."""
    gate = asyncio.Semaphore(PARALLEL)
    counts: Counter = Counter()

    async def one(p):
        async with gate:
            try:
                verdict = await verify(p.email, env)
            except Exception:
                counts["error"] += 1
                return
            counts[verdict if verdict in FIRM else "guess"] += 1
            if verdict in FIRM and verdict != (p.email_status or "guess"):
                await state.update_prospect_email(p.id, p.email, verdict)

    await asyncio.gather(*(one(p) for p in prospects))
    return counts
