"""Research and draft every ready partnership account. Everything here is invented."""
from types import SimpleNamespace

import pytest
import pytest_asyncio

from sellbird import partnerships as p
from sellbird.state import StateManager
from tests.test_partnerships import row

DRAFT = {'subject': 'payment api component',
         'body': 'Hi Alex,\n\nYour site lists commerce integrations. We could own one payment reconciliation component.\n\nOpen to a short call?\n\nSam'}
BRIEF = {'usable': True, 'hooks': ['Lists ERP and commerce integrations as a service'],
         'sources': ['https://studio.example/services']}


class FakeBrain:
    def __init__(self):
        self.calls = 0

    async def think_json(self, *a, **k):
        self.calls += 1
        return DRAFT


@pytest_asyncio.fixture
async def store(tmp_path):
    state = StateManager(str(tmp_path / 'b.db'))
    await state.init_db()
    s = p.PartnershipStore(state)
    await s.set_setting('brand', {'name': 'Example Engineering'})
    return s


@pytest.fixture
def cfg():
    return SimpleNamespace(product=SimpleNamespace(offer=None),
                           writer=SimpleNamespace(review=False, review_rounds=2))


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    from sellbird import humanize

    async def no_polish(_brain, drafts, **kw):
        return drafts
    monkeypatch.setattr(humanize, 'humanize', no_polish)


async def seed(store, **changes):
    await store.import_records([row(**{'email_subject': '', 'email_body': '', 'pdf_observation': '', **changes})])
    return (await store.query('SELECT id FROM partnership_accounts'))[0]['id']


@pytest.mark.asyncio
async def test_a_ready_account_gets_a_sourced_observation_and_a_draft(store, cfg, monkeypatch):
    aid = await seed(store)

    async def research(*a, **k):
        return BRIEF
    monkeypatch.setattr('sellbird.research.research_company', research)
    result = await p.bulk_draft(store, FakeBrain(), cfg)
    record = (await store.account(aid))['record']
    assert result['drafted'] == 1 and not result['failed'] and not result['running']
    assert record['pdf_observation'] == 'Lists ERP and commerce integrations as a service (from https://studio.example/services)'
    assert record['email_body'] == DRAFT['body']


@pytest.mark.asyncio
async def test_nothing_specific_found_means_no_invented_observation(store, cfg, monkeypatch):
    aid = await seed(store)

    async def research(*a, **k):
        return {'usable': False, 'hooks': [], 'sources': []}
    monkeypatch.setattr('sellbird.research.research_company', research)
    brain = FakeBrain()
    result = await p.bulk_draft(store, brain, cfg)
    record = (await store.account(aid))['record']
    assert result['drafted'] == 0 and len(result['skipped']) == 1
    assert not record.get('pdf_observation') and not record.get('email_body') and brain.calls == 0


@pytest.mark.asyncio
async def test_an_existing_observation_is_kept_and_research_skipped(store, cfg, monkeypatch):
    aid = await seed(store, pdf_observation='Written by hand, with a source.')

    async def research(*a, **k):
        raise AssertionError('should not research')
    monkeypatch.setattr('sellbird.research.research_company', research)
    result = await p.bulk_draft(store, FakeBrain(), cfg)
    record = (await store.account(aid))['record']
    assert result['drafted'] == 1 and record['pdf_observation'] == 'Written by hand, with a source.'


@pytest.mark.asyncio
async def test_who_is_not_ready(store):
    def acct(n, **changes):
        return row(**{'id': n, 'company': f'Studio {n}', 'website': f'https://{n.lower()}.example',
                      'direct_email': f'alex@{n.lower()}.example', 'target_person': f'Alex {n}',
                      'target_profile': f'https://www.linkedin.com/in/{n.lower()}-profile/', **changes})
    await store.import_records([
        acct('A1'),
        acct('A2', email_body='', partnership_angle=''),
        acct('A3', email_body='', direct_email='', business_inbox='hello@a3.example'),
        acct('A4', email_body='', email_subject=''),
        acct('A5', direct_email='', business_inbox='hello@a5.example'),
        acct('A6', email_body='', direct_email='', target_person='Alex', target_profile='')])
    reasons = {}
    for r in await store.query('SELECT id FROM partnership_accounts ORDER BY external_id'):
        a = await store.account(r['id'])
        reasons[a['record']['id']] = p.draftable_for_bulk(a)
    # A3 and A5 have only a general inbox: they need a direct email found, so they are in scope;
    # A5 already has a draft, which can't be queued without one.
    assert reasons == {'A1': 'already drafted', 'A2': 'no partnership angle', 'A3': '',
                       'A4': '', 'A5': '', 'A6': 'no named decision maker'}


def _found(email, status):
    async def finder(first, last, domain, **kw):
        from sellbird.integrations.email_finder import EmailResult
        return EmailResult(email=email, status=status)
    return finder


@pytest.mark.asyncio
async def test_a_general_inbox_only_account_gets_a_verified_direct_email_first(store, cfg, monkeypatch):
    aid = await seed(store, direct_email='', business_inbox='hello@studio.example')

    async def research(*a, **k):
        return BRIEF
    monkeypatch.setattr('sellbird.research.research_company', research)
    result = await p.bulk_draft(store, FakeBrain(), cfg, env=object(),
                                finder=_found('alex.example@studio.example', 'verified'))
    a = await store.account(aid)
    contact = await store.state.get_prospect(a['prospect_id'])
    assert result['emails_found'] == 1 and result['drafted'] == 1
    assert a['record']['direct_email'] == 'alex.example@studio.example'
    assert contact.email == 'alex.example@studio.example' and contact.email_status == 'verified'


