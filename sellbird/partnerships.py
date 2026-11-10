"""Partnership account workflow, coordinated tasks and immutable prospect briefs.

This module adds no external dispatch. Emails enter the existing Outbox and
retain its approval, verification and reply/suppression checks.
"""
from __future__ import annotations

import calendar
import csv
import io
import json
import logging
import os
import re
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from urllib.parse import urlparse

import aiosqlite
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, Response

from sellbird.partnership_assets import brief_hash, render_brief, safe_url, to_pdf
from sellbird.partnership_kit import MARKER, SEGMENT_ID, KitError, choose_segment, close_call, finalize, load_kit, preview as preview_markup

logger = logging.getLogger("sellbird.partnerships")

SCHEMA = '''
CREATE TABLE IF NOT EXISTS partnership_accounts (
 id TEXT PRIMARY KEY, company_id TEXT NOT NULL UNIQUE REFERENCES companies(id),
 prospect_id TEXT NOT NULL DEFAULT '', external_id TEXT NOT NULL UNIQUE,
 record_json TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS partnership_events (
 id TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES partnership_accounts(id),
 kind TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '', occurred_at TEXT NOT NULL,
 idempotency_key TEXT NOT NULL UNIQUE);
CREATE TABLE IF NOT EXISTS partnership_assets (
 id TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES partnership_accounts(id),
 version INTEGER NOT NULL, content_hash TEXT NOT NULL, html TEXT NOT NULL,
 pdf BLOB, approved INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL,
 UNIQUE(account_id, version));
CREATE TABLE IF NOT EXISTS partnership_email_bindings (
 outbox_id TEXT PRIMARY KEY REFERENCES outbox(id), account_id TEXT NOT NULL,
 asset_id TEXT NOT NULL DEFAULT '', snapshot_hash TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS partnership_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS partnership_event_account ON partnership_events(account_id, occurred_at);
'''
STOP = {'replied', 'positive_reply', 'declined', 'do_not_contact', 'call_booked', 'brief_received'}
EVENTS = {'linkedin_requested', 'linkedin_accepted', 'linkedin_declined', 'human_reply', 'positive_reply', 'call_booked', 'brief_received', 'do_not_contact', 'note'}
EDITABLE = {'company', 'city', 'region', 'size_claimed', 'research_date', 'contact_route', 'target_person', 'target_role', 'target_profile', 'direct_email', 'business_inbox', 'stage', 'next_action', 'followup_date', 'partnership_angle', 'research_sources', 'research_notes', 'pdf_observation', 'pdf_proposal', 'pdf_pilot', 'email_subject', 'email_body', 'loom_url', 'video_script', 'company_type'}


CAMPAIGN_PREFIX = 'partnership-'
HELD_REASON = 'this company is in the partnership track: it is written to one to one, from Partnerships'
# A contact in one of these states has answered or opted out: not a cold prospect any more.
ENGAGED = {'opted_out', 'replied', 'interested', 'meeting', 'closed'}
# Contact states the track takes over. Anything else (contacted, replied, opted out...) says something true.
ADOPTABLE = ('new', 'queued', 'write_failed')


def configured_kit_dir():
    """The brief kit folder from partnerships.brief_kit in the config, or '' for the built-in brief."""
    from sellbird.config import ConfigError, load_config
    try:
        return load_config().partnerships.brief_kit
    except ConfigError:
        return ''


def configured_output_dir():
    """Where generated briefs are saved as files (partnerships.output_dir), or '' for the default beside the database."""
    from sellbird.config import ConfigError, load_config
    try:
        return load_config().partnerships.output_dir
    except ConfigError:
        return ''


def file_slug(text, fallback='company'):
    """A company name as a safe file name: lowercase letters, digits and hyphens."""
    return re.sub(r'[^a-z0-9]+', '-', str(text or '').lower()).strip('-')[:40].strip('-') or fallback


def safe_part(text):
    """A tracker ID as a safe file name part. No dots at the edges, so it can never be '..'."""
    return re.sub(r'[^A-Za-z0-9._-]+', '-', str(text or '')).strip('.-')[:40] or 'account'


def now():
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat()


def today():
    return datetime.now(ZoneInfo('Asia/Kolkata')).date()


def default_pilot():
    start = today()
    month = start.month % 12 + 1
    year = start.year + (start.month == 12)
    end = date(year, month, min(start.day, calendar.monthrange(year, month)[1])) - timedelta(days=1)
    return {'status': 'draft', 'start_date': start.isoformat(), 'end_date': end.isoformat(), 'timezone': 'Asia/Kolkata'}


def active(pilot):
    return pilot.get('status') == 'active' and date.fromisoformat(pilot['start_date']) <= today() <= date.fromisoformat(pilot['end_date'])


def normalized_domain(url):
    value = safe_url(url)
    if not value:
        raise ValueError('Every account needs a valid HTTP(S) company website.')
    return urlparse(value).hostname.lower().removeprefix('www.')


def parse_import(payload):
    records = payload.get('records')
    if 'csv' in payload:
        raw = payload['csv']
        if not isinstance(raw, str) or len(raw) > 4_000_000:
            raise ValueError('CSV must be text smaller than 4 MB.')
        records = list(csv.DictReader(io.StringIO(raw.lstrip('\ufeff'))))
    if not isinstance(records, list) or not 1 <= len(records) <= 1000:
        raise ValueError('Import between 1 and 1,000 records.')
    seen_domains, seen_ids, clean = set(), set(), []
    for item in records:
        if not isinstance(item, dict):
            raise ValueError('Each account must be an object.')
        row = {str(k): str(v or '').strip() for k, v in item.items()}
        if len(json.dumps(row)) > 60_000:
            raise ValueError('An account record is too large.')
        if not row.get('company') or not row.get('id'):
            raise ValueError('Each row needs id and company.')
        domain = normalized_domain(row.get('website', ''))
        if domain in seen_domains or row['id'] in seen_ids:
            raise ValueError('Duplicate company domain or external ID in import.')
        seen_domains.add(domain); seen_ids.add(row['id']); clean.append(row)
    return clean


