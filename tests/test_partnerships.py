"""Partnership imports, identity, channel policy and immutable email assets."""
import asyncio
import sqlite3
from datetime import timedelta

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

from sellbird import partnerships as p
from sellbird.models.company import Company
from sellbird.models.prospect import Prospect
from sellbird.partnership_assets import render_brief
from sellbird.state import StateManager


def row(**changes):
    return {'id':'A01','company':'Example Studio','website':'https://studio.example',
            'region':'India','city':'Example City','target_person':'Alex Example',
            'target_role':'Founder','target_profile':'https://www.linkedin.com/in/example-profile/',
            'direct_email':'alex@studio.example','partnership_angle':'Payment API integration',
            'research_sources':'https://studio.example/work',
            'pdf_observation':'Your site describes commerce and ERP integrations.',
            'pdf_proposal':'An agreed payment reconciliation service.',
            'pdf_pilot':'Scope one integration and agree acceptance criteria.',
            'email_subject':'integration work with Example Studio','email_body':'Hi Alex,\nCould we discuss one defined API component?',**changes}


@pytest_asyncio.fixture
async def store(tmp_path):
    state=StateManager(str(tmp_path/'test.db'));await state.init_db()
    return p.PartnershipStore(state)


async def account(store,**changes):
    await store.import_records([row(**changes)])
    return (await store.query('SELECT id FROM partnership_accounts'))[0]['id']


async def activate(store):
    await store.set_setting('pilot',{**p.default_pilot(),'status':'active'})


@pytest.mark.asyncio
async def test_import_is_idempotent_and_preserves_operator_edits(store):
    aid=await account(store)
    await store.update(aid,{'next_action':'Discuss the brief','research_notes':'Reviewed personally'})
    result=await store.import_records([row(next_action='old source value')])
    assert result['created']==0 and result['preserved']==1
    a=await store.account(aid)
    assert a['record']['next_action']=='Discuss the brief'
    assert (await store.query('SELECT COUNT(*) n FROM companies'))[0]['n']==1
    assert (await store.query('SELECT COUNT(*) n FROM prospects'))[0]['n']==1
    contact=await store.state.get_prospect(a['prospect_id'])
    assert contact.status=='partnership' and contact.email_status=='guess' and not contact.email_verified


@pytest.mark.asyncio
async def test_domain_matches_existing_company_and_preserves_opt_out(store):
    cid=await store.state.add_company(Company(name='Example Studio',domain='studio.example'))
    pid=await store.state.add_prospect(Prospect(company_id=cid,first_name='Alex',last_name='Example',title='Founder',email='alex@studio.example',status='opted_out',email_status='verified'))
    aid=await account(store)
    a=await store.account(aid)
    assert a['company_id']==cid and a['prospect_id']==pid and a['stopped']
    await store.event(aid,{'kind':'human_reply','idempotency_key':'reply-1'})
    assert (await store.state.get_prospect(pid)).status=='opted_out'


@pytest.mark.asyncio
async def test_conflicting_import_identity_rolls_back_whole_batch(store):
    await account(store)
    before=(await store.query('SELECT COUNT(*) n FROM companies'))[0]['n']
    with pytest.raises(ValueError):
        await store.import_records([row(id='A02',company='Other Studio',website='https://other.example',target_profile='',direct_email='alex@studio.example')])
    assert (await store.query('SELECT COUNT(*) n FROM companies'))[0]['n']==before


def test_parser_rejects_duplicate_domains_and_invalid_urls():
    with pytest.raises(ValueError):p.parse_import({'records':[row(),row(id='A02')]})
    with pytest.raises(ValueError):p.parse_import({'records':[row(website='javascript:alert(1)')]})
    assert len(p.parse_import({'csv':'id,company,website\nA01,"Studio, Inc",https://studio.example\n'}))==1


@pytest.mark.asyncio
async def test_requests_require_active_pilot_and_do_not_imply_acceptance(store):
    aid=await account(store)
    with pytest.raises(ValueError):await store.event(aid,{'kind':'linkedin_requested','idempotency_key':'req'})
    await activate(store)
    await store.event(aid,{'kind':'linkedin_requested','idempotency_key':'req'})
    await store.event(aid,{'kind':'linkedin_requested','idempotency_key':'req'})
    events=(await store.account(aid))['events']
    assert len(events)==1 and events[0]['kind']=='linkedin_requested'
    with pytest.raises(ValueError):await store.event(aid,{'kind':'linkedin_message','idempotency_key':'dm'})


