"""LLM review of a drafted email: check it against the facts and the rules, fix it.

The Writer drafts from a company brief. This is the second pair of eyes before
a person looks at it: one brain call that reads the draft next to the FACTS and
the operator's rules, says what is wrong (an unsupported claim, a broken rule,
the wrong framing for who it is going to, a templated feel), and returns a
corrected version. A fix is applied only if it stays plain, short and link-free;
it is then checked again, up to ``rounds`` times.

The review never decides whether an email is sent. Every draft still goes to the
Outbox for a person, with the verdict and any open issues next to it.

Verdicts:
  passed      the first check found nothing
  revised     problems were found and fixed; the last check passed
  flagged     problems remain (no usable fix, or still failing after the last round)
  unreviewed  the brain gave no usable answer, so nobody has checked this draft
"""

from __future__ import annotations

import json
import logging
import re

from sellbird.untrusted import UNTRUSTED_RULE, fence

logger = logging.getLogger("sellbird.draft_review")

MAX_GROWTH_WORDS = 20
META_PREFIXES = ("i can't", "i cannot", "as an ai", "sorry,", "here is", "here's")
LINK = re.compile(r"https?://|www\.", re.IGNORECASE)

DEFAULT_RULES = """- Plain text, no formatting. No em dashes, no exclamation marks, at most one question.
- Email 1 under 75 words. Do not start with "I" or "We". No links.
- No hype or filler: "game-changer", "unlock", "leverage", "I'd love to", "just wanted to", "hope you're well".
- Do not offer free anything, guarantees, discounts or invented prices."""


def _words(text: str) -> int:
    return len(str(text or "").split())


def acceptable_revision(original: dict, revised) -> bool:
    """A fix is applied only when it is still an email, no longer by much, and adds no link."""
    if not isinstance(revised, dict):
        return False
    subject = str(revised.get("subject") or "").strip()
    body = str(revised.get("body") or "").strip()
    if not subject or not body or len(body) > 2000:
        return False
    if body.lower().startswith(META_PREFIXES):
        return False
    if "—" in subject + body:
        return False
    if LINK.search(subject + body) and not LINK.search(original["subject"] + original["body"]):
        return False
    return _words(body) <= _words(original["body"]) + MAX_GROWTH_WORDS


def _prompt(draft: dict, facts: list[str], rules: str) -> str:
    return f"""You are the reviewer for a cold email before a person reads it. Be strict and
specific. You are checking ONE draft.

FACTS: everything that is known about the recipient and their company. The email may state
only what these support:
{fence(chr(10).join(facts), "prospect research")}

{UNTRUSTED_RULE}

RULES the email must follow:
{rules}

THE DRAFT:
Subject: {draft['subject']}

{draft['body']}

Check, in this order:
1. GROUNDING: does every claim about the recipient or their company appear in the FACTS?
   An invented, stretched or flattering claim is an issue.
2. RULES: does it break any rule above (times, dates, links, length, more than one question,
   banned phrases, wrong sign-off)?
3. FIT: is it framed for the kind of company this is (an agency is offered overflow capacity,
   not sold development), and does it open with something specific rather than a template line?
4. VOICE: does it read like one person typed it? Flag flattery, stacked buzzwords and
   "not X but Y" constructions.

Return ONLY JSON:
{{"verdict": "pass" or "revise",
  "issues": ["one short sentence each, empty when verdict is pass"],
  "revised": {{"subject": "...", "body": "..."}} or null}}
When you revise, fix only what you listed, keep it as short as before, add no links, and invent
no facts. If the FACTS give nothing specific to open with, keep the opening plain instead of
making something up."""


def _parse(result) -> tuple[str, list[str], dict | None] | None:
    if not isinstance(result, dict):
        return None
    verdict = str(result.get("verdict") or "").strip().lower()
    if verdict not in ("pass", "revise"):
        return None
    issues = [re.sub(r"\s+", " ", str(i)).strip()[:300]
              for i in (result.get("issues") or []) if isinstance(i, (str, int, float))]
    issues = [i for i in issues if i][:6]
    revised = result.get("revised")
    return verdict, issues, revised if isinstance(revised, dict) else None


async def review_draft(brain, draft: dict, facts: list[str], *, rules: str = "",
                       rounds: int = 2) -> tuple[dict, str, dict]:
    """Review ``draft`` ({"subject", "body"}). Returns (final draft, verdict, notes)
    where notes is {"fixed": [...], "issues": [...], "rounds": n}."""
    current = dict(draft)
    fixed: list[str] = []
    open_issues: list[str] = []
    verdict = "unreviewed"
    done = 0
    for _ in range(max(1, rounds)):
        try:
            parsed = _parse(await brain.think_json(
                _prompt(current, facts, rules.strip() or DEFAULT_RULES),
                session_id="sellbird-review", agent="reviewer", task="draft_review"))
        except Exception as e:                       # a review failing must never lose the draft
            logger.warning(f"Review: call failed: {e}")
            parsed = None
        if parsed is None:
            if fixed:
                verdict, open_issues = "flagged", ["A fix was applied but couldn't be re-checked."]
            break
        done += 1
        said, issues, revised = parsed
        if said == "pass":
            verdict = "revised" if fixed else "passed"
            open_issues = []
            break
        open_issues = issues or ["The reviewer asked for a change without saying what."]
        if not acceptable_revision(current, revised):
            verdict = "flagged"
            break
        fixed.extend(i for i in issues if i not in fixed)
        current = {"subject": str(revised["subject"]).strip()[:120],
                   "body": str(revised["body"]).strip()[:2000]}
        verdict = "flagged"          # until the fix is checked and passes
    return current, verdict, {"fixed": fixed, "issues": open_issues, "rounds": done}


def notes_json(notes: dict) -> str:
    return json.dumps(notes, ensure_ascii=False)