async def _cancel_general_outbox(db, company_id=None):
    """Cancel cold sequence emails still waiting at partnership companies (one
    company, or all of them). Sent and in-flight emails, replies and the track's
    own emails are left alone."""
    scope = ' AND a.company_id=?' if company_id else ''
    cur = await db.execute(
        "UPDATE outbox SET status='cancelled', error=?, updated_at=? "
        "WHERE status IN ('pending_review','approved') AND kind='sequence' "
        "AND campaign_id NOT LIKE ? AND prospect_id IN ("
        "SELECT p.id FROM prospects p JOIN partnership_accounts a ON a.company_id=p.company_id "
        "WHERE p.company_id!=''" + scope + ")",
        (HELD_REASON, now(), CAMPAIGN_PREFIX + '%', *((company_id,) if company_id else ())))
    return cur.rowcount


async def release_general_outbox(state, company_id=None):
    """How many waiting cold emails were cancelled because their company is in the partnership track."""
    async with state._connect() as db:
        count = await _cancel_general_outbox(db, company_id)
        await db.commit()
    return count


async def _adopt_prospect(db, prospect_id):
    """Mark a contact the track now owns, unless its status already says something true."""
    marks = ','.join('?' for _ in ADOPTABLE)
    await db.execute(f"UPDATE prospects SET status='partnership' WHERE id=? AND status IN ({marks})", (prospect_id, *ADOPTABLE))


