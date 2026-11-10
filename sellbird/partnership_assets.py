"""Offline, escaped HTML briefs and deterministic PDF rendering: the built-in two pages, or a brief kit's own slides."""
from __future__ import annotations

import hashlib
import html
import re
import shutil
from urllib.parse import urlparse

from sellbird.partnership_kit import choose_segment, fill as fill_kit


def safe_url(value: str) -> str:
    value = (value or '').strip()
    parsed = urlparse(value)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
        return ''
    return value


def render_brief(record: dict, brand: dict, kit=None) -> str:
    """The brief's markup. With a kit, the account's text goes into the kit's template; without one, the built-in A4 brief."""
    def e(value):
        return html.escape(str(value or ''))
    def paragraphs(value):
        return ''.join('<p>'+e(s)+'</p>' for s in str(value or '').split('\n') if s.strip())
    company = record.get('company', '')
    observation = record.get('pdf_observation', '')
    proposal = record.get('pdf_proposal', '') or record.get('partnership_angle', '')
    pilot = record.get('pdf_pilot', '')
    if not all(str(v).strip() for v in (company, observation, proposal, pilot)):
        raise ValueError('Add an observation, proposed component and first engagement before generating a brief.')
    sources = [safe_url(u.strip()) for u in record.get('research_sources', '').split(' | ')]
    sources = list(dict.fromkeys(u for u in sources if u))[:3]
    if not sources:
        raise ValueError('Add at least one research source URL before generating a brief.')
    if kit is not None:
        return fill_kit(kit, company, observation, proposal, pilot, sources, choose_segment(kit, record)[0])
    logo = brand.get('logo_data_uri', '')
    if not logo.startswith('data:image/png;base64,') or len(logo) > 700000:
        logo = ''
    name = brand.get('name') or 'Engineering partner'
    mark = '<img alt="'+e(name)+'" src="'+e(logo)+'">' if logo else '<b>'+e(name)+'</b>'
    header = f'<header><div class="brand">{mark}</div><span>AGENCY PARTNERSHIPS</span></header>'
    sources_html = ' '.join(f'<a href="{e(u)}">{e(u)}</a>' for u in sources)
    contact = ' '.join(e(brand.get(k, '')) for k in ('contact_name', 'email', 'website'))
    calendar = safe_url(brand.get('calendar_url', ''))
    call = f'<a class="cta" href="{e(calendar)}">Arrange a conversation ↗</a>' if calendar else ''
    return f'''<!doctype html><html><head><meta charset="utf-8"><title>{e(company)} partnership brief</title>
<style>
@page{{size:A4 landscape;margin:0}}*{{box-sizing:border-box}}body{{margin:0;background:#0a0a0a;color:#ededed;font-family:Arial,sans-serif;-webkit-print-color-adjust:exact;print-color-adjust:exact}}section{{width:297mm;height:210mm;padding:15mm 18mm;position:relative;break-after:page;overflow:hidden}}section:last-child{{break-after:auto}}header{{display:flex;align-items:center;justify-content:space-between;border-bottom:1px solid #333;padding-bottom:14px;margin-bottom:25px}}header span,.eyebrow{{font:10px monospace;letter-spacing:2px;color:#999}}.brand{{display:flex;align-items:center;gap:10px;font-size:20px}}.brand img{{width:150px;height:40px;object-fit:contain;object-position:left center}}h1{{font-size:40px;line-height:1.12;margin:12px 0 18px;max-width:930px}}h2{{font-size:28px;margin:15px 0 24px}}h3{{font-size:17px;margin:0 0 12px}}p{{font-size:14px;line-height:1.5;margin:0 0 12px;overflow-wrap:anywhere}}.eyebrow{{color:#10b981}}.grid{{display:grid;grid-template-columns:1.1fr 1fr;gap:18px}}.card{{border:1px solid #333;border-radius:12px;background:#121212;padding:22px}}.accent{{border-top:3px solid #10b981}}.flow{{display:flex;align-items:center;gap:10px;margin-top:18px}}.node{{border:1px solid #35554b;color:#b8ecd9;border-radius:7px;padding:14px 10px;font-size:12px;flex:1;text-align:center}}.arrow{{color:#10b981}}.sources{{font-size:9px;color:#888;line-height:1.5;margin-top:16px;overflow-wrap:anywhere}}a{{color:#9adfc5}}.sources a{{display:block}}.three{{display:grid;grid-template-columns:repeat(3,1fr);gap:16px;margin:22px 0}}.three p{{font-size:13px}}.number{{font:12px monospace;color:#10b981;margin-bottom:12px}}footer{{position:absolute;bottom:12mm;left:18mm;right:18mm;display:flex;justify-content:space-between;color:#777;font-size:10px;border-top:1px solid #292929;padding-top:12px}}.muted{{color:#999;font-size:11px}}.cta{{display:inline-block;margin-top:12px;color:#0a0a0a;background:#10b981;border-radius:7px;padding:12px 20px;text-decoration:none;font-weight:bold}}.proof{{max-width:950px}}.contact{{margin-top:18px;color:#aaa;font-size:12px}}
</style></head><body>
<section>{header}<div class="eyebrow">A DISCUSSION PROPOSAL FOR {e(company).upper()}</div>
<h1>A backend partnership for<br>{e(company)}</h1>
<div class="grid"><div class="card"><h3>Why we reached out</h3>{paragraphs(observation)}<div class="sources">Research sources{sources_html}</div></div>
<div class="card accent"><h3>A component we could own</h3>{paragraphs(proposal)}<div class="flow"><div class="node">Your product &amp; client</div><span class="arrow">→</span><div class="node">Agreed backend component</div><span class="arrow">→</span><div class="node">Your review &amp; release</div></div></div></div>
<div style="margin-top:18px"><h3>A practical first engagement</h3>{paragraphs(pilot)}</div>
<footer><span>Proposed collaboration. Scope and suitability to be agreed.</span><span>01 / 02</span></footer></section>
<section>{header}<div class="eyebrow">HOW WE WOULD WORK TOGETHER</div><h2>Your client relationship.<br>A defined engineering responsibility.</h2>
<div class="proof">{paragraphs(brand.get('proof', ''))}<p class="muted">Employer references describe prior employment experience, not customers or endorsements.</p></div>
<div class="three"><div class="card"><div class="number">01 / SCOPE</div><h3>Agree the boundary</h3><p>Define the component, interfaces, acceptance criteria, fee and delivery plan together.</p></div><div class="card"><div class="number">02 / DELIVERY</div><h3>Work in your process</h3><p>Your team leads the wider project. We work in the agreed repository and communication channels.</p></div><div class="card"><div class="number">03 / HANDOVER</div><h3>Leave usable work</h3><p>Code, tests, documentation and handover are part of the agreed scope. Client pricing stays with you.</p></div></div>
<p>{e(brand.get('offer', 'A specialist engineering component delivered under your agency brand.'))}</p>{call}<div class="contact">{contact}</div>
<footer><span>{e(name)} · Confidential discussion draft</span><span>02 / 02</span></footer></section></body></html>'''


