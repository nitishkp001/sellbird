"""sellbird verify: re-check addresses that were only guesses.

Everything here is invented (example.com style).
"""
from types import SimpleNamespace

import pytest
import pytest_asyncio

from sellbird import partnerships as p
from sellbird.models import Company
from sellbird.models.prospect import Prospect
from sellbird.reverify import candidates, reverify, verifier_configured
from sellbird.state import StateManager
from tests.test_partnerships import row


@pytest_asyncio.fixture
async def state(tmp_path):
    s = StateManager(str(tmp_path / "v.db"))
    await s.init_db()
    return s


async def add(state, email, status="guess", prospect_status="new", company_id=""):
    pid = await state.add_prospect(Prospect(
        first_name="Ana", last_name="Doe", company="Acme", company_id=company_id,
        email=email, email_status=status, status=prospect_status))
    return await state.get_prospect(pid)


def test_a_verifier_must_have_a_key():
    assert not verifier_configured(SimpleNamespace(reoon_api_key="", zerobounce_api_key="", hunter_api_key=""))
    assert verifier_configured(SimpleNamespace(reoon_api_key="k", zerobounce_api_key="", hunter_api_key=""))
    assert verifier_configured(SimpleNamespace(reoon_api_key="", zerobounce_api_key="", hunter_api_key="k"))


@pytest.mark.asyncio
async def test_only_guesses_still_in_line_are_rechecked(state):
    guess = await add(state, "a@one.example")
    queued = await add(state, "b@two.example", prospect_status="queued")
    await add(state, "c@three.example", status="verified")              # already firm
    await add(state, "d@four.example", prospect_status="replied")      # already past cold email
    suppressed = await add(state, "e@five.example")
    await state.add_suppression("address", "e@five.example", "manual")
    got = {x.email for x in await candidates(state)}
    assert got == {guess.email, queued.email} and suppressed.email not in got


@pytest.mark.asyncio
async def test_partnership_contacts_are_left_out_unless_asked(state):
    await p.PartnershipStore(state).import_records([row()])
    studio = await state.get_company_by_domain("studio.example")
    await add(state, "sam@studio.example", company_id=studio.id)
    await add(state, "ana@direct.example")
    assert [x.email for x in await candidates(state)] == ["ana@direct.example"]
    assert {x.email for x in await candidates(state, include_partnership=True)} >= {"sam@studio.example"}


@pytest.mark.asyncio
async def test_status_and_limit_choose_what_is_checked(state):
    await add(state, "a@x.example")
    await add(state, "b@x.example")
    await add(state, "c@x.example", status="risky")
    assert len(await candidates(state, limit=1)) == 1
    assert [x.email for x in await candidates(state, status="risky")] == ["c@x.example"]


@pytest.mark.asyncio
async def test_a_firm_verdict_is_saved_and_an_unsure_one_changes_nothing(state):
    a = await add(state, "a@x.example")
    b = await add(state, "b@x.example")
    c = await add(state, "c@x.example")
    d = await add(state, "d@x.example")
    verdicts = {a.email: "verified", b.email: "risky", c.email: "invalid", d.email: "guess"}

    async def fake(email, env):
        return verdicts[email]

    counts = await reverify(state, None, [a, b, c, d], verify=fake)
    assert dict(counts) == {"verified": 1, "risky": 1, "invalid": 1, "guess": 1}
    status = {x.email: (await state.get_prospect(x.id)).email_status for x in (a, b, c, d)}
    assert status == verdicts | {d.email: "guess"}


@pytest.mark.asyncio
async def test_a_failing_check_is_counted_and_never_stops_the_rest(state):
    a = await add(state, "a@x.example")
    b = await add(state, "b@x.example")

    async def fake(email, env):
        if email == a.email:
            raise RuntimeError("verifier down")
        return "verified"

    counts = await reverify(state, None, [a, b], verify=fake)
    assert dict(counts) == {"error": 1, "verified": 1}
    assert (await state.get_prospect(a.id)).email_status == "guess"
    assert (await state.get_prospect(b.id)).email_status == "verified"


@pytest.mark.asyncio
async def test_a_risky_address_can_be_upgraded_but_a_guess_never_downgrades_it(state):
    r = await add(state, "r@x.example", status="risky")

    async def unsure(email, env):
        return "guess"

    await reverify(state, None, [r], verify=unsure)
    assert (await state.get_prospect(r.id)).email_status == "risky"

    async def sure(email, env):
        return "verified"

    await reverify(state, None, [r], verify=sure)
    assert (await state.get_prospect(r.id)).email_status == "verified"
