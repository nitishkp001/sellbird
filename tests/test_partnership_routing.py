"""One company, one track: the cold-email pipeline leaves partnership companies
alone, and Companies can move a company into Partnerships."""

import asyncio
from types import SimpleNamespace

import pytest
import pytest_asyncio
from fastapi import HTTPException
from fastapi.testclient import TestClient

import sellbird.config as sellbird_config
import sellbird.dashboard as dash
from sellbird import partnerships as p
from sellbird.agents.writer import Writer
from sellbird.register import build_register
from sellbird.models.company import Company
from sellbird.models.prospect import Prospect
from sellbird.state import StateManager
from tests.test_outbox_native import (  # noqa: F401  (fixture)
    FakeProvider, _now_iso, make_sender, seed_campaign, state,
)
from tests.test_writer_retry import CONFIG as WRITER_CONFIG, GOOD, FakeBrain

ANGLE_NOTES = ("Role Match: Founder.\nProduct Fit: Builds booking tools.\n"
               "Acme Angle: Fixed-scope sprint for a payments API.")


@pytest_asyncio.fixture
async def store(state):  # noqa: F811
    return p.PartnershipStore(state)


async def company(state, name="Tracked Studio", domain="tracked.example", **fields):
    return await state.add_company(Company(name=name, domain=domain, website=f"https://{domain}", **fields))


async def person(state, cid, first="Alex", email="alex@tracked.example", status="new",
                 estatus="verified", score=50, notes="", title="Founder"):
    return await state.add_prospect(Prospect(
        company_id=cid, first_name=first, last_name="Example" if first else "", title=title,
        company="Tracked Studio", email=email, email_status=estatus,
        email_verified=(estatus == "verified"), status=status, score=score,
        personalization_notes=notes, industry="Software",
    ))


async def outbox(state, pid, email, status="pending_review", kind="sequence", campaign="c1", step=1):
    return await state.add_outbox_item(
        prospect_id=pid, to_email=email, subject="s", body="b", send_at=_now_iso(),
        status=status, campaign_id=campaign, step=step, kind=kind)


async def item_status(state, item_id):
    [row] = [r for r in await state.get_outbox() if r["id"] == item_id]
    return row["status"], row.get("error") or ""


async def track(store, cid):
    """Put a company in the track the way Companies does."""
    return (await store.promote(cid))["id"]


# ── the cold-email pipeline leaves the company alone ──


@pytest.mark.asyncio
async def test_writer_skips_everyone_at_a_partnership_company(state, store):
    tracked, other = await company(state), await company(state, "Other Studio", "other.example")
    await person(state, tracked, "Alex", "alex@tracked.example", score=90)
    colleague = await person(state, tracked, "Sam", "sam@tracked.example")
    normal = await person(state, other, "Jo", "jo@other.example")
    await track(store, tracked)

    writer = Writer(FakeBrain(reply=GOOD), state, WRITER_CONFIG, None)
    await writer.run()

    assert (await state.get_prospect(normal)).status == "queued"
    assert (await state.get_prospect(colleague)).status == "new"    # not written for
    [campaign] = await state.get_campaigns_by_status("draft")
    assert campaign.prospect_ids == [normal]


@pytest.mark.asyncio
async def test_the_heartbeat_does_not_wait_on_prospects_it_will_never_write(state, store):
    tracked, other = await company(state), await company(state, "Other Studio", "other.example")
    await person(state, tracked, "Alex", "alex@tracked.example")
    await person(state, tracked, "Sam", "sam@tracked.example")
    await person(state, other, "Jo", "jo@other.example")
    assert await state.count_writable_prospects() == 3
    await track(store, tracked)
    assert await state.count_writable_prospects() == 1


