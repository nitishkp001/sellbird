"""Companies and Contacts say which kind of lead each row is: partnership (an agency written to one
to one from Partnerships) or direct outreach. The tabs filter on it.

Everything here is invented (example.com style).
"""
import asyncio
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import sellbird.dashboard as dash
from sellbird import partnerships as p
from sellbird.models import Company
from sellbird.models.prospect import Prospect
from sellbird.state import StateManager
from tests.test_partnerships import row


@pytest.fixture
def client(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "sellbird.db"
        monkeypatch.setattr(dash, "DB_PATH", db)
        monkeypatch.setattr(dash, "WEB_DIR", Path(__file__).resolve().parent.parent / "sellbird" / "web")
        dash.token_store.configure(Path(tmp) / "data" / "dashboard-token")
        token = dash.token_store.get()
        with TestClient(dash.app, base_url="http://127.0.0.1:5555",
                        headers={"Authorization": f"Bearer {token}"}) as c:
            c.db_path = db
            yield c


def seed(db_path):
    async def go():
        state = StateManager(str(db_path))
        await state.init_db()
        await p.PartnershipStore(state).import_records([row()])      # Example Studio: a partnership account
        cid = await state.add_company(Company(name="Direct Co", domain="direct.example",
                                              website="https://direct.example"))
        await state.add_prospect(Prospect(first_name="Dana", last_name="Direct", company="Direct Co",
                                          company_id=cid, email="dana@direct.example",
                                          email_status="verified", status="new"))
        # A contact at a partnership company who was never marked 'partnership' still counts as one.
        studio = await state.get_company_by_domain("studio.example")
        await state.add_prospect(Prospect(first_name="Sam", last_name="Studio", company="Example Studio",
                                          company_id=studio.id, email="sam@studio.example",
                                          email_status="verified", status="new"))
    asyncio.run(go())


def test_contacts_are_flagged_partnership_or_direct(client):
    seed(client.db_path)
    kinds = {r["company"]: r["partnership"] for r in client.get("/api/prospects").json()}
    assert kinds["Direct Co"] is False
    assert kinds["Example Studio"] is True            # both the imported contact and the later one


def test_companies_are_flagged_partnership_or_direct(client):
    seed(client.db_path)
    kinds = {c["name"]: c["partnership"] for c in client.get("/api/companies").json()}
    assert kinds["Direct Co"] is False and kinds["Example Studio"] is True


def test_the_tabs_ship_the_filter(client):
    js = client.get("/static/app.js").text
    assert "setCompanyKind" in js and "setContactKind" in js and "kindMatches" in js
