"""Company types: a kit's proof pages and rows follow the account's type, chosen or suggested from its notes."""
import asyncio
import re

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

import sellbird.dashboard as dash
from sellbird import partnerships as p
from sellbird.partnership_assets import brief_hash, render_brief
from sellbird.partnership_kit import KitError, choose_segment, close_call, finalize, load_kit
from sellbird.state import StateManager
from tests.test_partnership_kit import RECORD, TEMPLATE, make_kit

SEGMENTS = {
    'segments': [
        {'id': 'fintech', 'label': 'Fintech builders', 'keywords': ['fintech', 'blockchain', 'payment gateway']},
        {'id': 'commerce', 'label': 'Commerce studios', 'keywords': ['shopify', 'e-commerce', 'ecommerce', 'erp']},
        {'id': 'app', 'label': 'App companies', 'keywords': ['mobile app', 'ios', 'android', 'app']},
        {'id': 'general', 'label': 'Other', 'keywords': []},
    ],
    'default_segment': 'general',
}
PROOF = ('<!-- only: commerce, fintech --><section class="slide" id="money">MONEY PROOF'
         '<footer><span class="index">09 / 09</span></footer></section><!-- /only -->'
         '<!-- only: app, general --><section class="slide" id="apis">API PROOF'
         '<footer><span class="index">09 / 09</span></footer></section><!-- /only -->'
         '<section class="slide" id="always">DOMAINS'
         '<!-- only: fintech --><div class="row">BLOCKCHAIN ROW</div><!-- /only -->'
         '<div class="row">PAYMENTS ROW</div><footer><span class="index">09 / 09</span></footer></section>')
TYPED = TEMPLATE.replace('</body>', PROOF + '</body>')


def kit(tmp_path, template=TYPED, **manifest):
    return load_kit(make_kit(tmp_path, template, {**SEGMENTS, **manifest}))


def record(**changes):
    return {**RECORD, **changes}


# ── the template and manifest are checked when the kit loads ──


def test_a_typed_kit_loads(tmp_path):
    loaded = kit(tmp_path)
    assert [s.id for s in loaded.segments] == ['fintech', 'commerce', 'app', 'general'] and loaded.default_segment == 'general'


@pytest.mark.parametrize('template,match', [
    (TYPED.replace('only: fintech', 'only: fintec'), "names 'fintec'"),
    (TYPED.replace('<!-- /only -->', '', 1), 'unbalanced or nested'),
    (TYPED.replace('<!-- only: app, general -->', '<!-- only: app, general --><!-- only: app -->', 1), 'unbalanced or nested'),
    (TYPED.replace('<!-- only: fintech -->', '<!-- only: -->'), 'no company type'),
])
def test_a_bad_only_block_is_refused(tmp_path, template, match):
    with pytest.raises(KitError, match=match):
        kit(tmp_path, template)


def test_only_blocks_need_declared_types(tmp_path):
    with pytest.raises(KitError, match="doesn't declare"):
        load_kit(make_kit(tmp_path, TYPED))


@pytest.mark.parametrize('change,match', [
    ({'default_segment': 'nope'}, 'default_segment must be one of'),
    ({'default_segment': ''}, 'default_segment must be one of'),
    ({'segments': SEGMENTS['segments'] + [{'id': 'app', 'label': 'again'}]}, 'listed twice'),
    ({'segments': [{'id': 'Bad Id', 'label': 'x'}], 'default_segment': 'Bad Id'}, 'id of lowercase'),
    ({'segments': [{'id': 'a', 'label': 'x', 'keywords': ['k']}], 'default_segment': 'a'}, 'keywords of 2 to 40'),
    ({'segments': 'commerce'}, 'list of at most 12'),
])
def test_bad_segment_declarations_are_refused(tmp_path, change, match):
    with pytest.raises(KitError, match=match):
        kit(tmp_path, TEMPLATE, **change)


def test_a_type_must_never_drop_the_accounts_own_text(tmp_path):
    hidden = TEMPLATE.replace('{{OBSERVATION}}', '<!-- only: fintech -->{{OBSERVATION}}<!-- /only -->')
    with pytest.raises(KitError, match=r'\{\{OBSERVATION\}\}.*for the commerce type'):
        kit(tmp_path, hidden)


# ── choosing the type ──


def test_the_type_chosen_on_the_account_wins(tmp_path):
    k = kit(tmp_path)
    notes = record(research_notes='A Shopify studio.', company_type='  FinTech ')
    assert choose_segment(k, notes) == ('fintech', 'chosen')


def test_a_type_the_kit_does_not_have_is_refused(tmp_path):
    with pytest.raises(KitError, match="isn't one of this kit's types"):
        choose_segment(kit(tmp_path), record(company_type='plumbing'))


