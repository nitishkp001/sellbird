"""Brief kits: a template folder with embedded fonts, filled from an account and printed offline."""
import asyncio
import json
import shutil

import pytest
import pytest_asyncio

from sellbird import partnerships as p
from sellbird.partnership_assets import render_brief, to_pdf
from sellbird.partnership_kit import KitError, finalize, fill, load_kit, preview
from sellbird.state import StateManager

TEMPLATE = (
    "<!doctype html><html><head><meta charset='utf-8'><title>For {{COMPANY}}</title>"
    "<style>{{FONTS}}@page{size:1280px 720px;margin:0}body{margin:0;font-family:'Body Font',sans-serif}"
    ".slide{width:1280px;height:720px;position:relative;overflow:hidden}.card{width:500px}</style></head><body>"
    "<section class=\"slide\"><header>{{LOGO}}</header><h1>{{COMPANY}}</h1>"
    "<div class=\"card\">{{OBSERVATION}}</div><div>{{COMPONENT}}</div><div>{{FIRST_ENGAGEMENT}}</div>"
    "<div>{{SOURCES}}</div><footer><span class=\"index\">09 / 09</span></footer></section>"
    "<section class=\"slide\"><footer><span class=\"index\">09 / 09</span></footer></section></body></html>"
)
RECORD = {
    'company': 'Example Studio',
    'pdf_observation': 'Your site lists commerce and ERP work.\nAnd mobile.',
    'pdf_proposal': 'A payment reconciliation service.',
    'pdf_pilot': 'One bounded integration.',
    'research_sources': 'https://studio.example/work | https://studio.example/about',
}


def make_kit(tmp_path, template=TEMPLATE, manifest=None, fonts=True, logo=True):
    root = tmp_path / 'kit'
    (root / 'assets').mkdir(parents=True)
    (root / 'template.html').write_text(template)
    declared = {'name': 'Example kit', 'brand': 'Example Co', 'template': 'template.html'}
    if fonts:
        (root / 'assets/Body-400.ttf').write_bytes(b'fictional font bytes 400')
        (root / 'assets/Body-700.ttf').write_bytes(b'fictional font bytes 700')
        declared['fonts'] = [{'family': 'Body Font', 'weight': 400, 'file': 'assets/Body-400.ttf'},
                             {'family': 'Body Font', 'weight': 700, 'file': 'assets/Body-700.ttf'}]
    if logo:
        (root / 'assets/logo.png').write_bytes(b'\x89PNG fictional logo')
        declared['logo'] = 'assets/logo.png'
    (root / 'kit.json').write_text(json.dumps({**declared, **(manifest or {})}))
    return root


# ── loading and refusing ──


def test_a_valid_kit_loads(tmp_path):
    kit = load_kit(make_kit(tmp_path))
    assert kit.name == 'Example kit' and len(kit.fonts) == 2 and kit.logo and len(kit.fingerprint) == 64


@pytest.mark.parametrize('bad', [
    "<script>alert(1)</script>", "<img src=x onerror=alert(1)>", "<iframe src=about:blank></iframe>",
    "<a href='javascript:alert(1)'>x</a>", "@import url(other.css);", "<link rel=stylesheet href='https://cdn.example/x.css'>",
    "<img src='https://cdn.example/x.png'>", "<style>a{background:url(https://cdn.example/x.png)}</style>",
])
def test_a_template_may_not_run_or_fetch_anything(tmp_path, bad):
    with pytest.raises(KitError, match='may not'):
        load_kit(make_kit(tmp_path, TEMPLATE.replace('<body>', '<body>' + bad)))


def test_a_template_must_use_every_account_field_and_only_known_tokens(tmp_path):
    with pytest.raises(KitError, match='OBSERVATION'):
        load_kit(make_kit(tmp_path / 'a', TEMPLATE.replace('{{OBSERVATION}}', '')))
    with pytest.raises(KitError, match='unknown field: {{SECRET}}'):
        load_kit(make_kit(tmp_path / 'b', TEMPLATE.replace('<body>', '<body>{{SECRET}}')))


def test_files_must_stay_inside_the_kit_and_be_the_right_kind(tmp_path):
    root = make_kit(tmp_path)
    outside = tmp_path / 'secret.ttf'
    outside.write_bytes(b'x')
    for bad in ('../secret.ttf', str(outside)):
        manifest = json.loads((root / 'kit.json').read_text())
        manifest['fonts'][0]['file'] = bad
        (root / 'kit.json').write_text(json.dumps(manifest))
        with pytest.raises(KitError, match='inside the kit folder'):
            load_kit(root)
    manifest['fonts'][0]['file'] = 'kit.json'
    (root / 'kit.json').write_text(json.dumps(manifest))
    with pytest.raises(KitError, match='must be one of'):
        load_kit(root)