@pytest.mark.asyncio
async def test_staging_leaves_partnership_companies_out_of_a_draft_campaign(state, store):
    tracked, other = await company(state), await company(state, "Other Studio", "other.example")
    await person(state, tracked, "Alex", "alex@tracked.example", score=90)   # becomes the account's contact
    colleague = await person(state, tracked, "Sam", "sam@tracked.example")    # stays 'new': only the guard stops them
    normal = await person(state, other, "Jo", "jo@other.example")
    await track(store, tracked)
    assert (await state.get_prospect(colleague)).status == "new"
    await seed_campaign(state, [colleague, normal])

    await make_sender(state, FakeProvider())._run_native()

    recipients = {i["to_email"] for i in await state.get_outbox()}
    assert recipients == {"jo@other.example"}


@pytest.mark.asyncio
async def test_send_time_check_cancels_cold_sequences_only(state, store):
    cid = await company(state)
    contact = await person(state, cid)
    colleague = await person(state, cid, "Sam", "sam@tracked.example")
    aid = await track(store, cid)

    cold = await state.get_outbox_item(await outbox(state, colleague, "sam@tracked.example", "approved"))
    reply = await state.get_outbox_item(await outbox(state, colleague, "sam@tracked.example", "approved",
                                                     kind="reply", campaign="", step=1))
    verdict = await p.send_verdict(state, cold)
    assert verdict[0] == "cancelled" and "partnership track" in verdict[1]
    assert await p.send_verdict(state, reply) is None       # they wrote to us: not cold

    # The track's own email is judged by the track's own rules, not this one.
    await state.update_prospect_email(contact, "alex@tracked.example", "verified")
    await store.update(aid, {"email_subject": "Hello", "email_body": "Hi Alex"})
    await store.set_setting("pilot", {**p.default_pilot(), "status": "active"})
    queued = await store.stage_email(aid)
    own = await state.get_outbox_item(queued["outbox_id"])
    own["approved_by"] = "you"
    assert await p.send_verdict(state, own) is None


@pytest.mark.asyncio
async def test_a_company_outside_the_track_is_untouched(state, store):
    cid = await company(state, "Other Studio", "other.example")
    pid = await person(state, cid, "Jo", "jo@other.example")
    item = await state.get_outbox_item(await outbox(state, pid, "jo@other.example", "approved"))
    assert await p.send_verdict(state, item) is None


# ── joining the track withdraws what was already drafted ──


@pytest.mark.asyncio
async def test_moving_a_company_cancels_waiting_cold_emails_and_nothing_else(state, store):
    cid = await company(state)
    contact = await person(state, cid)
    colleague = await person(state, cid, "Sam", "sam@tracked.example")
    waiting = await outbox(state, contact, "alex@tracked.example", "pending_review")
    approved = await outbox(state, colleague, "sam@tracked.example", "approved")
    sent = await outbox(state, colleague, "sam@tracked.example", "sent", step=2)
    answer = await outbox(state, colleague, "sam@tracked.example", "pending_review", kind="reply",
                          campaign="", step=1)
    await track(store, cid)

    for item in (waiting, approved):
        status, error = await item_status(state, item)
        assert status == "cancelled" and "partnership track" in error
    assert (await item_status(state, sent))[0] == "sent"
    assert (await item_status(state, answer))[0] == "pending_review"


@pytest.mark.asyncio
async def test_importing_a_company_already_in_the_pipeline_withdraws_its_drafts(state, store):
    cid = await company(state)
    pid = await person(state, cid)
    waiting = await outbox(state, pid, "alex@tracked.example")
    await store.import_records([{
        "id": "A01", "company": "Tracked Studio", "website": "https://tracked.example",
        "target_person": "Alex Example", "direct_email": "alex@tracked.example"}])
    assert (await item_status(state, waiting))[0] == "cancelled"
    assert (await state.get_prospect(pid)).status == "partnership"


@pytest.mark.asyncio
async def test_every_sender_cycle_sweeps_drafts_for_accounts_made_earlier(state, store):
    cid = await company(state)
    await person(state, cid)
    await track(store, cid)
    colleague = await person(state, cid, "Sam", "sam@tracked.example")
    late = await outbox(state, colleague, "sam@tracked.example", "pending_review")
    await make_sender(state, FakeProvider())._run_native()
    assert (await item_status(state, late))[0] == "cancelled"