def test_the_type_is_suggested_from_the_notes_that_describe_the_company(tmp_path):
    k = kit(tmp_path)
    assert choose_segment(k, record(research_notes='Builds Shopify and ERP integrations for retail.')) == ('commerce', 'suggested')
    assert choose_segment(k, record(research_notes='', pdf_observation='A fintech with a payment gateway.')) == ('fintech', 'suggested')


def test_what_we_propose_to_build_never_decides_the_type(tmp_path):
    # A web studio we offer a payments component to is not a fintech company.
    studio = record(research_notes='A web design studio.', pdf_observation='Builds brochure sites.',
                    pdf_proposal='A blockchain payment gateway reconciliation service.',
                    partnership_angle='fintech blockchain payment gateway')
    assert choose_segment(kit(tmp_path), studio) == ('general', 'default')


def test_keywords_match_whole_words_only(tmp_path):
    k = kit(tmp_path)
    assert choose_segment(k, record(pdf_observation='', research_notes='A happy team near the apple orchard, with a caption.')) == ('general', 'default')
    assert choose_segment(k, record(pdf_observation='', research_notes='Ships an Android app.')) == ('app', 'suggested')


def test_most_matches_wins_and_the_earlier_type_breaks_a_tie(tmp_path):
    k = kit(tmp_path)
    assert choose_segment(k, record(pdf_observation='', research_notes='Shopify, ecommerce and ERP work, plus an iOS app.')) == ('commerce', 'suggested')
    assert choose_segment(k, record(pdf_observation='', research_notes='A fintech and a Shopify shop.')) == ('fintech', 'suggested')     # one each: fintech is listed first


def test_a_close_call_names_the_runner_up(tmp_path):
    k = kit(tmp_path)
    blurred = record(pdf_observation='', research_notes='Shopify stores and an Android build.')   # one keyword each; commerce is listed first
    assert choose_segment(k, blurred)[1] == 'suggested' and close_call(k, blurred) == 'App companies'
    clear = record(pdf_observation='', research_notes='Shopify, ecommerce and ERP work, plus iOS.')   # 3 against 1
    assert close_call(k, clear) == ''
    assert close_call(k, record(pdf_observation='', research_notes='A law firm.')) == ''


def test_nothing_matching_gives_the_default(tmp_path):
    assert choose_segment(kit(tmp_path), record(research_notes='A law firm.', pdf_observation='')) == ('general', 'default')


def test_a_kit_without_types_has_none(tmp_path):
    assert choose_segment(load_kit(make_kit(tmp_path)), record(company_type='anything')) == ('', 'none')


# ── what each type gets ──


@pytest.mark.parametrize('kind,present,absent', [
    ('commerce', ['MONEY PROOF', 'PAYMENTS ROW'], ['API PROOF', 'BLOCKCHAIN ROW']),
    ('fintech', ['MONEY PROOF', 'BLOCKCHAIN ROW', 'PAYMENTS ROW'], ['API PROOF']),
    ('app', ['API PROOF', 'PAYMENTS ROW'], ['MONEY PROOF', 'BLOCKCHAIN ROW']),
    ('general', ['API PROOF', 'PAYMENTS ROW'], ['MONEY PROOF', 'BLOCKCHAIN ROW']),
])
def test_each_type_gets_its_own_proof(tmp_path, kind, present, absent):
    k = kit(tmp_path)
    markup = render_brief(record(company_type=kind), {}, k)
    for text in present:
        assert text in markup
    for text in absent:
        assert text not in markup
    assert 'only:' not in markup and '/only' not in markup           # the markers themselves never reach the page
    assert f'<!-- brief-type:{kind} -->' in markup
    assert 'DOMAINS' in markup and 'Example Studio' in markup           # the shared pages and the account's text are always there


def test_page_numbers_follow_each_types_page_count(tmp_path):
    k = kit(tmp_path)
    for kind in ('commerce', 'app'):
        markup = render_brief(record(company_type=kind), {}, k)
        spans = re.findall(r'<span class="index">(\d+ / \d+)</span>', markup)
        assert spans[-1].split(' / ')[0] == spans[-1].split(' / ')[1] and len(spans) == len(set(spans))


def test_a_different_type_is_a_different_version(tmp_path):
    k = kit(tmp_path)
    one, two = render_brief(record(company_type='commerce'), {}, k), render_brief(record(company_type='app'), {}, k)
    assert brief_hash(one) != brief_hash(two)
    assert '{{' not in finalize(k, one)


def test_text_typed_into_an_account_cannot_open_or_close_a_block(tmp_path):
    k = kit(tmp_path)
    markup = render_brief(record(company_type='app', pdf_observation='<!-- /only --><!-- only: app -->sneaky'), {}, k)
    assert '<!-- /only -->' not in markup.replace('<!-- brief', '') and '&lt;!-- /only --&gt;' in markup


# ── the store and the dashboard ──


@pytest_asyncio.fixture
async def state(tmp_path):
    state = StateManager(str(tmp_path / 'test.db'))
    await state.init_db()
    return state