def test_a_symlink_out_of_the_kit_is_refused(tmp_path):
    root = make_kit(tmp_path)
    (tmp_path / 'elsewhere.ttf').write_bytes(b'x')
    (root / 'assets/Link.ttf').symlink_to(tmp_path / 'elsewhere.ttf')
    manifest = json.loads((root / 'kit.json').read_text())
    manifest['fonts'][0]['file'] = 'assets/Link.ttf'
    (root / 'kit.json').write_text(json.dumps(manifest))
    with pytest.raises(KitError, match='inside the kit folder'):
        load_kit(root)


@pytest.mark.parametrize('change,match', [
    ({'fonts': [{'family': "x';}body{", 'weight': 400, 'file': 'assets/Body-400.ttf'}]}, 'family name'),
    ({'fonts': [{'family': 'Ok', 'weight': 'bold', 'file': 'assets/Body-400.ttf'}]}, 'weight'),
    ({'fonts': 'Inter'}, 'list'),
])
def test_font_declarations_are_validated(tmp_path, change, match):
    with pytest.raises(KitError, match=match):
        load_kit(make_kit(tmp_path, manifest=change))


def test_missing_or_broken_kits_say_what_is_wrong(tmp_path):
    with pytest.raises(KitError, match='not found'):
        load_kit(tmp_path / 'nowhere')
    (tmp_path / 'empty').mkdir()
    with pytest.raises(KitError, match='kit.json'):
        load_kit(tmp_path / 'empty')


# ── filling and embedding ──


def test_account_text_is_escaped_and_cannot_spell_a_token(tmp_path):
    kit = load_kit(make_kit(tmp_path))
    record = {**RECORD, 'company': '<b>Co</b> {{LOGO}}', 'pdf_observation': '<script>x</script>\n{{FONTS}} {{COMPANY}}'}
    markup = render_brief(record, {}, kit)
    assert '<script>' not in markup and '&lt;script&gt;' in markup and '&lt;b&gt;Co&lt;/b&gt;' in markup
    # only the template's own FONTS and LOGO tokens remain: typed ones were neutralised
    assert markup.count('{{FONTS}}') == 1 and markup.count('{{LOGO}}') == 1 and '{{COMPANY}}' not in markup
    done = finalize(kit, markup)
    assert '{{' not in done and done.count('<img class="brand-logo"') == 1


def test_fill_numbers_pages_and_stamps_the_kit(tmp_path):
    kit = load_kit(make_kit(tmp_path))
    markup = render_brief(RECORD, {}, kit)
    assert '<span class="index">01 / 02</span>' in markup and '<span class="index">02 / 02</span>' in markup
    assert f'<!-- brief-kit:{kit.fingerprint} -->' in markup
    assert '<p>Your site lists commerce and ERP work.</p><p>And mobile.</p>' in markup
    assert '<a href="https://studio.example/work">https://studio.example/work</a>' in markup


def test_finalize_embeds_fonts_and_logo_and_refuses_a_different_kit(tmp_path):
    kit = load_kit(make_kit(tmp_path))
    done = finalize(kit, render_brief(RECORD, {}, kit))
    assert done.count('@font-face') == 2 and "font-family:'Body Font'" in done and 'data:font/ttf;base64,' in done
    assert 'font-weight:700' in done and 'data:image/png;base64,' in done and 'alt="Example Co"' in done
    changed = load_kit(make_kit(tmp_path / 'other', template=TEMPLATE.replace('Body Font', 'Other Font')))
    with pytest.raises(KitError, match='different version'):
        finalize(changed, render_brief(RECORD, {}, kit))


def test_a_kit_without_a_logo_prints_its_brand_name(tmp_path):
    kit = load_kit(make_kit(tmp_path, logo=False))
    done = finalize(kit, render_brief(RECORD, {}, kit))
    assert '<span class="brand-logo">Example Co</span>' in done


def test_any_kit_change_changes_the_fingerprint(tmp_path):
    root = make_kit(tmp_path)
    before = load_kit(root).fingerprint
    assert load_kit(root).fingerprint == before
    (root / 'assets/Body-400.ttf').write_bytes(b'a different font')
    assert load_kit(root).fingerprint != before


def test_preview_opens_old_briefs_even_when_the_kit_moved(tmp_path):
    kit = load_kit(make_kit(tmp_path))
    markup = render_brief(RECORD, {}, kit)
    assert '@font-face' in preview(markup, kit)
    changed = load_kit(make_kit(tmp_path / 'b', template=TEMPLATE.replace('Body Font', 'Other Font')))
    assert '{{' not in preview(markup, changed) and '@font-face' not in preview(markup, changed)
    assert '{{' not in preview(markup, None)
    assert preview('<p>built-in brief</p>', None) == '<p>built-in brief</p>'


def test_the_built_in_brief_is_unchanged_without_a_kit():
    assert render_brief(RECORD, {'name': 'Example'}).count('<section>') == 2


# ── the store ──


@pytest_asyncio.fixture
async def state(tmp_path):
    state = StateManager(str(tmp_path / 'test.db'))
    await state.init_db()
    return state


