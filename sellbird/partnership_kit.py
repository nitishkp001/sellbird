"""Brief template kits: the operator's own HTML, fonts and logo, kept on their machine.

A kit is a folder holding ``kit.json``, a template and the files it names. The
template is plain HTML with ``{{TOKENS}}``. Harvey fills the account's text into
it (escaped) and prints it with Chromium, so a brief looks like the rest of the
operator's material and uses the same fonts on every machine: they are embedded
in the page, never looked up by name at print time.

The template is served to the dashboard in a preview, so it may not run or
fetch anything: no scripts, event handlers, frames or links to other sites.

A kit can also vary by company type. ``kit.json`` lists the types (each with
keywords) and a default; the template wraps the parts that only suit some types in
``<!-- only: commerce, app -->`` ... ``<!-- /only -->``. A page or a row outside any
such wrapper is in every brief. The type is the one chosen on the account, or else the
best keyword match in the notes that describe the company.
"""
from __future__ import annotations

import base64
import hashlib
import html
import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

MAX_TEMPLATE = 500_000
MAX_ASSET = 3_000_000
MAX_TOTAL = 12_000_000
FONT_TYPES = {'.ttf': ('font/ttf', 'truetype'), '.otf': ('font/otf', 'opentype'),
              '.woff': ('font/woff', 'woff'), '.woff2': ('font/woff2', 'woff2')}
# Filled from the account, escaped. A template must use every one: leaving one out would drop
# what the operator wrote for this company.
FIELDS = ('COMPANY', 'OBSERVATION', 'COMPONENT', 'FIRST_ENGAGEMENT', 'SOURCES')
# Filled from the kit's own files when the brief is printed or previewed.
EMBEDS = ('FONTS', 'LOGO')
TOKEN = re.compile(r'\{\{([A-Z_]+)\}\}')
MARKER = re.compile(r'<!-- brief-kit:([0-9a-f]{64}) -->')
FAMILY = re.compile(r'[A-Za-z0-9][A-Za-z0-9 _-]{0,39}')
SEGMENT_ID = re.compile(r'[a-z][a-z0-9_-]{0,29}')
# Optional parts of a template. They don't nest: a page or a row is either in or out.
ONLY = re.compile(r'<!-- only: ?([^>]*?) ?-->([\s\S]*?)<!-- /only -->')
ONLY_OPEN = re.compile(r'<!-- only:')
ONLY_CLOSE = re.compile(r'<!-- /only -->')
# The account's own words about the company. Not what we propose to build for it: a studio we offer a
# payments component to is not a payments company.
DESCRIBES = ('research_notes', 'pdf_observation')
# Anything that would run, or fetch something, when the brief is previewed or printed. A fetch is
# refused too: printing is offline, so it would silently fall back to a different font or image.
FORBIDDEN = re.compile(
    r'<script|<iframe|<object|<embed|@import|javascript:|\son[a-z]+\s*=|'
    r'<link\b[^>]*\bhref=["\']?https?:|\bsrc=["\']?https?:|url\(\s*["\']?https?:', re.I)


class KitError(ValueError):
    """The kit can't be used. The message says what to fix."""


@dataclass(frozen=True)
class Segment:
    id: str
    label: str
    keywords: tuple


@dataclass(frozen=True)
class Kit:
    root: Path
    name: str
    brand: str
    template: str
    fonts: tuple          # (family, weight, style, path) for each font file
    logo: Path | None
    fingerprint: str
    segments: tuple = ()          # the company types the template can vary by
    default_segment: str = ''     # used when nothing matches


@lru_cache(maxsize=64)
def _digest(path: str, mtime_ns: int, size: int) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _file(root: Path, relative, kinds, label, budget):
    """A file inside the kit folder: it must exist, stay inside, be small and be of an allowed kind."""
    if not isinstance(relative, str) or not relative:
        raise KitError(f'{label} needs a file name.')
    try:
        path = (root / relative).resolve(strict=True)
    except OSError:
        raise KitError(f'{label}: {relative} was not found.')
    if root not in path.parents or not path.is_file():
        raise KitError(f'{label}: {relative} must be a file inside the kit folder.')
    if path.suffix.lower() not in kinds:
        raise KitError(f'{label}: {relative} must be one of {", ".join(sorted(kinds))}.')
    size = path.stat().st_size
    if size > MAX_ASSET:
        raise KitError(f'{label}: {relative} is larger than {MAX_ASSET // 1_000_000} MB.')
    budget[0] += size
    if budget[0] > MAX_TOTAL:
        raise KitError(f'The kit is larger than {MAX_TOTAL // 1_000_000} MB.')
    return path