class PartnershipStore:
    def __init__(self, state, kit_dir='', output_dir=''):
        self.state = state
        self.kit_dir = kit_dir
        self.output_dir = output_dir

    def kit(self):
        """The configured brief kit, or None for the built-in brief. A kit that is set but unusable is an
        error, not a fallback: a brief in a different design should never go out unnoticed."""
        if not self.kit_dir:
            return None
        try:
            return load_kit(self.kit_dir)
        except KitError as exc:
            raise ValueError(f"Your brief kit can't be used: {exc}") from exc

    def output_root(self):
        """The folder briefs are saved in: the configured one, else beside the database. None for an in-memory database."""
        if self.output_dir:
            return Path(self.output_dir).expanduser()
        db = str(self.state.db_path)
        return None if db.startswith(':') else Path(db).parent / 'partnership-briefs'

    async def save_files(self, aid, asset_id):
        """Save a brief version as files named for the company: the personalised HTML exactly as it was printed,
        and the PDF made from it. They sit in a folder per account (tracker ID and company), one pair per version.
        The files are outputs: to change a brief, edit the account and generate a new version.
        Returns the paths, or None when there is nowhere to save."""
        root = self.output_root()
        if root is None:
            return None
        a = await self.account(aid)
        rows = await self.query('SELECT version,html,pdf FROM partnership_assets WHERE id=? AND account_id=?', (asset_id, aid))
        if not rows or not rows[0]['pdf']:
            raise ValueError('That brief version was not found.')
        row, markup = rows[0], rows[0]['html']
        if MARKER.search(markup):
            kit = self.kit()
            try:
                markup = finalize(kit, markup) if kit else None
            except KitError:
                markup = None
            if markup is None:
                raise ValueError("This version was made with a different brief kit, so its HTML can't be reproduced exactly. Generate it again.")
        stem = safe_part(a['external_id']) + '-' + file_slug(a['record'].get('company'))
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        folder = root / stem
        folder.mkdir(mode=0o700, exist_ok=True)
        if folder.is_symlink() or folder.resolve().parent != root.resolve():
            raise ValueError('The brief folder must sit directly inside the output folder.')
        saved = {}
        for kind, data in (('html', markup.encode('utf-8')), ('pdf', bytes(row['pdf']))):
            target = folder / f"{stem}-brief-v{row['version']}.{kind}"
            if not (target.is_file() and target.read_bytes() == data):
                temp = target.with_name(target.name + '.tmp')
                with os.fdopen(os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), 'wb') as handle:
                    handle.write(data)
                os.replace(temp, target)
            saved[kind] = str(target)
        async with self.state._connect() as db:
            await db.execute('INSERT OR IGNORE INTO partnership_events VALUES (?,?,?,?,?,?)',
                             (uuid.uuid4().hex[:12], aid, 'note', f"Brief v{row['version']} saved as HTML and PDF in {folder}", now(), f'files:{asset_id}'))
            await db.commit()
        return {'folder': str(folder), **saved}

    def brief_plan(self, record):
        """For the dashboard: the company type the brief will use and how it was decided, with the kit's types.
        None when the kit has no types. ``error`` says why a type can't be used."""
        try:
            kit = self.kit()
        except ValueError:
            return None
        if kit is None or not kit.segments:
            return None
        plan = {'segments': [{'id': x.id, 'label': x.label} for x in kit.segments]}
        try:
            kind, how = choose_segment(kit, record)
        except KitError as exc:
            return {**plan, 'error': str(exc)}
        close = close_call(kit, record) if how == 'suggested' else ''
        return {**plan, 'segment': kind, 'label': next(x.label for x in kit.segments if x.id == kind), 'how': how, 'close': close}

    async def brief_markup(self, record):
        return render_brief(record, await self.setting('brand', {}), self.kit())

    async def query(self, sql, params=()):
        async with self.state._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql, params) as cur:
                return [dict(r) for r in await cur.fetchall()]

    async def setting(self, key, default):
        rows = await self.query('SELECT value FROM partnership_settings WHERE key=?', (key,))
        return json.loads(rows[0]['value']) if rows else default

    async def set_setting(self, key, value):
        async with self.state._connect() as db:
            await db.execute('INSERT INTO partnership_settings VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, json.dumps(value)))
            await db.commit()

    async def preview_import(self, records):
        result = []
        for row in records:
            domain = normalized_domain(row['website'])
            matches = await self.query("SELECT id FROM companies WHERE lower(domain) IN (?,?)", (domain,'www.'+domain))
            accounts = await self.query('SELECT id,company_id FROM partnership_accounts WHERE external_id=?', (row['id'],))
            if len(matches) > 1 or (accounts and (not matches or accounts[0]['company_id'] != matches[0]['id'])):
                raise ValueError('An imported identity conflicts with an existing company. Resolve the external ID/domain before importing.')
            by_company=await self.query('SELECT id FROM partnership_accounts WHERE company_id=?',(matches[0]['id'],)) if matches else []
            result.append({'id': row['id'], 'company': row['company'], 'domain': domain, 'action': 'preserve existing account' if accounts or by_company else 'link company' if matches else 'create company'})
        return result

    async def import_records(self, records):
        preview = await self.preview_import(records)
        created = 0
        async with self.state._connect() as db:
            await db.execute('BEGIN IMMEDIATE')
            for row in records:
                domain = normalized_domain(row['website'])
                cur = await db.execute('SELECT id,company_id FROM partnership_accounts WHERE external_id=?', (row['id'],))
                existing = await cur.fetchone()
                if existing:
                    continue  # A repeat import never resets operator edits or event history.
                cur = await db.execute('SELECT id FROM companies WHERE lower(domain) IN (?,?)', (domain,'www.'+domain))
                match = await cur.fetchone()
                cid = match[0] if match else uuid.uuid4().hex[:12]
                if match:
                    cur=await db.execute('SELECT id FROM partnership_accounts WHERE company_id=?',(cid,))
                    if await cur.fetchone():continue
                if not match:
                    await db.execute('INSERT INTO companies (id,name,domain,website,location,source,notes) VALUES (?,?,?,?,?,?,?)', (cid,row['company'],domain,row['website'],row.get('city',''),'partnership_import',row.get('research_notes','')))
                pid = ''
                name = row.get('target_person','')
                email = row.get('direct_email','').lower()
                profile = row.get('target_profile','')
                if name:
                    candidates = []
                    if email:
                        cur = await db.execute('SELECT id,company_id FROM prospects WHERE lower(email)=?', (email,)); candidates += await cur.fetchall()
                    if profile:
                        cur = await db.execute('SELECT id,company_id FROM prospects WHERE linkedin_url=?', (profile,)); candidates += await cur.fetchall()
                    ids = {r[0] for r in candidates}
                    if len(ids)>1 or any(r[1] and r[1]!=cid for r in candidates):
                        raise ValueError('A contact identity belongs to another account. Import was rolled back.')
                    if candidates:
                        pid = candidates[0][0]
                    else:
                        first, _, last = name.partition(' ')
                        pid = uuid.uuid4().hex[:12]
                        await db.execute('INSERT INTO prospects (id,company_id,first_name,last_name,email,email_status,linkedin_url,title,source,source_url,status,company,personalization_notes) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)', (pid,cid,first,last,email,'guess' if email else '',profile,row.get('target_role',''),'partnership_import',row.get('contact_route',''),'partnership',row['company'],row.get('partnership_angle','')))
                aid = uuid.uuid4().hex[:12]
                await db.execute('INSERT INTO partnership_accounts VALUES (?,?,?,?,?,?)', (aid,cid,pid,row['id'],json.dumps(row),now()))
                # The company now has one voice: drafts the template pipeline already queued are withdrawn.
                if pid:
                    await _adopt_prospect(db, pid)
                await _cancel_general_outbox(db, cid)
                if row.get('linkedin_touch_date'):
                    await db.execute('INSERT INTO partnership_events VALUES (?,?,?,?,?,?)', (uuid.uuid4().hex[:12],aid,'legacy_linkedin',row.get('linkedin_status','Acceptance unknown'),row['linkedin_touch_date'],f'import:{aid}:linkedin'))
                created += 1
            await db.commit()
        return {'created': created, 'preserved': len(records)-created, 'preview': preview}

    async def account(self, aid):
        rows = await self.query('SELECT * FROM partnership_accounts WHERE id=?', (aid,))
        if not rows:
            raise HTTPException(404, 'Partnership account not found.')
        raw = rows[0]
        record = json.loads(raw.pop('record_json'))
        events = await self.query('SELECT * FROM partnership_events WHERE account_id=? ORDER BY occurred_at DESC,id DESC', (aid,))
        assets = await self.query('SELECT id,version,content_hash,approved,created_at FROM partnership_assets WHERE account_id=? ORDER BY version DESC', (aid,))
        emails = await self.query('SELECT o.id,o.campaign_id,o.subject,o.status,o.sent_at,o.error,o.step,b.asset_id FROM partnership_email_bindings b JOIN outbox o ON o.id=b.outbox_id WHERE b.account_id=? ORDER BY o.created_at DESC', (aid,))
        prospect = await self.state.get_prospect(raw['prospect_id']) if raw['prospect_id'] else None
        stopped = bool(prospect and prospect.status in {'replied','opted_out','lost','meeting','closed'}) or any(e['kind'] in STOP or e['kind']=='human_reply' for e in events) or record.get('stage','').strip().lower() in {'paused','declined','do not contact'}
        return {**raw,'record':record,'events':events,'assets':assets,'emails':emails,'email_verification':prospect.email_status if prospect else '', 'contact_status':prospect.status if prospect else '', 'stopped':stopped}

    async def update(self, aid, changes):
        account = await self.account(aid)
        if not isinstance(changes,dict) or set(changes)-EDITABLE:
            raise ValueError('Unknown account fields.')
        clean = {}
        for k,v in changes.items():
            if not isinstance(v,str) or len(v)>12000:
                raise ValueError('Fields must be text, at most 12,000 characters.')
            clean[k] = v.strip()
        if 'followup_date' in clean and clean['followup_date']:
            date.fromisoformat(clean['followup_date'])
        if clean.get('research_date'):
            date.fromisoformat(clean['research_date'])
        if clean.get('company_type'):
            kind = clean['company_type'] = clean['company_type'].lower()
            try:
                kit = self.kit()
            except ValueError:
                kit = None          # a broken kit is reported when a brief is generated, not when a field is saved
            if not SEGMENT_ID.fullmatch(kind) or (kit and kit.segments and kind not in {x.id for x in kit.segments}):
                raise ValueError('Choose one of the company types the brief kit offers, or leave it blank to have one suggested.')
        if 'company' in clean and not clean['company']:
            raise ValueError('Company name cannot be blank.')
        for k in ('target_profile','loom_url'):
            if clean.get(k) and not safe_url(clean[k]):
                raise ValueError('Profile and video links must be HTTP(S) URLs.')
        record = {**account['record'],**clean}
        if record.get('direct_email') and not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+',record['direct_email']):
            raise ValueError('Enter a valid direct business email.')
        pid = account['prospect_id']
        if {'target_person','target_role','target_profile','direct_email'} & set(clean) and record.get('target_person'):
            from sellbird.models.prospect import Prospect
            first,_,last=record['target_person'].partition(' ')
            previous=await self.state.get_prospect(pid) if pid else None
            email=record.get('direct_email','').lower()
            matching=await self.state.get_prospect_by_email(email) if email else None
            if matching and matching.id!=pid:
                raise ValueError('That email already belongs to another contact. Resolve the identity in Contacts first.')
            if previous:
                async with self.state._connect() as db:
                    await db.execute('UPDATE prospects SET first_name=?,last_name=?,title=?,linkedin_url=?,email=?,email_status=?,email_verified=? WHERE id=?',(first,last,record.get('target_role',''),record.get('target_profile',''),email,previous.email_status if previous.email==email else 'guess',int(previous.email_verified and previous.email==email),pid))
                    await db.commit()
            else:
                pid=await self.state.add_prospect(Prospect(company_id=account['company_id'],first_name=first,last_name=last,title=record.get('target_role',''),email=email,email_status='guess' if email else '',linkedin_url=record.get('target_profile',''),company=record['company'],source='partnership_import',status='partnership'))
        async with self.state._connect() as db:
            await db.execute('UPDATE partnership_accounts SET record_json=?,prospect_id=?,updated_at=? WHERE id=?', (json.dumps(record),pid,now(),aid))
            if 'company' in clean or 'city' in clean:
                await db.execute('UPDATE companies SET name=?,location=? WHERE id=?',(record['company'],record.get('city',''),account['company_id']))
                await db.execute('UPDATE prospects SET company=? WHERE company_id=?',(record['company'],account['company_id']))
            # Material changes invalidate only the current review, never an already shared immutable asset.
            await db.commit()
        return await self.account(aid)

    async def event(self, aid, payload):
        a = await self.account(aid)
        kind = payload.get('kind')
        if kind not in EVENTS:
            raise ValueError('Unknown event type. LinkedIn messages are not part of this pilot.')
        stamp = payload.get('occurred_at') or now()
        datetime.fromisoformat(stamp)
        detail = str(payload.get('detail',''))[:3000]
        key = str(payload.get('idempotency_key') or '')
        if not key or len(key)>200:
            raise ValueError('An idempotency key is required.')
        if kind=='linkedin_requested':
            if not active(await self.setting('pilot',default_pilot())) or a['stopped']:
                raise ValueError('The pilot is inactive, expired or this account has replied/stopped.')
            if not a['record'].get('target_profile'):
                raise ValueError('Add the researched LinkedIn profile first.')
            previous=[e for e in a['events'] if e['kind']=='linkedin_requested']
            if previous and all(e['idempotency_key']!=key for e in previous):
                raise ValueError('A connection request is already recorded. Record acceptance separately.')
        async with self.state._connect() as db:
            await db.execute('INSERT OR IGNORE INTO partnership_events VALUES (?,?,?,?,?,?)', (uuid.uuid4().hex[:12],aid,kind,detail,stamp,key))
            if kind in STOP or kind=='human_reply':
                if a['prospect_id']:
                    status = 'opted_out' if kind=='do_not_contact' else 'lost' if kind=='declined' else 'replied'
                    await db.execute("UPDATE prospects SET status=? WHERE id=? AND status!='opted_out'", (status,a['prospect_id']))
                await db.execute("UPDATE outbox SET status='cancelled',error='partnership account replied or stopped' WHERE id IN (SELECT outbox_id FROM partnership_email_bindings WHERE account_id=?) AND status IN ('pending_review','approved')",(aid,))
            await db.commit()
        return await self.account(aid)

    async def promote(self, company_id, prospect_id='', fit=None):
        """Move a company from the cold-email pipeline into the partnership track.

        The best named contact is linked as the decision maker, and the
        existing contact record is kept, so its email verification survives.
        Fit reasons, company notes and the writer's notes become research
        notes; the observation and proposal stay blank for you to write.
        Cold emails already drafted for the company are cancelled. ``fit`` is
        ``{'fit': 0-100, 'reasons': [...]}`` when the caller has it."""
        companies = await self.query('SELECT * FROM companies WHERE id=?', (company_id,))
        if not companies:
            raise HTTPException(404, 'Company not found.')
        company = companies[0]
        found = await self.query('SELECT id FROM partnership_accounts WHERE company_id=?', (company_id,))
        if found:
            return await self.account(found[0]['id'])
        website = safe_url(company.get('website') or ('https://' + company['domain'] if company.get('domain') else ''))
        if not website:
            raise ValueError('This company has no website, which every partnership account needs.')
        people = await self.query('SELECT * FROM prospects WHERE company_id=?', (company_id,))
        if any(p['status'] in ENGAGED for p in people):
            raise ValueError('Someone at this company already replied or asked not to be emailed. Handle it from Conversations instead.')
        if prospect_id:
            chosen = next((p for p in people if p['id'] == prospect_id), None)
            if not chosen:
                raise ValueError('That contact does not belong to this company.')
        else:
            named = [p for p in people if (p['first_name'] or '').strip() and p['status'] != 'lost']
            rank = {'verified': 0, 'risky': 1, 'guess': 2}
            named.sort(key=lambda p: (-(p['score'] or 0), rank.get(p['email_status'], 3)))
            chosen = named[0] if named else None
        name = ' '.join(x for x in (chosen['first_name'], chosen['last_name']) if x).strip() if chosen and (chosen['first_name'] or '').strip() else ''
        email = ((chosen['email'] if chosen else '') or '').strip().lower()
        notes = (chosen['personalization_notes'] if chosen else '') or ''
        inbox = ''
        if not name:
            # No named person: a general inbox is kept apart from the decision maker, as an import does.
            general = chosen or next((p for p in people if (p['email'] or '').strip() and not (p['first_name'] or '').strip() and p['status'] != 'lost'), None)
            inbox = ((general['email'] if general else '') or '').strip().lower()
        prior =(await self.query("SELECT COUNT(*) n FROM outbox o JOIN prospects p ON p.id=o.prospect_id WHERE p.company_id=? AND o.status='sent' AND o.campaign_id NOT LIKE ?", (company_id, CAMPAIGN_PREFIX + '%')))[0]['n']
        if prior:
            stage, action = 'Review prior outreach', f'{prior} cold email(s) already went to this company. Check the mailbox before the personal pitch.'
        elif not name or not email:
            stage, action = 'Needs contact research', "Research the decision maker and find their direct business email, then verify it."
        else:
            stage, action = 'Needs research', 'Write the sourced observation and proposed component, then verify the email.'
        reasons = ' · '.join((fit or {}).get('reasons') or [])
        sections = [
            f"Fit {fit['fit']}/100" + (f': {reasons}' if reasons else '') if fit and isinstance(fit.get('fit'), int) else '',
            (company.get('notes') or '').strip(),
            f"Notes on {name or 'the contact'} from the pipeline:\n{notes.strip()}" if notes.strip() else '',
        ]
        angle = re.search(r'^[ \t]*[^\n:]{1,40}\bAngle:[ \t]*(.+)$', notes, re.M)
        sources = list(dict.fromkeys(u for u in (website, safe_url((chosen or {}).get('source_url') or ''), safe_url(company.get('source_url') or '')) if u))
        aid, external = uuid.uuid4().hex[:12], 'P' + uuid.uuid4().hex[:8]
        record = {'id': external, 'company': company['name'] or company.get('domain', ''), 'website': website, 'city': company.get('location') or '', 'region': '',
                  'target_person': name, 'target_role': (chosen['title'] if name else '') or '', 'target_profile': safe_url((chosen['linkedin_url'] if name else '') or ''),
                  'direct_email': email if name else '', 'business_inbox': inbox,
                  'contact_route': safe_url((chosen or {}).get('source_url') or ''),
                  'research_sources': ' | '.join(sources), 'research_notes': '\n\n'.join(x for x in sections if x)[:6000],
                  'partnership_angle': angle.group(1).strip()[:600] if angle else '',
                  'research_date': today().isoformat(), 'stage': stage, 'next_action': action}
        existing = ''
        async with self.state._connect() as db:
            await db.execute('BEGIN IMMEDIATE')
            cur = await db.execute('SELECT id FROM partnership_accounts WHERE company_id=?', (company_id,))
            row = await cur.fetchone()
            if row:
                existing = row[0]
                await db.rollback()
            else:
                pid = chosen['id'] if name else ''
                await db.execute('INSERT INTO partnership_accounts VALUES (?,?,?,?,?,?)', (aid, company_id, pid, external, json.dumps(record), now()))
                if pid:
                    await _adopt_prospect(db, pid)
                released = await _cancel_general_outbox(db, company_id)
                detail = 'Moved from Companies.' + (f" Fit {fit['fit']}/100." if fit and isinstance(fit.get('fit'), int) else '') + (f' Cancelled {released} cold email(s) already drafted.' if released else '')
                await db.execute('INSERT INTO partnership_events VALUES (?,?,?,?,?,?)', (uuid.uuid4().hex[:12], aid, 'note', detail, now(), f'promote:{aid}'))
                await db.commit()
        return await self.account(existing or aid)

    async def create_asset(self, aid):
        a = await self.account(aid)
        markup = await self.brief_markup(a['record'])
        fingerprint = brief_hash(markup)
        same = await self.query('SELECT id FROM partnership_assets WHERE account_id=? AND content_hash=? ORDER BY version DESC LIMIT 1', (aid,fingerprint))
        if same:
            return same[0]['id']
        kit = self.kit()
        pdf = await to_pdf(finalize(kit, markup) if kit else markup)
        asset = uuid.uuid4().hex[:12]
        async with self.state._connect() as db:
            await db.execute('BEGIN IMMEDIATE')
            cur = await db.execute('SELECT COALESCE(MAX(version),0)+1 FROM partnership_assets WHERE account_id=?',(aid,)); version = (await cur.fetchone())[0]
            await db.execute('INSERT INTO partnership_assets VALUES (?,?,?,?,?,?,0,?)',(asset,aid,version,fingerprint,markup,pdf,now()))
            await db.commit()
        return asset

    async def stage_email(self, aid, asset_id=''):
        a = await self.account(aid)
        pilot = await self.setting('pilot',default_pilot())
        r = a['record']
        if not active(pilot) or a['stopped']:
            raise ValueError('Activate the dated pilot first. Replied/stopped accounts cannot be queued.')
        if not a['prospect_id'] or not r.get('direct_email') or not r.get('email_subject','').strip() or not r.get('email_body','').strip():
            raise ValueError('A named contact, direct email, subject and personalized body are required.')
        prospect = await self.state.get_prospect(a['prospect_id'])
        if prospect.email.lower()!=r['direct_email'].lower():
            raise ValueError('The email differs from the linked contact. Update/verify the contact in Contacts before queuing.')
        if await self.state.is_suppressed(prospect.email):
            raise ValueError('This contact is suppressed.')
        asset_hash = ''
        if asset_id:
            rows = await self.query('SELECT * FROM partnership_assets WHERE id=? AND account_id=?',(asset_id,aid))
            current_hash = brief_hash(await self.brief_markup(r))
            if not rows or not rows[0]['approved'] or rows[0]['content_hash']!=current_hash:
                raise ValueError('Generate and approve the current PDF version first.')
            asset_hash = rows[0]['content_hash']
        existing = [e for e in a['emails'] if e['status'] not in ('cancelled','failed')]
        if any(e['status'] in ('pending_review','approved','sending') for e in existing):
            raise ValueError('An email is already waiting or being sent for this account.')
        if any(str(e.get('error','')).startswith('may have gone out:') for e in a['emails']):
            raise ValueError('An earlier email may have been sent. Check the mailbox before any further outreach.')
        sent=[e for e in a['emails'] if e['status']=='sent']
        step = 1 + max([e['step'] for e in sent],default=0)
        if step>4:
            raise ValueError('The four-email pilot limit has been reached.')
        cid = sent[0]['campaign_id'] if sent else 'partnership-'+aid+'-'+uuid.uuid4().hex[:6]
        send_at=now()
        touches=[e['sent_at'] for e in sent if e.get('sent_at')]
        touches += [e['occurred_at'] for e in a['events'] if e['kind']=='linkedin_requested']
        if touches:
            parsed=[datetime.fromisoformat(t) for t in touches]
            last=max(t if t.tzinfo else t.replace(tzinfo=timezone.utc) for t in parsed)
            earliest=last.astimezone(ZoneInfo('Asia/Kolkata'))
            working_days=0
            while working_days<2:
                earliest += timedelta(days=1)
                if earliest.weekday()<5:working_days+=1
            send_at=max(datetime.fromisoformat(now()).replace(tzinfo=timezone.utc),earliest.astimezone(timezone.utc)).replace(tzinfo=None).isoformat()
        async with self.state._connect() as db:
            await db.execute('BEGIN IMMEDIATE')
            cur = await db.execute("SELECT 1 FROM partnership_email_bindings b JOIN outbox o ON o.id=b.outbox_id WHERE b.account_id=? AND o.status IN ('pending_review','approved','sending')",(aid,))
            if await cur.fetchone():
                raise ValueError('An email is already queued.')
            await db.execute("INSERT OR IGNORE INTO campaigns (id,name,channel,status,prospect_ids_json) VALUES (?,?,'email','active',?)", (cid,'Partnership: '+r['company'],json.dumps([a['prospect_id']])))
            oid = uuid.uuid4().hex[:12]
            await db.execute("INSERT INTO outbox (id,campaign_id,prospect_id,step,kind,to_email,subject,body,status,send_at) VALUES (?,?,?,?,'sequence',?,?,?,'pending_review',?)", (oid,cid,a['prospect_id'],step,prospect.email,r['email_subject'],r['email_body'],send_at))
            await db.execute('INSERT INTO partnership_email_bindings VALUES (?,?,?,?)',(oid,aid,asset_id,asset_hash))
            await db.commit()
        return {'outbox_id':oid,'status':'pending_review'}


async def send_verdict(state, item):
    """Enforce partnership restrictions immediately before native dispatch."""
    store = PartnershipStore(state)
    if not str(item.get('campaign_id','')).startswith(CAMPAIGN_PREFIX):
        # Cold sequences stay out of a partnership company. A reply to someone
        # who wrote to us is not cold, and goes through its own review.
        if item.get('kind', 'sequence') == 'sequence' and item.get('prospect_id'):
            tracked = await store.query("SELECT 1 FROM prospects p JOIN partnership_accounts a ON a.company_id=p.company_id WHERE p.id=? AND p.company_id!=''", (item['prospect_id'],))
            if tracked:
                return 'cancelled', HELD_REASON
        return None
    rows = await store.query('SELECT account_id FROM partnership_email_bindings WHERE outbox_id=?',(item['id'],))
    if not rows:
        return 'cancelled','partnership email has no account binding'
    a = await store.account(rows[0]['account_id'])
    if a['stopped']:
        return 'cancelled','partnership account replied or stopped'
    if not active(await store.setting('pilot',default_pilot())):
        return 'pending_review','partnership pilot is paused, not started or expired'
    if item.get('approved_by')!='you':
        return 'pending_review','partnership email requires your review'
    p = await state.get_prospect(a['prospect_id'])
    if not p or p.email_status!='verified':
        return 'pending_review','verify the direct contact email before sending'
    return None


async def email_attachment(state,item):
    """The exact approved asset bound when the email was queued."""
    if not str(item.get('campaign_id','')).startswith('partnership-'):
        return None
    store = PartnershipStore(state)
    rows = await store.query('SELECT b.asset_id,a.pdf,a.approved,a.id,a.content_hash,b.snapshot_hash FROM partnership_email_bindings b LEFT JOIN partnership_assets a ON a.id=b.asset_id WHERE b.outbox_id=?',(item['id'],))
    if not rows or not rows[0]['asset_id']:
        return None
    row = rows[0]
    if not row['approved'] or not row['pdf'] or row['content_hash']!=row['snapshot_hash']:
        raise ValueError('The attached partnership brief is missing or unapproved.')
    return {'filename':'partnership-brief.pdf','content_type':'application/pdf','data':row['pdf']}


def build_router(get_state):
    router = APIRouter(prefix='/api/partnerships')

    async def store():
        state = get_state()
        await state.init_db()
        return PartnershipStore(state, configured_kit_dir(), configured_output_dir())

    @router.get('')
    async def listing():
        s = await store()
        ids = await s.query('SELECT id FROM partnership_accounts ORDER BY external_id')
        accounts = [await s.account(r['id']) for r in ids]
        pilot = await s.setting('pilot',default_pilot())
        if pilot['status']=='active' and today()>date.fromisoformat(pilot['end_date']):
            pilot={**pilot,'status':'expired'}
        metrics={'accounts':len(accounts),'drafts':sum(bool(a['record'].get('email_body')) for a in accounts),'emails_sent':sum(sum(e['status']=='sent' for e in a['emails']) for a in accounts)}
        for label,event in [('requests','linkedin_requested'),('accepted','linkedin_accepted'),('replies','human_reply'),('positive_replies','positive_reply'),('calls','call_booked'),('briefs','brief_received')]:
            metrics[label]=sum(any(e['kind']==event for e in a['events']) for a in accounts)
        metrics['replies']=sum(a['contact_status']=='replied' or any(e['kind'] in ('human_reply','positive_reply') for e in a['events']) for a in accounts)
        return {'accounts':accounts,'pilot':pilot,'metrics':metrics,'brand':await s.setting('brand',{})}

    @router.post('/import/preview')
    async def preview(request:Request):
        try:
            records = parse_import(await request.json())
            return {'rows':await (await store()).preview_import(records),'total':len(records)}
        except (ValueError,TypeError) as exc:
            raise HTTPException(422,str(exc))

    @router.post('/import')
    async def importing(request:Request):
        try:
            payload=await request.json()
            records=parse_import(payload)
            if payload.get('confirm') is not True:
                raise ValueError('Preview the merge, then confirm the import.')
            s=await store()
            # Backup a consistent SQLite snapshot, including WAL, before a live import.
            from pathlib import Path
            if not str(s.state.db_path).startswith(':'):
                import asyncio, sqlite3
                path=Path(s.state.db_path)
                target=path.parent/'partnership-backups'/('before-import-'+uuid.uuid4().hex+'.db')
                def backup():
                    target.parent.mkdir(parents=True,exist_ok=True)
                    with sqlite3.connect(path) as src, sqlite3.connect(target) as dst:
                        src.backup(dst)
                    target.chmod(0o600)
                await asyncio.to_thread(backup)
            return await s.import_records(records)
        except (ValueError,TypeError) as exc:
            raise HTTPException(422,str(exc))

    @router.post('/pilot')
    async def pilot(request:Request):
        payload=await request.json()
        try:
            start=date.fromisoformat(payload['start_date']);end=date.fromisoformat(payload['end_date'])
            if not 27 <= (end-start).days <= 30 or payload.get('status') not in ('draft','active','paused'):
                raise ValueError('Choose a one-month window of 28–31 days and draft, active or paused status.')
            value={'start_date':start.isoformat(),'end_date':end.isoformat(),'status':payload['status'],'timezone':'Asia/Kolkata'}
            await (await store()).set_setting('pilot',value)
            return value
        except (KeyError,ValueError,TypeError) as exc:
            raise HTTPException(422,str(exc))

    @router.post('/brand')
    async def brand(request:Request):
        payload=await request.json()
        fields={'name','proof','offer','contact_name','email','website','calendar_url','logo_data_uri'}
        if not isinstance(payload,dict) or set(payload)-fields or any(not isinstance(v,str) or len(v)>700000 for v in payload.values()):
            raise HTTPException(422,'Invalid brand settings.')
        await (await store()).set_setting('brand',payload)
        return {'success':True}

    @router.post('/create')
    async def create(request:Request):
        payload=await request.json()
        if not isinstance(payload,dict) or any(not isinstance(v,str) for v in payload.values()):
            raise HTTPException(422,'Enter company details as text.')
        try:
            record={'id':'P'+uuid.uuid4().hex[:8],'company':payload.get('company',''),'website':payload.get('website',''),'city':payload.get('city',''),'region':payload.get('region',''),'stage':'Needs contact research','next_action':'Research the decision maker and relevant project before outreach.'}
            records=parse_import({'records':[record]})
            s=await store();await s.import_records(records)
            domain=normalized_domain(record['website'])
            found=await s.query('SELECT a.id FROM partnership_accounts a JOIN companies c ON c.id=a.company_id WHERE lower(c.domain) IN (?,?)',(domain,'www.'+domain))
            return await s.account(found[0]['id'])
        except (ValueError,TypeError) as exc:
            raise HTTPException(422,str(exc)) from exc

    @router.post('/promote')
    async def promote(request:Request):
        payload=await request.json()
        if not isinstance(payload,dict) or not isinstance(payload.get('company_id'),str) or not isinstance(payload.get('prospect_id',''),str):
            raise HTTPException(422,'Choose a company to move.')
        s=await store();fit=None
        try:
            from sellbird.config import load_config
            from sellbird.register import build_register
            row=next(iter(await build_register(s.query,load_config(),payload['company_id'])),None)
            if row:fit={'fit':row['fit'],'reasons':row['fit_reasons']}
        except Exception as exc:
            # A config that won't load must not stop the move; the fit line is only a note.
            logger.warning(f'Company fit unavailable for the move: {exc}')
        try:
            return await s.promote(payload['company_id'],payload.get('prospect_id',''),fit)
        except ValueError as exc:
            raise HTTPException(422,str(exc))

    @router.get('/export.csv')
    async def export():
        s=await store();ids=await s.query('SELECT id FROM partnership_accounts ORDER BY external_id')
        data=[]
        for r in ids:
            a=await s.account(r['id'])
            data.append({**a['record'],'harvey_id':a['id'],'company_id':a['company_id'],'prospect_id':a['prospect_id'],'email_verification':a['email_verification'],'stopped':a['stopped'],'recorded_events':json.dumps(a['events'])})
        fields=list(dict.fromkeys(k for r in data for k in r)) or ['id','company']
        out=io.StringIO();w=csv.DictWriter(out,fieldnames=fields);w.writeheader()
        for row in data:
            w.writerow({k:("'"+str(v) if str(v).startswith(('=','+','-','@')) else v) for k,v in row.items()})
        return Response(out.getvalue(),media_type='text/csv',headers={'Content-Disposition':'attachment; filename="partnerships.csv"'})

    @router.get('/{aid}')
    async def account(aid:str):
        s=await store()
        a=await s.account(aid)
        return {**a,'brief_plan':s.brief_plan(a['record'])}

    @router.post('/{aid}')
    async def update(aid:str,request:Request):
        try:
            return await (await store()).update(aid,await request.json())
        except (ValueError,TypeError) as exc:
            raise HTTPException(422,str(exc))

    @router.post('/{aid}/events')
    async def event(aid:str,request:Request):
        try:
            return await (await store()).event(aid,await request.json())
        except (ValueError,TypeError) as exc:
            raise HTTPException(422,str(exc))

    @router.post('/{aid}/brief')
    async def brief(aid:str):
        try:
            s=await store()
            asset_id=await s.create_asset(aid)
            try:
                files,problem=await s.save_files(aid,asset_id),''
            except (ValueError,OSError) as exc:
                files,problem=None,f'The brief was generated, but its files could not be saved: {exc}'
            return {'asset_id':asset_id,'files':files,'files_error':problem}
        except ValueError as exc:
            raise HTTPException(422,str(exc))
        except Exception as exc:
            from playwright.async_api import Error as PlaywrightError
            if isinstance(exc,PlaywrightError):
                raise HTTPException(502,'Chromium could not render the brief. Check the local browser installation, then try again.') from exc
            raise

    @router.post('/{aid}/draft')
    async def draft(aid:str):
        from sellbird.brain import Brain
        from sellbird.config import load_config
        from sellbird.untrusted import fence, UNTRUSTED_RULE
        from sellbird.humanize import humanize
        s=await store();a=await s.account(aid);brand=await s.setting('brand',{})
        r=a['record']
        if not r.get('pdf_observation') or not r.get('partnership_angle') or not brand.get('name'):
            raise HTTPException(422,'Save a sourced observation, partnership angle and brand settings first.')
        config=load_config();brain=Brain(s.state,config.brain)
        prompt=('Write one plain-text agency partnership introduction, 80–125 words. Use a specific observation, a concrete component offer, relevant proof, and one short-call question. The agency retains the client and pricing. Use a personal greeting and signature. Do not invent projects, capacity, numbers, clients or skills gaps. Avoid hype, flattery, em dashes, attachments claims and unprovided links. Return ONLY JSON with subject and body. '+UNTRUSTED_RULE+'\n'+fence(json.dumps({'recipient':r.get('target_person'),'company':r['company'],'observation':r['pdf_observation'],'component':r['partnership_angle'],'brand':brand}), 'reviewed partnership facts'))
        result=await brain.think_json(prompt,session_id='sellbird-partnership-writer',agent='writer',task='personal_email')
        if not isinstance(result,dict) or not isinstance(result.get('subject'),str) or not isinstance(result.get('body'),str):
            raise HTTPException(502,'The configured writer could not produce a draft. You can edit it manually.')
        refined=(await humanize(brain,[{'subject':result['subject'][:120],'body':result['body'][:4000]}],agent='writer',enabled=True))[0]
        return await s.update(aid,{'email_subject':refined['subject'],'email_body':refined['body']})

    @router.post('/{aid}/video-script')
    async def video_script(aid:str):
        s=await store();a=await s.account(aid);r=a['record'];brand=await s.setting('brand',{})
        if not r.get('pdf_observation') or not r.get('partnership_angle'):
            raise HTTPException(422,'Save a sourced observation and proposed component first.')
        script=(f"0–15s: Open {r['website']}. Introduce yourself as {brand.get('contact_name','the delivery lead')} from {brand.get('name','our team')}.\n\n15–35s: Show the relevant public service or project. {r['pdf_observation']}\n\n35–65s: Open the proposed brief. Explain: {r['partnership_angle']}\n\n65–90s: Explain that their agency owns the client relationship and pricing. Suggest a short conversation about one defined engagement.\n\nRecord the walkthrough personally, then paste the actual Loom recording URL into this account.")
        return await s.update(aid,{'video_script':script})

    @router.post('/{aid}/verify-email')
    async def verify_email(aid:str):
        from sellbird.config import load_env
        from sellbird.integrations.email_finder import verify_published_address
        s=await store();a=await s.account(aid)
        if not a['prospect_id'] or not a['record'].get('direct_email'):
            raise HTTPException(422,'Save a named contact and direct business email first.')
        person=await s.state.get_prospect(a['prospect_id'])
        email=person.email
        status=await verify_published_address(email,load_env())
        current=await s.state.get_prospect(a['prospect_id'])
        if current.email!=email:
            raise HTTPException(409,'The contact address changed during verification. Check the current address again.')
        await s.state.update_prospect_email(person.id,email,status)
        return await s.account(aid)

    @router.get('/{aid}/assets/{asset_id}/{fmt}')
    async def asset(aid:str,asset_id:str,fmt:str):
        s=await store()
        rows=await s.query('SELECT * FROM partnership_assets WHERE id=? AND account_id=?',(asset_id,aid))
        if not rows or fmt not in ('html','pdf'):
            raise HTTPException(404,'Asset not found.')
        if fmt=='html':
            try:
                kit=s.kit()
            except ValueError:
                kit=None
            return HTMLResponse(preview_markup(rows[0]['html'],kit))
        return Response(rows[0]['pdf'],media_type='application/pdf',headers={'Content-Disposition':'attachment; filename="partnership-brief.pdf"'})

    @router.post('/{aid}/assets/{asset_id}/approve')
    async def approve(aid:str,asset_id:str):
        s=await store();a=await s.account(aid)
        try:
            current=brief_hash(await s.brief_markup(a['record']))
        except ValueError as exc:
            raise HTTPException(422,str(exc)) from exc
        async with s.state._connect() as db:
            cur=await db.execute('UPDATE partnership_assets SET approved=1 WHERE id=? AND account_id=? AND content_hash=?',(asset_id,aid,current));await db.commit()
            if not cur.rowcount:
                raise HTTPException(409,'Generate and review the current brief first.')
        return {'success':True}

    @router.post('/{aid}/queue-email')
    async def queue(aid:str,request:Request):
        try:
            payload=await request.json()
            return await (await store()).stage_email(aid,payload.get('asset_id',''))
        except (ValueError,TypeError) as exc:
            raise HTTPException(422,str(exc))

    return router