def brief_hash(markup: str) -> str:
    return hashlib.sha256(markup.encode()).hexdigest()


# Text that must sit inside its slide, above the footer line.
SLIDE_OVERFLOW = """() => {
  const found = [];
  for (const [i, slide] of [...document.querySelectorAll('.slide')].entries()) {
    const box = slide.getBoundingClientRect();
    for (const el of slide.querySelectorAll('h1,h2,h3,p,li,.eyebrow,.terminal-code')) {
      const r = el.getBoundingClientRect();
      if (el.scrollHeight > el.clientHeight + 2 || el.scrollWidth > el.clientWidth + 2 || r.right > box.right + 1 ||
          r.bottom > box.bottom - 38 || r.left < box.left - 1) { found.push(i + 1); break; }
    }
  }
  return found;
}"""


# The first font each piece of text asks for, when the kit embeds fonts and the text asks for another.
UNEMBEDDED_FONTS = """() => {
  const clean = name => name.replace(/["']/g, '').trim();
  const embedded = new Set([...document.fonts].map(f => clean(f.family)));
  const missing = new Set();
  if (!embedded.size) return [];
  for (const el of document.querySelectorAll('body *')) {
    if (![...el.childNodes].some(n => n.nodeType === 3 && n.textContent.trim())) continue;
    const first = clean(getComputedStyle(el).fontFamily.split(',')[0]);
    if (!embedded.has(first)) missing.add(first);
  }
  return [...missing];
}"""


async def _loaded_fonts(page):
    """Wait for the page's fonts and refuse a brief that would print in any other face: a font that failed
    to load, or text set in a font the kit doesn't embed (which falls back to whatever the machine has)."""
    await page.evaluate('document.fonts.ready')
    failed = await page.evaluate("[...document.fonts].filter(f => f.status === 'error').map(f => f.family)")
    if failed:
        raise ValueError('A font in the brief kit could not be loaded: ' + ', '.join(sorted(set(failed))) + '.')
    missing = await page.evaluate(UNEMBEDDED_FONTS)
    if missing:
        raise ValueError("The brief sets text in a font the kit doesn't embed: " + ', '.join(sorted(missing))
                         + '. It would print in a different face on another machine.')


async def _slides_to_pdf(browser, markup: str) -> bytes:
    """A kit's slides: checked on screen at full size, then printed from a fresh page at the template's own page size."""
    page = await browser.new_page(viewport={'width': 1440, 'height': 1000})
    await page.route('**/*', lambda route: route.abort())
    await page.set_content(markup, wait_until='load')
    await _loaded_fonts(page)
    too_long = await page.evaluate(SLIDE_OVERFLOW)
    if too_long:
        raise ValueError(f'Slide {too_long[0]} is too long. Shorten the observation, proposal or first engagement before exporting.')
    await page.close()
    printer = await browser.new_page(viewport={'width': 1280, 'height': 720})
    await printer.route('**/*', lambda route: route.abort())
    await printer.emulate_media(media='print')
    await printer.set_content(markup, wait_until='load')
    await _loaded_fonts(printer)
    return await printer.pdf(print_background=True, prefer_css_page_size=True, tagged=True)


async def to_pdf(markup: str) -> bytes:
    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        executable = shutil.which('google-chrome') or shutil.which('chromium')
        browser = await p.chromium.launch(headless=True, **({'executable_path': executable} if executable else {}))
        try:
            if re.search(r'<section class="slide[\s"]', markup):
                return await _slides_to_pdf(browser, markup)
            page = await browser.new_page()
            await page.route('**/*', lambda route: route.abort())
            await page.emulate_media(media='print')
            await page.set_content(markup, wait_until='load')
            overflow = await page.evaluate('''() => [...document.querySelectorAll('section')].some(s => [...s.children].filter(x=>x.tagName!=='FOOTER').some(x=>x.getBoundingClientRect().bottom > s.querySelector('footer').getBoundingClientRect().top-8))''')
            if overflow:
                raise ValueError('This brief is too long. Shorten the observation, proposal or proof before exporting.')
            return await page.pdf(format='A4', landscape=True, print_background=True, prefer_css_page_size=True)
        finally:
            await browser.close()