# ── moving a company from the Companies tab ──


@pytest.mark.asyncio
async def test_promote_links_the_best_contact_and_keeps_its_verification(state, store):
    cid = await company(state, notes="Runs a design studio.", location="Example City")
    weak = await person(state, cid, "Sam", "sam@tracked.example", estatus="guess", score=20)
    best = await person(state, cid, "Alex", "alex@tracked.example", score=85, notes=ANGLE_NOTES)

    account = await store.promote(cid, fit={"fit": 85, "reasons": ["sector match", "decision maker"]})

    record = account["record"]
    assert account["prospect_id"] == best
    assert record["target_person"] == "Alex Example" and record["target_role"] == "Founder"
    assert record["direct_email"] == "alex@tracked.example"
    assert record["website"] == "https://tracked.example"
    assert "Fit 85/100: sector match · decision maker" in record["research_notes"]
    assert "Runs a design studio." in record["research_notes"] and record["city"] == "Example City"
    assert "Product Fit: Builds booking tools." in record["research_notes"]
    assert record["partnership_angle"] == "Fixed-scope sprint for a payments API."
    assert not record.get("pdf_observation") and not record.get("pdf_proposal")   # yours to write
    assert record["stage"] == "Needs research"
    contact = await state.get_prospect(best)
    assert contact.status == "partnership" and contact.email_status == "verified"
    assert (await state.get_prospect(weak)).status == "new"           # colleague: guarded, not adopted
    assert [e["kind"] for e in account["events"]] == ["note"]
    assert "Moved from Companies" in account["events"][0]["detail"]


@pytest.mark.asyncio
async def test_promote_is_idempotent_and_never_duplicates_the_contact(state, store):
    cid = await company(state)
    await person(state, cid)
    first = await store.promote(cid)
    second = await store.promote(cid)
    assert first["id"] == second["id"]
    assert (await store.query("SELECT COUNT(*) n FROM prospects"))[0]["n"] == 1
    assert (await store.query("SELECT COUNT(*) n FROM partnership_accounts"))[0]["n"] == 1


@pytest.mark.asyncio
async def test_promote_with_only_a_general_inbox_names_no_decision_maker(state, store):
    cid = await company(state)
    await person(state, cid, first="", email="hello@tracked.example", title="General inbox")
    account = await store.promote(cid)
    record = account["record"]
    assert not account["prospect_id"] and not record["target_person"]
    assert record["business_inbox"] == "hello@tracked.example" and not record["direct_email"]
    assert record["stage"] == "Needs contact research"


@pytest.mark.asyncio
async def test_promote_without_any_contact_still_makes_an_account(state, store):
    cid = await company(state)
    account = await store.promote(cid)
    assert not account["prospect_id"] and account["record"]["stage"] == "Needs contact research"


@pytest.mark.asyncio
async def test_promote_flags_a_company_the_pipeline_already_emailed(state, store):
    cid = await company(state)
    pid = await person(state, cid, status="contacted")
    await outbox(state, pid, "alex@tracked.example", "sent")
    account = await store.promote(cid)
    assert account["record"]["stage"] == "Review prior outreach"
    assert "1 cold email" in account["record"]["next_action"]
    assert (await state.get_prospect(pid)).status == "contacted"      # what happened stays true


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["replied", "opted_out", "meeting", "interested"])
async def test_promote_refuses_a_company_that_has_answered(state, store, status):
    cid = await company(state)
    await person(state, cid, status=status)
    with pytest.raises(ValueError, match="Conversations"):
        await store.promote(cid)
    assert (await store.query("SELECT COUNT(*) n FROM partnership_accounts"))[0]["n"] == 0


@pytest.mark.asyncio
async def test_promote_rejects_unknown_company_wrong_contact_and_no_website(state, store):
    with pytest.raises(HTTPException) as err:
        await store.promote("nope")
    assert err.value.status_code == 404
    cid = await company(state)
    other = await company(state, "Other Studio", "other.example")
    stranger = await person(state, other, "Jo", "jo@other.example")
    with pytest.raises(ValueError, match="does not belong"):
        await store.promote(cid, stranger)
    bare = await state.add_company(Company(name="No Site"))
    with pytest.raises(ValueError, match="no website"):
        await store.promote(bare)


