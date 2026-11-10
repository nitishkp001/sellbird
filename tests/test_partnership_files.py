"""Each brief is saved as files named for the company: its personalised HTML and the PDF printed from it."""
import asyncio
import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

import sellbird.dashboard as dash
from sellbird import partnerships as p
from sellbird.state import StateManager
from tests.test_partnership_kit import make_kit

FAKE_PDF = b'%PDF-1.7 fictional brief'
ROW = {'id': 'S50', 'company': 'Example Studio', 'website': 'https://studio.example',
       'research_sources': 'https://studio.example/work', 'pdf_observation': 'Your site lists commerce work.',
       'pdf_proposal': 'A reconciliation service.', 'pdf_pilot': 'One bounded integration.'}


@pytest.fixture(autouse=True)
def fake_printer(monkeypatch):
    printed = []

    async def to_pdf(markup):
        printed.append(markup)
        return FAKE_PDF
    monkeypatch.setattr(p, 'to_pdf', to_pdf)
    return printed


@pytest_asyncio.fixture
async def state(tmp_path):
    state = StateManager(str(tmp_path / 'data' / 'test.db'))
    (tmp_path / 'data').mkdir()
    await state.init_db()
    return state


async def account(store, **changes):
    await store.import_records([{**ROW, **changes}])
    return (await store.query('SELECT id FROM partnership_accounts'))[0]['id']


async def generate(store, aid):
    asset = await store.create_asset(aid)
    return asset, await store.save_files(aid, asset)


# ── what is saved, and where ──


@pytest.mark.asyncio
async def test_a_brief_is_saved_as_company_named_html_and_pdf(state, tmp_path):
    store = p.PartnershipStore(state, output_dir=str(tmp_path / 'briefs'))
    asset, files = await generate(store, await account(store))
    folder = tmp_path / 'briefs' / 'S50-example-studio'
    assert files == {'folder': str(folder), 'html': str(folder / 'S50-example-studio-brief-v1.html'),
                     'pdf': str(folder / 'S50-example-studio-brief-v1.pdf')}
    assert (folder / 'S50-example-studio-brief-v1.pdf').read_bytes() == FAKE_PDF
    assert 'Example Studio' in (folder / 'S50-example-studio-brief-v1.html').read_text()
    # private: they hold prospect research
    assert stat.S_IMODE(os.stat(files['pdf']).st_mode) == 0o600 and stat.S_IMODE(os.stat(folder).st_mode) == 0o700
    assert not list(folder.glob('*.tmp'))


@pytest.mark.asyncio
async def test_the_saved_html_is_exactly_what_was_printed_in_the_kit_design(state, tmp_path, fake_printer):
    store = p.PartnershipStore(state, str(make_kit(tmp_path)), str(tmp_path / 'briefs'))
    asset, files = await generate(store, await account(store))
    html = open(files['html'], encoding='utf-8').read()
    assert html == fake_printer[-1]                                   # the very markup handed to the printer
    assert html.count('@font-face') == 2 and '{{' not in html          # fonts embedded: it opens standalone
    assert 'data:image/png;base64,' in html and 'Example Studio' in html


@pytest.mark.asyncio
async def test_saving_twice_changes_nothing_and_a_new_version_sits_beside_the_old(state, tmp_path):
    store = p.PartnershipStore(state, output_dir=str(tmp_path / 'briefs'))
    aid = await account(store)
    asset, first = await generate(store, aid)
    before = os.stat(first['pdf']).st_mtime_ns
    again = await store.save_files(aid, asset)
    assert again == first and os.stat(first['pdf']).st_mtime_ns == before
    events = [e for e in (await store.account(aid))['events'] if e['detail'].startswith('Brief v1 saved')]
    assert len(events) == 1 and 'S50-example-studio' in events[0]['detail']
    await store.update(aid, {'pdf_proposal': 'A different component.'})
    _, second = await generate(store, aid)
    names = sorted(os.listdir(first['folder']))
    assert names == ['S50-example-studio-brief-v1.html', 'S50-example-studio-brief-v1.pdf',
                     'S50-example-studio-brief-v2.html', 'S50-example-studio-brief-v2.pdf']
    assert 'A reconciliation service.' in open(first['html']).read()   # v1 is untouched
    assert 'A different component.' in open(second['html']).read()


@pytest.mark.asyncio
async def test_the_default_folder_sits_beside_the_database(state):
    store = p.PartnershipStore(state)
    _, files = await generate(store, await account(store))
    assert files['folder'] == str(Path(str(state.db_path)).parent / 'partnership-briefs' / 'S50-example-studio')