@pytest.mark.asyncio
async def test_expired_pilot_blocks_queue_and_native_dispatch(store):
    aid=await account(store);await activate(store)
    item=await store.stage_email(aid)
    expired={**p.default_pilot(),'status':'active','start_date':(p.today()-timedelta(days=32)).isoformat(),'end_date':(p.today()-timedelta(days=1)).isoformat()}
    await store.set_setting('pilot',expired)
    with pytest.raises(ValueError):await store.stage_email(aid)
    o=await store.state.get_outbox_item(item['outbox_id'])
    assert (await p.send_verdict(store.state,o))[0]=='pending_review'


@pytest.mark.asyncio
async def test_draft_not_sendable_until_direct_email_verified_and_human_approved(store):
    aid=await account(store);await activate(store)
    item=await store.stage_email(aid);o=await store.state.get_outbox_item(item['outbox_id'])
    assert o['status']=='pending_review'
    assert (await p.send_verdict(store.state,o))[0]=='pending_review'
    o['approved_by']='you'
    assert 'verify' in (await p.send_verdict(store.state,o))[1]
    a=await store.account(aid)
    await store.state.update_prospect_email(a['prospect_id'],'alex@studio.example','verified')
    assert await p.send_verdict(store.state,o) is None
    await store.update(aid,{'direct_email':'new@studio.example'})
    assert (await store.state.get_prospect(a['prospect_id'])).email_status=='guess'


@pytest.mark.asyncio
async def test_reply_cancels_pending_outbox_and_stops_queue(store):
    aid=await account(store);await activate(store);item=await store.stage_email(aid)
    with pytest.raises(ValueError):await store.stage_email(aid)
    await store.event(aid,{'kind':'human_reply','idempotency_key':'reply'})
    assert (await store.state.get_outbox_item(item['outbox_id']))['status']=='cancelled'
    with pytest.raises(ValueError):await store.stage_email(aid)


@pytest.mark.asyncio
async def test_unknown_person_can_be_completed_from_dashboard(store):
    aid=await account(store,target_person='',direct_email='',target_profile='')
    assert not (await store.account(aid))['prospect_id']
    a=await store.update(aid,{'target_person':'Jordan Example','target_role':'CTO','direct_email':'jordan@studio.example'})
    assert a['prospect_id'] and a['email_verification']=='guess'


@pytest.mark.asyncio
async def test_pdf_binding_keeps_exact_reviewed_version(store,monkeypatch):
    async def fake_pdf(markup):return b'%PDF-1.7 fictional test bytes'
    monkeypatch.setattr(p,'to_pdf',fake_pdf)
    aid=await account(store);await activate(store)
    await store.set_setting('brand',{'name':'Example Engineering','proof':'Prior software delivery experience.'})
    asset=await store.create_asset(aid)
    with pytest.raises(ValueError):await store.stage_email(aid,asset)
    async with store.state._connect() as db:
        await db.execute('UPDATE partnership_assets SET approved=1 WHERE id=?',(asset,));await db.commit()
    item=await store.stage_email(aid,asset)
    await store.update(aid,{'pdf_proposal':'A different component to discuss.'})
    attachment=await p.email_attachment(store.state,await store.state.get_outbox_item(item['outbox_id']))
    assert attachment['data']==b'%PDF-1.7 fictional test bytes'
    new=await store.create_asset(aid)
    assert new!=asset
    async with store.state._connect() as db:
        await db.execute('DELETE FROM partnership_assets WHERE id=?',(asset,));await db.commit()
    with pytest.raises(ValueError):
        await p.email_attachment(store.state,await store.state.get_outbox_item(item['outbox_id']))


@pytest.mark.asyncio
async def test_rejected_first_draft_can_be_revised_without_becoming_a_followup(store):
    aid=await account(store);await activate(store)
    first=await store.stage_email(aid)
    await store.state.update_outbox_item(first['outbox_id'],status='cancelled')
    second=await store.stage_email(aid)
    a=await store.state.get_outbox_item(first['outbox_id'])
    b=await store.state.get_outbox_item(second['outbox_id'])
    assert b['step']==1 and a['campaign_id']!=b['campaign_id']


@pytest.mark.asyncio
async def test_request_and_email_are_spaced_in_working_days(store):
    from datetime import datetime
    aid=await account(store);await activate(store)
    await store.event(aid,{'kind':'linkedin_requested','idempotency_key':'request'})
    item=await store.stage_email(aid)
    email=await store.state.get_outbox_item(item['outbox_id'])
    assert datetime.fromisoformat(email['send_at'])>datetime.fromisoformat(p.now())+timedelta(days=1)