# ── what the Companies tab shows ──


@pytest.mark.asyncio
async def test_the_register_says_why_cold_email_skips_the_company(state, store):
    cid = await company(state)
    await person(state, cid)
    config = SimpleNamespace(icp=SimpleNamespace(industries=[], titles=[], hiring_signals=[]),
                             compliance=SimpleNamespace(do_not_contact=[]))
    [before] = await build_register(store.query, config, cid)
    assert before["status"] == "Ready"
    await track(store, cid)
    [after] = await build_register(store.query, config, cid)
    assert after["status"] == "Partnership" and "Partnerships tab" in after["why_not_contacted"]


# ── the dashboard API ──

API_CONFIG = SimpleNamespace(
    icp=SimpleNamespace(industries=["Software"], titles=["Founder"], hiring_signals=[]),
    compliance=SimpleNamespace(do_not_contact=[]),
)


@pytest.fixture
def client(tmp_path, monkeypatch):
    db = tmp_path / "api.db"
    monkeypatch.setattr(dash, "DB_PATH", db)
    monkeypatch.setattr(sellbird_config, "load_config", lambda *a, **k: API_CONFIG)
    dash.token_store.configure(tmp_path / "token")

    async def seed():
        s = StateManager(str(db))
        await s.init_db()
        cid = await s.add_company(Company(name="Tracked Studio", domain="tracked.example",
                                          website="https://tracked.example", industry="Software"))
        await s.add_prospect(Prospect(company_id=cid, first_name="Alex", last_name="Example",
                                      title="Founder", email="alex@tracked.example",
                                      email_status="verified", score=70, company="Tracked Studio"))
        return cid
    cid = asyncio.run(seed())
    headers = {"Authorization": "Bearer " + dash.token_store.get()}
    with TestClient(dash.app, base_url="http://127.0.0.1:5555", headers=headers) as c:
        c.company_id = cid
        yield c


def test_moving_a_company_from_the_companies_tab(client):
    [row] = client.get("/api/companies").json()
    assert row["partnership_account_id"] == "" and row["register_status"] == "Ready"

    moved = client.post("/api/partnerships/promote", json={"company_id": client.company_id})
    assert moved.status_code == 200
    account = moved.json()
    assert account["record"]["target_person"] == "Alex Example"
    assert "Fit " in account["record"]["research_notes"]      # the fit line came from the register

    [row] = client.get("/api/companies").json()
    assert row["partnership_account_id"] == account["id"] and row["register_status"] == "Partnership"
    profile = client.get(f"/api/companies/{client.company_id}/profile").json()
    assert profile["partnership_account_id"] == account["id"]
    assert client.post("/api/partnerships/promote", json={"company_id": client.company_id}).json()["id"] == account["id"]


def test_the_move_works_when_the_fit_score_is_unavailable(client, monkeypatch):
    def broken(*a, **k):
        raise RuntimeError("config will not load")
    monkeypatch.setattr(sellbird_config, "load_config", broken)
    moved = client.post("/api/partnerships/promote", json={"company_id": client.company_id})
    assert moved.status_code == 200 and "Fit " not in moved.json()["record"]["research_notes"]


def test_the_move_validates_its_input_and_the_company(client):
    assert client.post("/api/partnerships/promote", json={}).status_code == 422
    assert client.post("/api/partnerships/promote", json={"company_id": 7}).status_code == 422
    assert client.post("/api/partnerships/promote", json={"company_id": "missing"}).status_code == 404
    assert client.post("/api/partnerships/promote", json={"company_id": client.company_id},
                       headers={"Authorization": ""}).status_code == 401


def test_the_shell_wires_the_move_button():
    js = open("sellbird/web/partnerships.js").read()
    assert "partnerCompanyAction" in js and "/api/partnerships/promote" in js
    assert "partnerCompanyAction" in open("sellbird/web/app.js").read()