def test_an_in_memory_database_has_nowhere_to_save():
    assert p.PartnershipStore(SimpleNamespace(db_path=':memory:')).output_root() is None


def test_the_configured_folder_expands_the_home_marker(monkeypatch, tmp_path):
    monkeypatch.setenv('HOME', str(tmp_path))
    assert p.PartnershipStore(SimpleNamespace(db_path='x.db'), output_dir='~/out').output_root() == tmp_path / 'out'


# ── safety ──


@pytest.mark.parametrize('company,external,expect', [
    ('Acme / Studio: "Brand" & Co.', 'S01', 'S01-acme-studio-brand-co'),
    ('../../etc', 'S02', 'S02-etc'),
    ('Café Münster', 'S03', 'S03-caf-m-nster'),
    ('???', 'S04', 'S04-company'),
    ('x' * 200, 'S05', 'S05-' + 'x' * 40),
])
def test_company_names_become_safe_folder_names(company, external, expect):
    assert p.safe_part(external) + '-' + p.file_slug(company) == expect


@pytest.mark.parametrize('bad', ['../../x', '..', '.', '/etc/passwd', 'a/b', '...'])
def test_a_tracker_id_can_never_climb_out_of_the_folder(bad):
    part = p.safe_part(bad)
    assert '/' not in part and not part.startswith('.') and part not in ('', '.', '..')


@pytest.mark.asyncio
async def test_a_folder_that_is_really_a_link_elsewhere_is_refused(state, tmp_path):
    root = tmp_path / 'briefs'
    root.mkdir()
    elsewhere = tmp_path / 'elsewhere'
    elsewhere.mkdir()
    (root / 'S50-example-studio').symlink_to(elsewhere)
    store = p.PartnershipStore(state, output_dir=str(root))
    aid = await account(store)
    asset = await store.create_asset(aid)
    with pytest.raises(ValueError, match='directly inside'):
        await store.save_files(aid, asset)
    assert not list(elsewhere.iterdir())


@pytest.mark.asyncio
async def test_a_version_from_another_kit_is_refused_not_saved_wrongly(state, tmp_path):
    root = make_kit(tmp_path)
    store = p.PartnershipStore(state, str(root), str(tmp_path / 'briefs'))
    aid = await account(store)
    asset = await store.create_asset(aid)
    (root / 'assets/Body-400.ttf').write_bytes(b'a different font')
    with pytest.raises(ValueError, match='different brief kit'):
        await store.save_files(aid, asset)
    assert not (tmp_path / 'briefs' / 'S50-example-studio').exists() or not list((tmp_path / 'briefs' / 'S50-example-studio').iterdir())


# ── the dashboard route ──


@pytest.fixture
def client(tmp_path, monkeypatch):
    (tmp_path / 'data').mkdir()
    db = tmp_path / 'data' / 'api.db'
    asyncio.run(StateManager(str(db)).init_db())
    monkeypatch.setattr(dash, 'DB_PATH', db)
    dash.token_store.configure(tmp_path / 'token')
    with TestClient(dash.app, base_url='http://127.0.0.1:5555', headers={'Authorization': 'Bearer ' + dash.token_store.get()}) as c:
        c.root = tmp_path
        yield c


def made_account(client):
    assert client.post('/api/partnerships/import', json={'records': [ROW], 'confirm': True}).status_code == 200
    return client.get('/api/partnerships').json()['accounts'][0]['id']


def test_generating_a_brief_reports_where_the_files_went(client):
    aid = made_account(client)
    reply = client.post(f'/api/partnerships/{aid}/brief', json={}).json()
    folder = client.root / 'data' / 'partnership-briefs' / 'S50-example-studio'
    assert reply['files']['folder'] == str(folder) and not reply['files_error']
    assert (folder / 'S50-example-studio-brief-v1.pdf').read_bytes() == FAKE_PDF
    assert (folder / 'S50-example-studio-brief-v1.html').is_file()


def test_a_folder_that_cannot_be_written_does_not_lose_the_brief(client, monkeypatch):
    blocker = client.root / 'not-a-folder'
    blocker.write_text('a file')
    monkeypatch.setattr(p, 'configured_output_dir', lambda: str(blocker / 'inside'))
    aid = made_account(client)
    response = client.post(f'/api/partnerships/{aid}/brief', json={})
    assert response.status_code == 200
    reply = response.json()
    assert reply['asset_id'] and reply['files'] is None and 'could not be saved' in reply['files_error']
    assert len(client.get(f'/api/partnerships/{aid}').json()['assets']) == 1      # the version itself exists
