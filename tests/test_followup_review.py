"""LLM review of follow-up emails (steps 2 and 3).

Follow-ups are one template for a whole batch, with merge variables, so they are reviewed once
per campaign against what is known about the sender and the offer, and each step carries its
verdict into the Outbox rows the Sender stages from it.

Everything here is invented (example.com style).
"""
import json
import os
import tempfile
from types import SimpleNamespace

import pytest
import pytest_asyncio

from sellbird.agents.writer import Writer
from sellbird.config import ComplianceConfig
from sellbird.draft_review import acceptable_revision, _prompt, review_draft
from sellbird.models.campaign import Campaign, EmailStep
from sellbird.models.prospect import Prospect
from sellbird.state import StateManager
from tests.test_outbox_native import FakeProvider, make_sender, seed_prospect

STEP1 = {"step": 1, "subject": "first", "body": "Hi {{first_name}}, one question about {{company}}?\n\nSam", "delay_days": 0}
STEP2 = {"step": 2, "subject": "another angle", "body": "Hi {{first_name}}, we helped a client cut costs 40%.\n\nSam", "delay_days": 3}
STEP3 = {"step": 3, "subject": "last note", "body": "Hi {{first_name}}, closing the loop.\n\nSam", "delay_days": 4}
FIXED2 = {"subject": "another angle", "body": "Hi {{first_name}}, here is a different angle.\n\nSam"}


class ScriptedBrain:
    def __init__(self, *replies):
        self.replies, self.calls = list(replies), []

    def load_skills_for_agent(self, name):
        return ""

    def load_prompt(self, name, **kw):
        return "prompt"

    async def think_json(self, prompt, **kw):
        self.calls.append(kw.get("task", ""))
        return self.replies.pop(0) if self.replies else None


def config(review=True):
    return SimpleNamespace(
        channels=SimpleNamespace(email=SimpleNamespace(provider="gmail", require_approval=True)),
        product=SimpleNamespace(name="P", description="fixed-scope projects", key_benefits=["milestone pricing"],
                                pricing="quoted per milestone"),
        persona=SimpleNamespace(name="Sam", company="Example Studio", role="Founder", tone="plain"),
        writer=SimpleNamespace(humanizer=False, email_rules="x", research=False, review=review, review_rounds=2),
        compliance=ComplianceConfig(),
    )


@pytest_asyncio.fixture
async def state(tmp_path):
    s = StateManager(str(tmp_path / "f.db"))
    await s.init_db()
    return s


async def prospects(state):
    pid = await state.add_prospect(Prospect(
        first_name="Ana", last_name="Doe", title="Owner", company="Acme Co", industry="Retail",
        email="ana@acme.example", email_status="verified"))
    return [await state.get_prospect(pid)]


def writer(brain, state, review=True):
    w = Writer(brain, state, config(review))
    w.skills, w.email_rules = "", "Operator rules: keep it short."     # operator rules: no built-in shape gate
    return w


# ── the reviewer ────────────────────────────────────────────────────────────

def test_a_fix_may_not_drop_or_invent_a_merge_variable():
    original = {"subject": "s", "body": "Hi {{first_name}}, about {{company}}."}
    assert acceptable_revision(original, {"subject": "s", "body": "Hi {{first_name}}, re {{company}}."})
    assert not acceptable_revision(original, {"subject": "s", "body": "Hi Ana, about {{company}}."})
    assert not acceptable_revision(original, {"subject": "s", "body": "Hi {{first_name}}, {{title}} at {{company}}."})


def test_the_follow_up_prompt_protects_placeholders_and_proof():
    text = _prompt({"subject": "s", "body": "b"}, ["- Sender: Sam"], "rules", kind="follow_up", step=2)
    assert "follow-up email 2" in text and "placeholder" in text and "case studies" in text
    assert "ONE first email" not in text


# ── the Writer ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_follow_ups_are_reviewed_and_email_one_is_not(state):
    brain = ScriptedBrain(
        [STEP1, STEP2, STEP3],
        {"verdict": "revise", "issues": ["Invented a 40% result"], "revised": FIXED2},
        {"verdict": "pass", "issues": [], "revised": None},        # re-check of step 2
        {"verdict": "pass", "issues": [], "revised": None},        # step 3
    )
    steps = await writer(brain, state)._write_sequence(await prospects(state))
    assert [s.step for s in steps] == [1, 2, 3]
    assert brain.calls.count("draft_review") == 3 and brain.calls[0] == "write_sequence"
    assert (steps[0].review_verdict, steps[0].review_notes) == ("", "")
    assert steps[1].review_verdict == "revised" and "40%" not in steps[1].body
    assert json.loads(steps[1].review_notes)["fixed"] == ["Invented a 40% result"]
    assert steps[2].review_verdict == "passed"


@pytest.mark.asyncio
async def test_follow_up_review_is_off_unless_writer_review_is_on(state):
    brain = ScriptedBrain([STEP1, STEP2, STEP3])
    steps = await writer(brain, state, review=False)._write_sequence(await prospects(state))
    assert brain.calls == ["write_sequence"] and all(s.review_verdict == "" for s in steps)


@pytest.mark.asyncio
async def test_a_silent_reviewer_leaves_the_template_and_says_unreviewed(state):
    brain = ScriptedBrain([STEP1, STEP2, STEP3])                    # the reviewer never answers
    steps = await writer(brain, state)._write_sequence(await prospects(state))
    assert steps[1].body == STEP2["body"] and steps[1].review_verdict == "unreviewed"


# ── the Sender ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_staged_follow_ups_carry_their_verdict_into_the_outbox(state):
    pid = await seed_prospect(state)
    notes = json.dumps({"fixed": ["Removed an invented result"], "issues": [], "rounds": 2})
    campaign = Campaign(id="", name="c", channel="email", prospect_ids=[pid], status="draft", sequence=[
        EmailStep(step=1, subject="hi {{first_name}}", body="Body for {{company}}.", delay_days=0),
        EmailStep(step=2, subject="again", body="Another angle {{first_name}}.", delay_days=3,
                  review_verdict="revised", review_notes=notes)])
    campaign.id = await state.add_campaign(campaign)
    await make_sender(state, FakeProvider())._run_native()
    rows = {i["step"]: i for i in await state.get_outbox(status="pending_review")}
    assert rows[2]["review_verdict"] == "revised" and json.loads(rows[2]["review_notes"])["fixed"]
    assert rows[1]["review_verdict"] == ""                          # email 1 is reviewed per person, elsewhere
