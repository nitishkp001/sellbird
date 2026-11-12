"""The LLM review of partnership drafts: saved with the draft, dropped when the text is edited,
and carried into the Outbox so the reviewer's verdict is next to the email when it is approved.

Everything here is invented (example.com style).
"""
import asyncio
import json
from types import SimpleNamespace

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

from sellbird import partnerships as p
from sellbird.state import StateManager
from tests.test_partnerships import activate, row

DRAFT = {'subject': 'payment api component',
         'body': 'Hi Alex,\n\nYour site describes commerce and ERP integrations. We could own one payment reconciliation component.\n\nOpen to a short call?\n\nSam'}
FIXED = {'subject': 'payment api component',
         'body': 'Hi Alex,\n\nYour site describes commerce and ERP integrations. We could own one payment reconciliation component.\n\nWould a short call help?\n\nSam'}


class ScriptedBrain:
    replies = []

    def __init__(self, *args):
        pass

    async def think_json(self, *args, **kwargs):
        return self.replies.pop(0) if self.replies else None


@pytest.fixture
def client(tmp_path, monkeypatch):
    import sellbird.dashboard as dash
    from sellbird import brain, config, humanize
    db = tmp_path / 'api.db'
    asyncio.run(StateManager(str(db)).init_db())
    monkeypatch.setattr(dash, 'DB_PATH', db)
    monkeypatch.setattr(brain, 'Brain', ScriptedBrain)

    async def no_polish(_brain, drafts, **kw):
        return drafts
    monkeypatch.setattr(humanize, 'humanize', no_polish)
    dash.token_store.configure(tmp_path / 'token')
    with TestClient(dash.app, base_url='http://127.0.0.1:5555',
                    headers={'Authorization': 'Bearer ' + dash.token_store.get()}) as c:
        c.set_review = lambda on: monkeypatch.setattr(
            config, 'load_config',
            lambda: SimpleNamespace(brain=None, writer=SimpleNamespace(review=on, review_rounds=2)))
        c.db = db
        yield c


def seed(client):
    client.post('/api/partnerships/import', json={'records': [row(email_subject='', email_body='')], 'confirm': True})
    client.post('/api/partnerships/brand', json={'name': 'Example Engineering'})
    return client.get('/api/partnerships').json()['accounts'][0]['id']


def test_draft_is_reviewed_fixed_and_saved_with_its_verdict(client):
    client.set_review(True)
    aid = seed(client)
    ScriptedBrain.replies = [
        DRAFT,
        {'verdict': 'revise', 'issues': ['Two questions in one email'], 'revised': FIXED},
        {'verdict': 'pass', 'issues': [], 'revised': None},
    ]
    record = client.post(f'/api/partnerships/{aid}/draft', json={}).json()['record']
    assert record['email_body'] == FIXED['body']
    assert record['email_review'] == {'verdict': 'revised', 'fixed': ['Two questions in one email'], 'issues': []}


def test_review_off_leaves_the_draft_unreviewed(client):
    client.set_review(False)
    aid = seed(client)
    ScriptedBrain.replies = [DRAFT]
    record = client.post(f'/api/partnerships/{aid}/draft', json={}).json()['record']
    assert record['email_body'] == DRAFT['body'] and 'email_review' not in record


def test_editing_the_email_drops_a_review_that_no_longer_describes_it(client):
    client.set_review(True)
    aid = seed(client)
    ScriptedBrain.replies = [DRAFT, {'verdict': 'pass', 'issues': [], 'revised': None}]
    client.post(f'/api/partnerships/{aid}/draft', json={})
    same = client.post(f'/api/partnerships/{aid}', json={'email_body': DRAFT['body']}).json()['record']
    assert same['email_review']['verdict'] == 'passed'          # saving unchanged text keeps it
    edited = client.post(f'/api/partnerships/{aid}', json={'email_body': 'Hi Alex, changed by hand'}).json()['record']
    assert 'email_review' not in edited


def test_the_review_cannot_be_set_through_the_account_form(client):
    aid = seed(client)
    assert client.post(f'/api/partnerships/{aid}', json={'email_review': 'passed'}).status_code == 422


@pytest_asyncio.fixture
async def store(tmp_path):
    state = StateManager(str(tmp_path / 's.db'))
    await state.init_db()
    return p.PartnershipStore(state)


@pytest.mark.asyncio
async def test_the_verdict_travels_into_the_outbox_item(store):
    await store.import_records([row()])
    aid = (await store.query('SELECT id FROM partnership_accounts'))[0]['id']
    await activate(store)
    await store.set_email_review(aid, {'verdict': 'flagged', 'fixed': [], 'issues': ['Opens with flattery']})
    queued = await store.stage_email(aid)
    item = await store.state.get_outbox_item(queued['outbox_id'])
    assert item['review_verdict'] == 'flagged'
    assert json.loads(item['review_notes']) == {'fixed': [], 'issues': ['Opens with flattery']}


@pytest.mark.asyncio
async def test_an_unreviewed_email_queues_with_no_verdict(store):
    await store.import_records([row()])
    aid = (await store.query('SELECT id FROM partnership_accounts'))[0]['id']
    await activate(store)
    item = await store.state.get_outbox_item((await store.stage_email(aid))['outbox_id'])
    assert item['review_verdict'] == '' and item['review_notes'] == ''
