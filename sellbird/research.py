"""Company research: read a business's own site and boil it down to a brief.

A first email is only as specific as what Sellbird knows about the company.
Research reads the company's own pages (home, about, services; at most four,
fetched the same safe way profiling does), adds what Sellbird already stored
(description, location, notes, detected tools), and asks the brain for a short
brief: what the company does, where, and a few specific details that are
actually written on its pages. The brief is saved on the company, so it is
paid for once and shown beside the draft when a person reviews it.

A company is *usable* only when the brief holds at least one specific, checkable
detail and the brain is not guessing (confidence above "low"). The Writer
leaves everyone else alone instead of sending a generic email.

Everything read from a website is fenced as untrusted text.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin, urlparse

import httpx

from sellbird.collectors.profile import _text, looks_like_challenge
from sellbird.public_http import public_http_client
from sellbird.untrusted import UNTRUSTED_RULE, fence

logger = logging.getLogger("sellbird.research")

PAGE_PATHS = ("", "/about", "/about-us", "/company", "/services", "/what-we-do")
MAX_PAGES = 4
PAGE_CHARS = 3500
NOTES_CHARS = 1500
BRIEF_MAX_AGE = timedelta(days=30)
CONFIDENCE = ("high", "medium", "low")
RECIPIENT_TYPES = (
    "agency", "product_company", "investor_or_accelerator", "marketplace_or_platform", "other",
)
USER_AGENT = "Mozilla/5.0 (compatible; SellbirdResearch/1.0)"


def _clip(value, limit: int) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


async def fetch_site_pages(company) -> list[tuple[str, str]]:
    """(url, text) for the company's readable pages. Never raises."""
    base = (company.website or "").strip() or (
        f"https://{company.domain}" if company.domain else "")
    if not base:
        return []
    if not re.match(r"^https?://", base, re.I):
        base = "https://" + base
    pages: list[tuple[str, str]] = []
    seen: set[str] = set()
    try:
        async with public_http_client(
            timeout=12.0,
            headers={"User-Agent": USER_AGENT, "Accept": "text/html,*/*;q=0.8"},
        ) as client:
            for path in PAGE_PATHS:
                if len(pages) >= MAX_PAGES:
                    break
                url = urljoin(base if base.endswith("/") else base + "/", path.lstrip("/"))
                try:
                    resp = await client.get(url)
                except (httpx.HTTPError, ValueError) as e:
                    logger.debug(f"research fetch failed {url}: {e}")
                    continue
                final = str(resp.url)
                if resp.status_code != 200 or final in seen:
                    continue
                if "html" not in resp.headers.get("content-type", "").lower():
                    continue
                if looks_like_challenge(resp.text or "", resp.status_code):
                    logger.info(f"research: {urlparse(url).netloc} answered with a bot challenge")
                    break           # every page on the host will do the same
                text = _text(resp.text or "").strip()
                if len(text) < 80:
                    continue
                seen.add(final)
                pages.append((final, text[:PAGE_CHARS]))
    except Exception as e:                                  # research must never stop the Writer
        logger.warning(f"research: couldn't read {base}: {e}")
    return pages


def _fresh(checked_at) -> bool:
    if not checked_at:
        return False
    try:
        when = datetime.fromisoformat(str(checked_at).replace("Z", ""))
    except ValueError:
        return False
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    return now - when < BRIEF_MAX_AGE


def _strings(value, count: int, limit: int) -> list[str]:
    if not isinstance(value, list):
        return []
    out = [_clip(v, limit) for v in value if isinstance(v, (str, int, float))]
    return [v for v in out if v][:count]


def clean_brief(raw, sources: list[str]) -> dict | None:
    """The brain's reply as a brief we trust the shape of, or None."""
    if not isinstance(raw, dict):
        return None
    confidence = str(raw.get("confidence") or "").lower().strip()
    if confidence not in CONFIDENCE:
        confidence = "low"
    recipient = str(raw.get("recipient_type") or "").lower().strip()
    if recipient not in RECIPIENT_TYPES:
        recipient = "other"
    brief = {
        "company_name": _clip(raw.get("company_name"), 80),
        "summary": _clip(raw.get("summary"), 400),
        "offerings": _strings(raw.get("offerings"), 6, 120),
        "location": _clip(raw.get("location"), 80),
        "audience": _clip(raw.get("audience"), 200),
        "recipient_type": recipient,
        "hooks": _strings(raw.get("hooks"), 5, 200),
        "software_fit": _clip(raw.get("software_fit"), 300),
        "unknowns": _strings(raw.get("unknowns"), 4, 120),
        "confidence": confidence,
        "sources": sources[:MAX_PAGES],
    }
    brief["usable"] = bool(brief["hooks"]) and confidence != "low"
    if not brief["usable"]:
        brief["reason"] = ("nothing specific and checkable to say about them"
                           if confidence != "low" else "the brain wasn't confident in what it read")
    return brief