@pytest.mark.asyncio
async def test_www_company_identity_is_not_duplicated(store):
    cid=await store.state.add_company(Company(name='Example Studio',domain='www.studio.example'))
    aid=await account(store)
    assert (await store.account(aid))['company_id']==cid


def test_api_verification_does_not_claim_an_inconclusive_check_succeeded(client,monkeypatch):
    from sellbird.integrations import email_finder
    async def unknown(email,env):return 'guess'
    monkeypatch.setattr(email_finder,'verify_published_address',unknown)
    client.post('/api/partnerships/import',json={'records':[row()],'confirm':True})
    aid=client.get('/api/partnerships').json()['accounts'][0]['id']
    result=client.post('/api/partnerships/'+aid+'/verify-email',json={})
    assert result.status_code==200 and result.json()['email_verification']=='guess'


def test_brief_escapes_content_and_requires_evidence():
    markup=render_brief(row(company='<script>alert(1)</script>'),{'name':'Example'})
    assert '<script>' not in markup and '&lt;script&gt;' in markup
    assert markup.count('<section>')==2
    with pytest.raises(ValueError):render_brief(row(research_sources=''),{})


@pytest.fixture
def client(tmp_path,monkeypatch):
    import sellbird.dashboard as dash
    db=tmp_path/'api.db';asyncio.run(StateManager(str(db)).init_db())
    monkeypatch.setattr(dash,'DB_PATH',db)
    dash.token_store.configure(tmp_path/'token')
    with TestClient(dash.app,base_url='http://127.0.0.1:5555',headers={'Authorization':'Bearer '+dash.token_store.get()}) as c:
        yield c


def test_api_preview_confirm_import_export_and_auth(client):
    assert client.get('/api/partnerships',headers={'Authorization':''}).status_code==401
    preview=client.post('/api/partnerships/import/preview',json={'records':[row()]}).json()
    assert preview['total']==1
    assert client.get('/api/partnerships').json()['metrics']['accounts']==0
    assert client.post('/api/partnerships/import',json={'records':[row()]}).status_code==422
    assert client.post('/api/partnerships/import',json={'records':[row()],'confirm':True}).json()['created']==1
    data=client.get('/api/partnerships').json()
    assert data['metrics']['requests']==0 and data['metrics']['accepted']==0
    assert data['pilot']['status']=='draft'
    assert client.get('/api/partnerships/export.csv').status_code==200
    assert client.post('/api/partnerships/pilot',json={'start_date':'2026-01-01','end_date':'2027-01-01','status':'active'}).status_code==422


def test_shell_has_partnerships_assets(client):
    shell=client.get('/').text
    assert 'partnerships-content' in shell and '/static/partnerships.js' in shell
    assert client.get('/static/partnerships.js').status_code==200


def test_accounts_can_be_created_and_deduplicated_in_dashboard(client):
    body={'company':'New Example Studio','website':'https://new.example','region':'India'}
    first=client.post('/api/partnerships/create',json=body)
    second=client.post('/api/partnerships/create',json=body)
    assert first.status_code==second.status_code==200
    assert first.json()['id']==second.json()['id']
    assert client.get('/api/partnerships').json()['metrics']['accounts']==1


def test_writer_unavailable_is_not_a_dashboard_authentication_error(client,monkeypatch):
    from types import SimpleNamespace
    from sellbird import brain,config
    class UnavailableBrain:
        def __init__(self,*args):pass
        async def think_json(self,*args,**kwargs):return None
    monkeypatch.setattr(brain,'Brain',UnavailableBrain)
    monkeypatch.setattr(config,'load_config',lambda:SimpleNamespace(brain=None))
    client.post('/api/partnerships/import',json={'records':[row()],'confirm':True})
    client.post('/api/partnerships/brand',json={'name':'Example Engineering'})
    aid=client.get('/api/partnerships').json()['accounts'][0]['id']
    assert client.post('/api/partnerships/'+aid+'/draft',json={}).status_code==502


@pytest.mark.asyncio
async def test_paused_account_does_not_queue_even_during_active_pilot(store):
    aid=await account(store);await activate(store)
    await store.update(aid,{'stage':'Paused'})
    with pytest.raises(ValueError):await store.stage_email(aid)