def load_kit(folder) -> Kit:
    try:
        root = Path(folder).expanduser().resolve(strict=True)
    except OSError:
        raise KitError(f'The kit folder {folder} was not found.')
    try:
        manifest = json.loads((root / 'kit.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        raise KitError('kit.json is missing or is not valid JSON.')
    if not isinstance(manifest, dict):
        raise KitError('kit.json must be an object.')
    budget = [0]
    template_path = _file(root, manifest.get('template', 'template.html'), {'.html'}, 'The template', budget)
    if template_path.stat().st_size > MAX_TEMPLATE:
        raise KitError('The template is larger than 500 KB.')
    template = template_path.read_text(encoding='utf-8')
    bad = FORBIDDEN.search(template)
    if bad:
        raise KitError(f'The template contains {bad.group(0).strip()!r}, which a brief may not: '
                       'no scripts, event handlers, frames or links to other sites.')
    if not re.search(r'<head[\s>]', template, re.I):
        raise KitError('The template needs a <head> element.')
    used = set(TOKEN.findall(template))
    unknown = used - set(FIELDS) - set(EMBEDS)
    if unknown:
        raise KitError('The template uses an unknown field: {{' + sorted(unknown)[0] + '}}.')
    segments, default_segment = _segments(manifest)
    ids = {seg.id for seg in segments}
    blocks = ONLY.findall(template)
    if len(blocks) != len(ONLY_OPEN.findall(template)) or len(blocks) != len(ONLY_CLOSE.findall(template)):
        raise KitError('The template has an unbalanced or nested <!-- only: ... --> block. They cannot nest.')
    for listed, _ in blocks:
        named = [x.strip() for x in listed.split(',') if x.strip()]
        stray = [x for x in named if x not in ids]
        if not named or stray:
            raise KitError('A <!-- only: ... --> block names ' + (repr(stray[0]) if stray else 'no company type')
                           + ", which kit.json doesn't declare under segments.")
    # Whatever type a brief is for, the account's own text must still appear in it.
    for variant in ([seg.id for seg in segments] or ['']):
        missing = [f for f in FIELDS if f not in set(TOKEN.findall(_select(template, variant)))]
        if missing:
            raise KitError('The template must use {{' + missing[0] + '}} so what you write for the company appears'
                           + (f' (for the {variant} type it is inside an <!-- only: --> block that leaves it out).' if variant else '.'))
    fonts = []
    declared = manifest.get('fonts', [])
    if not isinstance(declared, list) or len(declared) > 20:
        raise KitError('fonts must be a list of at most 20 files.')
    for item in declared:
        if not isinstance(item, dict) or not isinstance(item.get('family'), str) or not FAMILY.fullmatch(item['family']):
            raise KitError('Each font needs a family name of letters, digits, spaces, - or _.')
        weight = item.get('weight', 400)
        style = item.get('style', 'normal')
        if not isinstance(weight, int) or isinstance(weight, bool) or not 100 <= weight <= 900 or style not in ('normal', 'italic'):
            raise KitError(f'The font {item["family"]} needs a weight from 100 to 900 and a normal or italic style.')
        fonts.append((item['family'], weight, style, _file(root, item.get('file'), set(FONT_TYPES), f'The font {item["family"]}', budget)))
    logo = _file(root, manifest['logo'], {'.png'}, 'The logo', budget) if manifest.get('logo') else None
    name = manifest.get('name', root.name)
    brand = manifest.get('brand', name)
    if not isinstance(name, str) or not isinstance(brand, str):
        raise KitError('name and brand must be text.')
    digest = hashlib.sha256()
    digest.update(json.dumps(manifest, sort_keys=True).encode())
    digest.update(template.encode())
    for path in [f[3] for f in fonts] + ([logo] if logo else []):
        stat = path.stat()
        digest.update(_digest(str(path), stat.st_mtime_ns, stat.st_size).encode())
    return Kit(root, name, brand, template, tuple(fonts), logo, digest.hexdigest(), segments, default_segment)


def _segments(manifest):
    """The company types in kit.json, and the default one."""
    declared = manifest.get('segments', [])
    if not isinstance(declared, list) or len(declared) > 12:
        raise KitError('segments must be a list of at most 12 company types.')
    segments = []
    for item in declared:
        if not isinstance(item, dict) or not isinstance(item.get('id'), str) or not SEGMENT_ID.fullmatch(item['id']):
            raise KitError('Each company type needs an id of lowercase letters, digits, - or _.')
        words = item.get('keywords', [])
        label = item.get('label', item['id'])
        if (not isinstance(label, str) or len(label) > 80 or not isinstance(words, list) or len(words) > 60
                or not all(isinstance(w, str) and 2 <= len(w.strip()) <= 40 for w in words)):
            raise KitError(f'The company type {item["id"]} needs a short label and up to 60 keywords of 2 to 40 characters.')
        if any(seg.id == item['id'] for seg in segments):
            raise KitError(f'The company type {item["id"]} is listed twice.')
        segments.append(Segment(item['id'], label, tuple(w.strip().lower() for w in words)))
    default = manifest.get('default_segment', '')
    if segments and default not in {seg.id for seg in segments}:
        raise KitError('default_segment must be one of the company types in segments.')
    if not segments and default:
        raise KitError('default_segment needs segments.')
    return tuple(segments), default


def _select(template: str, segment: str) -> str:
    """The template for one company type: the parts marked for it stay, the other marked parts go."""
    return ONLY.sub(lambda m: m.group(2) if segment in {x.strip() for x in m.group(1).split(',')} else '', template)


def choose_segment(kit: Kit, record: dict):
    """(company type, how it was decided). The type chosen on the account wins. Otherwise the type whose keywords
    appear most in the notes describing the company, the earlier-listed type winning a tie, else the default.
    How is 'chosen', 'suggested' or 'default'; a kit without types gives ('', 'none')."""
    if not kit.segments:
        return '', 'none'
    ids = [seg.id for seg in kit.segments]
    chosen = str(record.get('company_type') or '').strip().lower()
    if chosen:
        if chosen not in ids:
            raise KitError(f"The company type {chosen!r} isn't one of this kit's types ({', '.join(ids)}).")
        return chosen, 'chosen'
    best, score = '', 0
    for seg, hits in _scores(kit, record):
        if hits > score:
            best, score = seg.id, hits
    return (best, 'suggested') if best else (kit.default_segment, 'default')


def _scores(kit: Kit, record: dict):
    """(type, number of its keywords found) for each type, in the kit's order."""
    text = ' '.join(str(record.get(key) or '') for key in DESCRIBES).lower()
    return [(seg, sum(1 for word in seg.keywords if re.search(r'(?<![a-z0-9])' + re.escape(word) + r'(?![a-z0-9])', text)))
            for seg in kit.segments]


def close_call(kit: Kit, record: dict) -> str:
    """The label of the runner-up when a suggested type barely beat it (within one keyword), else ''.
    Agencies often describe several kinds of work, so a close call is worth deciding by hand."""
    ranked = sorted(_scores(kit, record), key=lambda pair: -pair[1])
    if len(ranked) > 1 and ranked[1][1] > 0 and ranked[0][1] - ranked[1][1] <= 1:
        return ranked[1][0].label
    return ''


def _safe(value) -> str:
    """Escaped text that can't spell a token, so nothing typed into an account is read as one."""
    return html.escape(str(value or '')).replace('{', '&#123;').replace('}', '&#125;')


def _paragraphs(value) -> str:
    return ''.join('<p>' + _safe(line) + '</p>' for line in str(value or '').split('\n') if line.strip())


def fill(kit: Kit, company, observation, component, first_engagement, sources, segment='') -> str:
    """The brief's markup for one account: its text in the template, stamped with the kit's fingerprint.
    ``segment`` is the company type: the template's parts marked for other types are left out.

    Fonts and logo stay as tokens: they are embedded only when printing or previewing, so each
    stored version stays small and the content hash still changes whenever the kit does."""
    values = {
        'COMPANY': _safe(company),
        'OBSERVATION': _paragraphs(observation),
        'COMPONENT': _paragraphs(component),
        'FIRST_ENGAGEMENT': _paragraphs(first_engagement),
        'SOURCES': ''.join(f'<a href="{_safe(u)}">{_safe(u)}</a>' for u in sources),
    }
    markup = TOKEN.sub(lambda m: values.get(m.group(1), m.group(0)), _select(kit.template, segment))
    # Page numbers follow however many slides the template has.
    spans = len(re.findall(r'<span class="index">\d+ / \d+</span>', markup))
    counter = iter(range(1, spans + 1))
    markup = re.sub(r'<span class="index">\d+ / \d+</span>',
                    lambda m: f'<span class="index">{next(counter):02d} / {spans:02d}</span>', markup)
    stamp = f'<!-- brief-kit:{kit.fingerprint} -->' + (f'<!-- brief-type:{segment} -->' if segment else '')
    return re.sub(r'(<head[^>]*>)', lambda m: m.group(1) + stamp, markup, count=1, flags=re.I)


def finalize(kit: Kit, markup: str) -> str:
    """Embed the kit's fonts and logo, ready to print. Refuses a brief made with a different kit."""
    stamp = MARKER.search(markup)
    if not stamp or stamp.group(1) != kit.fingerprint:
        raise KitError('This brief was made with a different version of the kit. Generate it again.')
    faces = []
    for family, weight, style, path in kit.fonts:
        mime, fmt = FONT_TYPES[path.suffix.lower()]
        data = base64.b64encode(path.read_bytes()).decode()
        faces.append(f"@font-face{{font-family:'{family}';src:url(data:{mime};base64,{data}) format('{fmt}');"
                     f"font-style:{style};font-weight:{weight};font-display:block}}")
    if kit.logo:
        data = base64.b64encode(kit.logo.read_bytes()).decode()
        logo = f'<img class="brand-logo" alt="{_safe(kit.brand)}" src="data:image/png;base64,{data}">'
    else:
        logo = f'<span class="brand-logo">{_safe(kit.brand)}</span>'
    done = markup.replace('{{FONTS}}', '\n'.join(faces)).replace('{{LOGO}}', logo)
    if TOKEN.search(done):
        raise KitError('The brief still has an unfilled field.')
    return done


def preview(markup: str, kit: Kit | None) -> str:
    """Markup for the dashboard preview. A brief from a kit that has since changed, or from a kit that
    can no longer be loaded, still opens, without the embedded fonts and logo."""
    if not MARKER.search(markup):
        return markup
    if kit is not None:
        try:
            return finalize(kit, markup)
        except KitError:
            pass
    return markup.replace('{{FONTS}}', '').replace('{{LOGO}}', '')