@pytest.mark.asyncio
async def test_a_guessed_address_is_never_saved_and_nothing_is_drafted(store, cfg, monkeypatch):
    aid = await seed(store, direct_email='', business_inbox='hello@studio.example')

    async def research(*a, **k):
        raise AssertionError('no research without a sendable address')
    monkeypatch.setattr('sellbird.research.research_company', research)
    brain = FakeBrain()
    result = await p.bulk_draft(store, brain, cfg, env=object(), finder=_found('alex@studio.example', 'guess'))
    record = (await store.account(aid))['record']
    assert result['drafted'] == 0 and len(result['skipped']) == 1 and brain.calls == 0
    assert not record.get('direct_email') and not record.get('email_body')


@pytest.mark.asyncio
async def test_no_brand_means_nothing_is_spent(store, cfg, monkeypatch):
    await store.set_setting('brand', {})
    await seed(store)

    async def research(*a, **k):
        raise AssertionError('should not research')
    monkeypatch.setattr('sellbird.research.research_company', research)
    with pytest.raises(ValueError):
        await p.bulk_draft(store, FakeBrain(), cfg)
    assert p.BULK_DRAFT['running'] is False


@pytest.mark.asyncio
async def test_a_second_run_while_one_is_going_is_refused(store, cfg):
    p.BULK_DRAFT['running'] = True
    try:
        with pytest.raises(ValueError):
            await p.bulk_draft(store, FakeBrain(), cfg)
    finally:
        p.BULK_DRAFT['running'] = False


from tests.test_partnership_review import client  # noqa: E402,F401  (fixture)


def test_the_bulk_routes_are_not_swallowed_by_the_account_routes(client):
    status = client.get('/api/partnerships/draft-all')
    assert status.status_code == 200 and 'eligible' in status.json() and status.json()['running'] is False


def test_the_send_limit_endpoint_refuses_nonsense(client):
    assert client.post('/api/send-limit', json={'limit': 'lots'}).status_code == 400
    assert client.post('/api/send-limit', json={}).status_code == 400
    assert client.post('/api/send-limit', json={'limit': 9999}).status_code == 409


def test_a_unicode_digit_limit_is_refused_not_a_crash(client):
    # "²".isdigit() is True but int("²") raises: this used to be a 500.
    assert client.post('/api/send-limit', json={'limit': '²'}).status_code == 400
    assert client.post('/api/send-limit', json={'limit': '١٠'}).status_code == 400


class RecordingBrain(FakeBrain):
    def __init__(self):
        super().__init__()
        self.prompts = []

    async def think_json(self, prompt, *a, **k):
        self.prompts.append(prompt)
        return await super().think_json(prompt, *a, **k)


@pytest.mark.asyncio
async def test_a_researched_observation_is_marked_unchecked_until_someone_checks_it(store, cfg, monkeypatch):
    aid = await seed(store)

    async def research(*a, **k):
        return BRIEF
    monkeypatch.setattr('sellbird.research.research_company', research)
    review_facts = []

    async def review(brain, draft, facts, **kw):
        review_facts.extend(facts)
        return draft, 'passed', {'fixed': [], 'issues': [], 'rounds': 1}
    monkeypatch.setattr('sellbird.draft_review.review_draft', review)
    cfg.writer.review = True
    brain = RecordingBrain()
    await p.bulk_draft(store, brain, cfg)
    record = (await store.account(aid))['record']
    assert record['observation_origin'] == 'research'
    observed = [f for f in review_facts if f.startswith('- Observation')][0]
    assert 'NOT checked' in observed and 'entered by the sender' not in observed
    assert 'nobody has checked it' in brain.prompts[0]

    # Saving the account unchanged keeps the note; confirming it clears it.
    await store.update(aid, {'pdf_observation': record['pdf_observation']})
    assert (await store.account(aid))['record']['observation_origin'] == 'research'
    await store.set_observation_origin(aid, '')
    assert 'observation_origin' not in (await store.account(aid))['record']


@pytest.mark.asyncio
async def test_editing_a_researched_observation_makes_it_the_senders(store, cfg, monkeypatch):
    aid = await seed(store)

    async def research(*a, **k):
        return BRIEF
    monkeypatch.setattr('sellbird.research.research_company', research)
    await p.bulk_draft(store, FakeBrain(), cfg)
    await store.update(aid, {'pdf_observation': 'Their services page lists ERP integrations.'})
    assert 'observation_origin' not in (await store.account(aid))['record']


@pytest.mark.asyncio
async def test_all_accounts_load_the_same_as_one_at_a_time(store):
    await store.import_records([row(company='Alpha Studio', website='https://alpha.example'),
                                row(company='Beta Studio', website='https://beta.example')])
    ids = [r['id'] for r in await store.query('SELECT id FROM partnership_accounts ORDER BY external_id')]
    await store.event(ids[0], {'kind': 'note', 'idempotency_key': 'k1', 'detail': 'invented'})
    assert await store.accounts() == [await store.account(i) for i in ids]