ROW = {'id': 'S70', 'company': 'Example Studio', 'website': 'https://studio.example', 'research_sources': 'https://studio.example/work',
       'pdf_observation': 'Builds Shopify stores.', 'pdf_proposal': 'A sync service.', 'pdf_pilot': 'One integration.'}


@pytest.mark.asyncio
async def test_the_plan_says_which_type_and_why(state, tmp_path):
    store = p.PartnershipStore(state, str(make_kit(tmp_path, TYPED, SEGMENTS)))
    await store.import_records([ROW])
    aid = (await store.query('SELECT id FROM partnership_accounts'))[0]['id']
    plan = store.brief_plan((await store.account(aid))['record'])
    assert (plan['segment'], plan['label'], plan['how'], plan['close']) == ('commerce', 'Commerce studios', 'suggested', '')
    assert [s['id'] for s in plan['segments']] == ['fintech', 'commerce', 'app', 'general']
    updated = await store.update(aid, {'company_type': ' APP '})
    assert updated['record']['company_type'] == 'app'
    assert store.brief_plan(updated['record'])['how'] == 'chosen' and store.brief_plan(updated['record'])['close'] == ''
    cleared = await store.update(aid, {'company_type': ''})
    assert store.brief_plan(cleared['record'])['how'] == 'suggested'


@pytest.mark.asyncio
async def test_only_the_kits_types_can_be_saved(state, tmp_path):
    store = p.PartnershipStore(state, str(make_kit(tmp_path, TYPED, SEGMENTS)))
    await store.import_records([ROW])
    aid = (await store.query('SELECT id FROM partnership_accounts'))[0]['id']
    for bad in ('plumbing', 'Bad Id!', '../x'):
        with pytest.raises(ValueError, match='company types the brief kit offers'):
            await store.update(aid, {'company_type': bad})
    assert 'company_type' not in (await store.account(aid))['record']


@pytest.mark.asyncio
async def test_there_is_no_plan_without_types_or_a_kit(state, tmp_path):
    assert p.PartnershipStore(state).brief_plan(RECORD) is None
    assert p.PartnershipStore(state, str(make_kit(tmp_path))).brief_plan(RECORD) is None


@pytest.mark.asyncio
async def test_an_unusable_type_is_reported_in_the_plan_and_blocks_the_brief(state, tmp_path):
    store = p.PartnershipStore(state, str(make_kit(tmp_path, TYPED, SEGMENTS)))
    await store.import_records([{**ROW, 'company_type': 'plumbing'}])      # e.g. from a CSV import
    aid = (await store.query('SELECT id FROM partnership_accounts'))[0]['id']
    plan = store.brief_plan((await store.account(aid))['record'])
    assert "isn't one of this kit's types" in plan['error']
    with pytest.raises(ValueError, match="isn't one of this kit's types"):
        await store.create_asset(aid)


@pytest.fixture
def client(tmp_path, monkeypatch):
    db = tmp_path / 'api.db'
    asyncio.run(StateManager(str(db)).init_db())
    monkeypatch.setattr(dash, 'DB_PATH', db)
    kit_dir = str(make_kit(tmp_path, TYPED, SEGMENTS))
    monkeypatch.setattr(p, 'configured_kit_dir', lambda: kit_dir)
    dash.token_store.configure(tmp_path / 'token')
    printed = []

    async def to_pdf(markup):
        printed.append(markup)
        return b'%PDF-1.7 fictional'
    monkeypatch.setattr(p, 'to_pdf', to_pdf)
    with TestClient(dash.app, base_url='http://127.0.0.1:5555', headers={'Authorization': 'Bearer ' + dash.token_store.get()}) as c:
        c.printed = printed
        yield c


def test_the_dashboard_shows_the_type_and_the_brief_follows_it(client):
    assert client.post('/api/partnerships/import', json={'records': [ROW], 'confirm': True}).status_code == 200
    aid = client.get('/api/partnerships').json()['accounts'][0]['id']
    plan = client.get(f'/api/partnerships/{aid}').json()['brief_plan']
    assert (plan['segment'], plan['how']) == ('commerce', 'suggested')
    assert client.post(f'/api/partnerships/{aid}', json={'company_type': 'app'}).status_code == 200
    assert client.get(f'/api/partnerships/{aid}').json()['brief_plan']['how'] == 'chosen'
    assert client.post(f'/api/partnerships/{aid}', json={'company_type': 'plumbing'}).status_code == 422
    assert client.post(f'/api/partnerships/{aid}/brief', json={}).status_code == 200
    printed = client.printed[-1]
    assert 'API PROOF' in printed and 'MONEY PROOF' not in printed


def test_the_form_has_a_company_type_selector():
    js = open('sellbird/web/partnerships.js').read()
    assert 'data-partner-field="company_type"' in js and 'Suggest from my notes' in js