def unusable_brief(reason: str) -> dict:
    return {"company_name": "", "summary": "", "offerings": [], "location": "", "audience": "",
            "recipient_type": "other", "hooks": [], "software_fit": "", "unknowns": [],
            "confidence": "low", "sources": [], "usable": False, "reason": reason}


def _prompt(company, pages: list[tuple[str, str]], offer: str) -> str:
    source_lines = []
    if company.description:
        source_lines.append(f"Description we stored: {_clip(company.description, 500)}")
    if company.location:
        source_lines.append(f"Location we stored: {_clip(company.location, 120)}")
    if company.tech_stack:
        source_lines.append(f"Tools detected on their site: {', '.join(company.tech_stack[:8])}")
    if company.notes:
        source_lines.append(f"Our notes: {_clip(company.notes, NOTES_CHARS)}")
    for url, text in pages:
        source_lines.append(f"Page {url}:\n{text}")
    return f"""You are researching ONE company so that someone can write a specific,
honest first email to a person there. You are not writing the email.

What the sender offers (only so you can judge fit): {offer or "fixed-scope freelance software projects"}

SOURCES. Everything you may say about the company comes from here:
{fence(chr(10).join(source_lines), "company research")}

{UNTRUSTED_RULE}

Return ONLY JSON with these keys:
- "company_name": how the company names itself on its pages, or "" if you can't tell
- "summary": one or two sentences on what it does
- "offerings": up to 6 short items, the products or services it sells
- "location": city or country if the sources say so, else ""
- "audience": who it sells to, if the sources say so, else ""
- "recipient_type": one of {list(RECIPIENT_TYPES)}
- "hooks": up to 5 specific, checkable details written in the sources (a named product,
  a service line, a customer type, a location, something it recently launched). Each under
  25 words. Not praise, not generic industry talk.
- "software_fit": one sentence on why this company might buy fixed-scope software projects
  or partner with a freelance studio, based on the sources; "unclear" if they don't say
- "unknowns": up to 4 things the sources don't tell us that would matter
- "confidence": "high", "medium" or "low": how well the sources support the above

Rules: never guess size, funding, revenue, customers or headcount. If the sources are thin
or look like an error or bot-check page, say so with confidence "low" and no hooks."""


async def research_company(brain, state, company, *, offer: str = "", force: bool = False,
                           fetch=fetch_site_pages) -> dict | None:
    """The company's brief: from the saved copy if it is under 30 days old,
    otherwise freshly researched and saved. None when the brain couldn't answer
    (nothing is saved then, so the next cycle tries again)."""
    if not force and company.id:
        brief, checked_at = await state.get_company_research(company.id)
        if brief is not None and _fresh(checked_at):
            return brief

    pages = await fetch(company)
    if not pages and not (company.description or company.notes):
        brief = unusable_brief("no readable website and nothing stored about them")
        if company.id:
            await state.set_company_research(company.id, brief)
        return brief

    result = await brain.think_json(
        _prompt(company, pages, offer), session_id="sellbird-research",
        agent="researcher", task="company_brief",
    )
    brief = clean_brief(result, [url for url, _ in pages])
    if brief is None:
        return None
    if company.id:
        await state.set_company_research(company.id, brief)
    return brief


def brief_facts(brief: dict | None) -> list[str]:
    """A brief as FACTS lines for the Writer's prompt."""
    if not brief:
        return []
    facts = []
    if brief.get("summary"):
        facts.append(f"- What they do: {brief['summary']}")
    if brief.get("offerings"):
        facts.append(f"- What they sell: {'; '.join(brief['offerings'])}")
    if brief.get("location"):
        facts.append(f"- Based in: {brief['location']}")
    if brief.get("audience"):
        facts.append(f"- Who they sell to: {brief['audience']}")
    if brief.get("recipient_type") and brief["recipient_type"] != "other":
        facts.append(f"- Kind of company: {brief['recipient_type'].replace('_', ' ')}")
    for hook in brief.get("hooks", []):
        facts.append(f"- Specific detail from their own pages: {hook}")
    if brief.get("software_fit") and brief["software_fit"].lower() != "unclear":
        facts.append(f"- Possible fit (a judgement, not something they said): {brief['software_fit']}")
    return facts