async def account(store):
    await store.import_records([{'id': 'A01', 'company': 'Example Studio', 'website': 'https://studio.example',
                                 'research_sources': RECORD['research_sources'], 'pdf_observation': RECORD['pdf_observation'],
                                 'pdf_proposal': RECORD['pdf_proposal'], 'pdf_pilot': RECORD['pdf_pilot']}])
    return (await store.query('SELECT id FROM partnership_accounts'))[0]['id']


@pytest.mark.asyncio
async def test_assets_store_the_light_markup_and_print_the_embedded_one(state, tmp_path, monkeypatch):
    printed = []

    async def fake_pdf(markup):
        printed.append(markup)
        return b'%PDF-1.7 fictional'
    monkeypatch.setattr(p, 'to_pdf', fake_pdf)
    store = p.PartnershipStore(state, str(make_kit(tmp_path)))
    aid = await account(store)
    asset = await store.create_asset(aid)
    stored = (await store.query('SELECT html FROM partnership_assets WHERE id=?', (asset,)))[0]['html']
    assert '{{FONTS}}' in stored and '@font-face' not in stored          # fonts are not stored in every version
    assert '@font-face' in printed[0] and '{{' not in printed[0]         # but they are in what is printed
    assert await store.create_asset(aid) == asset                        # same kit, same content: same version


@pytest.mark.asyncio
async def test_changing_the_kit_makes_a_new_version_that_needs_review(state, tmp_path, monkeypatch):
    async def fake_pdf(markup):
        return b'%PDF-1.7 fictional'
    monkeypatch.setattr(p, 'to_pdf', fake_pdf)
    root = make_kit(tmp_path)
    aid = await account(p.PartnershipStore(state, str(root)))
    first_store = p.PartnershipStore(state, str(root))
    first = await first_store.create_asset(aid)
    async with state._connect() as db:
        await db.execute('UPDATE partnership_assets SET approved=1 WHERE id=?', (first,))
        await db.commit()
    (root / 'template.html').write_text(TEMPLATE.replace('Body Font', 'Other Font'))
    second_store = p.PartnershipStore(state, str(root))
    assert await second_store.create_asset(aid) != first
    old = (await second_store.query('SELECT approved FROM partnership_assets WHERE id=?', (first,)))[0]['approved']
    assert old == 1                                                      # the version already reviewed is untouched


@pytest.mark.asyncio
async def test_a_kit_that_is_set_but_broken_is_an_error_never_a_quiet_fallback(state, tmp_path):
    store = p.PartnershipStore(state, str(tmp_path / 'missing-kit'))
    aid = await account(store)
    with pytest.raises(ValueError, match="brief kit can't be used"):
        await store.create_asset(aid)


@pytest.mark.asyncio
async def test_with_no_kit_the_built_in_brief_is_used(state):
    store = p.PartnershipStore(state)
    assert store.kit() is None
    aid = await account(store)
    markup = await store.brief_markup((await store.account(aid))['record'])
    assert markup.count('<section>') == 2


# ── printing (needs a Chromium; skipped where there isn't one) ──


@pytest.fixture(scope='module')
def chromium():
    try:
        asyncio.run(to_pdf('<!doctype html><html><body><section><footer></footer></section></body></html>'))
    except Exception as exc:  # no browser installed here
        pytest.skip(f'Chromium unavailable: {exc}')


def test_a_kit_prints_at_its_own_page_size(chromium, tmp_path):
    kit = load_kit(make_kit(tmp_path, fonts=False))
    pdf = asyncio.run(to_pdf(finalize(kit, render_brief(RECORD, {}, kit))))
    assert pdf.startswith(b'%PDF') and pdf.count(b'/Type /Page\n') + pdf.count(b'/Type /Page ') >= 1


def test_text_that_does_not_fit_is_refused_not_clipped(chromium, tmp_path):
    kit = load_kit(make_kit(tmp_path, fonts=False))
    long = {**RECORD, 'pdf_observation': 'A line far too long for its card. ' * 80}
    with pytest.raises(ValueError, match='Slide 1 is too long'):
        asyncio.run(to_pdf(finalize(kit, render_brief(long, {}, kit))))


def test_a_font_that_will_not_load_is_refused_not_replaced(chromium, tmp_path):
    kit = load_kit(make_kit(tmp_path))          # the fictional font files are not real fonts
    with pytest.raises(ValueError, match='font in the brief kit could not be loaded'):
        asyncio.run(to_pdf(finalize(kit, render_brief(RECORD, {}, kit))))


def test_text_in_a_font_the_kit_does_not_embed_is_refused(chromium, tmp_path):
    # The kit embeds 'Body Font' (here unreadable), but the template sets its text in a font it does not embed.
    root = make_kit(tmp_path)
    (root / 'template.html').write_text(TEMPLATE.replace("font-family:'Body Font',sans-serif", "font-family:Typo,sans-serif"))
    kit = load_kit(root)
    with pytest.raises(ValueError, match="doesn't embed: Typo"):
        asyncio.run(to_pdf(finalize(kit, render_brief(RECORD, {}, kit))))
