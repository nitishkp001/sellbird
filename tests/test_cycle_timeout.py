"""A slow agent must not make the rest of the cycle look unfinished.

Sellbird holds every send when the reply check "couldn't finish". When the Writer (slow with
a local model) ran past the cycle limit, the whole batch was cancelled, including a reply
check that had already finished, so sending was held every cycle. Now only the agents still
running are cut off, and the Writer saves each email as it finishes.

Everything here is invented (example.com style).
"""
import asyncio

import pytest

import sellbird.main as main
from sellbird.models.campaign import Campaign
from tests.test_research_review import GOOD, ScriptedBrain, config, seed
from sellbird.agents.writer import Writer


@pytest.mark.asyncio
async def test_a_finished_agent_keeps_its_outcome_when_another_runs_too_long(monkeypatch):
    monkeypatch.setattr(main, "CYCLE_TIMEOUT_SECONDS", 0.2)
    cancelled = []

    async def replies():
        return "no new replies"

    async def writer():
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancelled.append(True)
            raise

    outcomes = await main._run_agents([("handle_replies", replies()), ("write_campaign", writer())])
    assert outcomes == {"handle_replies": "no new replies"}        # replies still count as read
    assert cancelled == [True]                                     # the slow one was stopped


@pytest.mark.asyncio
async def test_an_agent_that_raises_is_reported_and_the_others_still_finish():
    async def boom():
        raise RuntimeError("provider down")

    async def fine():
        return 3

    outcomes = await main._run_agents([("a", boom()), ("b", fine())])
    assert isinstance(outcomes["a"], RuntimeError) and outcomes["b"] == 3
    assert await main._run_agents([]) == {}


@pytest.mark.asyncio
async def test_each_draft_is_saved_as_it_finishes_so_a_cut_off_cycle_keeps_the_earlier_ones(tmp_path):
    from sellbird.state import StateManager
    state = StateManager(str(tmp_path / "t.db"))
    await state.init_db()
    cid1, first = await seed(state, name="Acme One", domain="one.example")
    cid2, second = await seed(state, name="Acme Two", domain="two.example")
    for cid in (cid1, cid2):
        await state.set_company_research(cid, {**GOOD, "usable": True})
    campaign_id = await state.add_campaign(Campaign(
        id="", name="x", channel="email", sequence=[], prospect_ids=[first.id, second.id], status="draft"))

    class CutOff(ScriptedBrain):
        async def think_json(self, prompt, **kw):
            if not self.replies:                    # the cycle's time ran out during the second draft
                raise asyncio.CancelledError()
            return await super().think_json(prompt, **kw)

    brain = CutOff({"subject": "one", "body": "Hi Ana,\n\nProofing cabinets need ordering. Portal?\n\nNitish"},
                   {"verdict": "pass", "issues": [], "revised": None})
    w = Writer(brain, state, config())
    w.skills, w.email_rules = "", ""
    with pytest.raises(asyncio.CancelledError):
        await w._personalize_first_emails(campaign_id, [first, second])

    staged = await state.get_outbox(status="pending_review")
    assert [i["to_email"] for i in staged] == [first.email]
    assert (await state.get_prospect(first.id)).status == "queued"      # saved: marked as done
    assert (await state.get_prospect(second.id)).status == "new"        # not stranded: tried next cycle
