// Partnership accounts share the dashboard's authenticated API and Outbox.
let partnerData = null, partnerAccount = null, partnerImport = null;

function partnerApi(path, options) {
  const opts = options ? {...options, headers: {...(options.headers || {}), 'Content-Type':'application/json'}} : undefined;
  return api(path, opts);
}

async function loadPartnerships(force = false) {
  if (partnerAccount && !force) return;
  partnerAccount = null;
  const el = document.getElementById('partnerships-content');
  const data = await partnerApi('/api/partnerships');
  if (!data) { el.innerHTML = offlineState(); return; }
  partnerData = data;
  const m = data.metrics, p = data.pilot;
  el.innerHTML = '<div class="partner-summary">' +
    [['Accounts',m.accounts],['Email drafts',m.drafts],['Emails sent',m.emails_sent],['Requests recorded',m.requests],['Accepted',m.accepted],['Positive replies',m.positive_replies]].map(([k,v])=>'<div><b>'+v+'</b><span>'+k+'</span></div>').join('') + '</div>' +
    '<div class="partner-panel"><h3>One-month partnership pilot</h3><p>Email and connection requests only. LinkedIn requests are completed on LinkedIn and recorded here. No automatic DMs.</p><div class="partner-toolbar">' +
    '<label>Start <input class="form-input" id="partner-start" type="date" value="'+escHtml(p.start_date)+'"></label>' +
    '<label>End <input class="form-input" id="partner-end" type="date" value="'+escHtml(p.end_date)+'"></label>' +
    '<label>Status <select class="form-input" id="partner-pilot-status">'+['draft','active','paused'].map(s=>'<option '+(s===p.status?'selected':'')+'>'+s+'</option>').join('')+'</select></label>' +
    '<button class="btn btn-secondary" onclick="savePartnerPilot()">Save pilot</button><span>'+escHtml(p.status)+' · Asia/Kolkata</span></div></div>' +
    '<details class="partner-panel"><summary>Brand and proof for briefs</summary>'+partnerBrandForm(data.brand)+'</details>' +
    '<details class="partner-panel"><summary>Add an agency</summary><div class="partner-form"><label>Company name<input class="form-input" id="partner-new-company"></label><label>Company website<input class="form-input" id="partner-new-website" placeholder="https://"></label><label>City / delivery location<input class="form-input" id="partner-new-city"></label><label>Region<input class="form-input" id="partner-new-region" placeholder="India"></label></div><button class="btn btn-secondary" onclick="createPartner()">Create account</button></details>' +
    '<details class="partner-panel"><summary>Import agency research</summary><p>Preview a CSV or JSON account list before importing. Existing account edits and history are preserved. Import does not enroll contacts or send messages.</p><input type="file" accept=".csv,.json" onchange="previewPartnerImport(this)"><div id="partner-import-preview"></div></details>' +
    '<div class="partner-toolbar"><input id="partner-search" class="form-input" placeholder="Search company, person or research" oninput="renderPartnerRows()"><select id="partner-region" class="form-input" onchange="renderPartnerRows()"><option value="">All regions</option><option>Mohali</option><option>India</option><option>International</option></select><select id="partner-filter" class="form-input" onchange="renderPartnerRows()"><option value="">All accounts</option><option value="draft">Email draft</option><option value="missing">Missing direct email</option><option value="history">Earlier LinkedIn activity</option><option value="requested">Request recorded</option><option value="accepted">Accepted</option><option value="replied">Replied / stopped</option><option value="due">Follow-up due</option></select><button class="btn btn-secondary" onclick="downloadCsv(\'/api/partnerships/export.csv\',\'partnerships.csv\',this)">Export CSV</button></div><div id="partner-rows"></div>';
  renderPartnerRows();
}

function partnerBrandForm(b) {
  return '<div class="partner-form">'+[['name','Company name'],['contact_name','Sender name'],['email','Contact email'],['website','Website'],['calendar_url','Call link']].map(([k,label])=>'<label>'+label+'<input class="form-input" id="partner-brand-'+k+'" value="'+escHtml(b[k]||'')+'"></label>').join('') +
    '<label class="partner-wide">Relevant team background<textarea class="form-input" id="partner-brand-proof">'+escHtml(b.proof||'')+'</textarea></label><label class="partner-wide">Partnership offer<textarea class="form-input" id="partner-brand-offer">'+escHtml(b.offer||'')+'</textarea></label></div><label>Logo (PNG) <input type="file" accept="image/png" id="partner-brand-logo"></label><button class="btn btn-secondary" onclick="savePartnerBrand()">Save brand</button>';
}

async function createPartner(){
  const payload={};['company','website','city','region'].forEach(k=>payload[k]=document.getElementById('partner-new-'+k).value);
  const result=await partnerApi('/api/partnerships/create',{method:'POST',body:JSON.stringify(payload)});
  if(result?.id){showToast('Account ready. Complete its research and contact details.');openPartner(result.id);}else showToast(failMsg(result,'Could not create account.'));
}

async function savePartnerBrand() {
  const b={...partnerData.brand};
  ['name','contact_name','email','website','calendar_url','proof','offer'].forEach(k=>b[k]=document.getElementById('partner-brand-'+k).value);
  const file=document.getElementById('partner-brand-logo').files[0];
  if (file) {
    if(file.size>500000){showToast('Choose a PNG smaller than 500 KB.');return;}
    b.logo_data_uri=await new Promise((resolve,reject)=>{const r=new FileReader();r.onload=()=>resolve(r.result);r.onerror=reject;r.readAsDataURL(file);});
  }
  const result=await partnerApi('/api/partnerships/brand',{method:'POST',body:JSON.stringify(b)});
  if(result?.success){partnerData.brand=b;showToast('Brand saved. Generate a new brief when its content changes.');}else showToast(failMsg(result,'Could not save brand.'));
}

async function savePartnerPilot(){
  const result=await partnerApi('/api/partnerships/pilot',{method:'POST',body:JSON.stringify({start_date:document.getElementById('partner-start').value,end_date:document.getElementById('partner-end').value,status:document.getElementById('partner-pilot-status').value})});
  if(result?.status){showToast('Pilot saved. This does not approve emails or send invitations.');loadPartnerships(true);}else showToast(failMsg(result,'Could not save pilot.'));
}

function renderPartnerRows(){
  const search=document.getElementById('partner-search').value.toLowerCase();
  const region=document.getElementById('partner-region').value;
  const filter=document.getElementById('partner-filter').value;
  const day=new Intl.DateTimeFormat('en-CA',{timeZone:'Asia/Kolkata',year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date());
  const rows=partnerData.accounts.filter(a=>{
    const r=a.record, has=k=>a.events.some(e=>e.kind===k);
    if(search&&!JSON.stringify(r).toLowerCase().includes(search))return false;
    if(region==='Mohali'&&!String(r.city).includes('Mohali'))return false;
    if(region==='India'&&!String(r.region).includes('India'))return false;
    if(region==='International'&&String(r.region).includes('India'))return false;
    return !filter || (filter==='draft'&&r.email_body) || (filter==='missing'&&!r.direct_email) || (filter==='history'&&has('legacy_linkedin')) || (filter==='requested'&&has('linkedin_requested')) || (filter==='accepted'&&has('linkedin_accepted')) || (filter==='replied'&&a.stopped) || (filter==='due'&&r.followup_date&&r.followup_date<=day&&!a.stopped);
  });
  document.getElementById('partner-rows').innerHTML='<p class="muted">'+rows.length+' accounts. Public email publication and deliverability verification are separate.</p><div class="table-card"><table><thead><tr><th>Company</th><th>Decision maker</th><th>Contact</th><th>Stage / next action</th><th></th></tr></thead><tbody>'+rows.map(a=>{
    const r=a.record;
    return '<tr><td><b>'+escHtml(r.company)+'</b><br><small>'+escHtml(r.city||r.region)+' · '+escHtml(r.id)+'</small></td><td>'+escHtml(r.target_person||'To identify')+'<br><small>'+escHtml(r.target_role)+'</small></td><td>'+escHtml(r.direct_email||'Direct email pending')+'<br><small>'+escHtml(a.email_verification||'Not verified')+'</small></td><td>'+escHtml(a.stopped?'Replied / stopped':r.stage)+'<br><small>'+escHtml(r.next_action)+'</small></td><td><button class="btn btn-secondary btn-sm" onclick="openPartner('+jsq(a.id)+')">Open account</button></td></tr>';
  }).join('')+'</tbody></table></div>';
}

async function previewPartnerImport(input){
  const file=input.files[0];if(!file)return;
  if(file.size>4000000){showToast('Choose a file smaller than 4 MB.');return;}
  try{
    const text=await file.text();partnerImport=file.name.endsWith('.json')?JSON.parse(text):{csv:text};
    if(Array.isArray(partnerImport))partnerImport={records:partnerImport};
    const result=await partnerApi('/api/partnerships/import/preview',{method:'POST',body:JSON.stringify(partnerImport)});
    if(!result?.rows){showToast(failMsg(result,'Could not preview import.'));return;}
    document.getElementById('partner-import-preview').innerHTML='<p>'+result.total+' records.</p><ul>'+result.rows.map(r=>'<li>'+escHtml(r.company)+': '+escHtml(r.action)+'</li>').join('')+'</ul><button class="btn btn-primary" onclick="applyPartnerImport(this)">Import these records</button>';
  }catch{showToast('Could not read the import file.');}
}
async function applyPartnerImport(button){
  button.disabled=true;
  const result=await partnerApi('/api/partnerships/import',{method:'POST',body:JSON.stringify({...partnerImport,confirm:true})});
  if(result?.created!==undefined){showToast(result.created+' added; '+result.preserved+' existing accounts preserved.');partnerImport=null;loadPartnerships(true);}else{button.disabled=false;showToast(failMsg(result,'Import failed.'));}
}

function partnerField(key,label,value,wide=false,textarea=false){
  return '<label class="'+(wide?'partner-wide':'')+'">'+label+(textarea?'<textarea':'<input')+' class="form-input" data-partner-field="'+key+'" '+(textarea?'rows="4">'+escHtml(value||'')+'</textarea>':'value="'+escHtml(value||'')+'">')+'</label>';
}

async function openPartner(id){
  const a=await partnerApi('/api/partnerships/'+encodeURIComponent(id));if(!a){showToast('Could not load this account.');return;}
  partnerAccount=a;const r=a.record;
  const profile=safeHref(r.target_profile), site=safeHref(r.website), loom=safeHref(r.loom_url);
  const f=(k,l,w=false,t=false)=>partnerField(k,l,r[k],w,t);
  const plan=a.brief_plan, how={chosen:'you chose this type',suggested:'suggested from your research notes and observation',default:'nothing in your notes matched, so the default'};
  const typeField=plan?'<label class="partner-wide">Company type (picks the proof pages)<select class="form-input" data-partner-field="company_type"><option value="">Suggest from my notes</option>'+plan.segments.map(x=>'<option value="'+escHtml(x.id)+'"'+(r.company_type===x.id?' selected':'')+'>'+escHtml(x.label)+'</option>').join('')+'</select><span class="muted">'+(plan.error?escHtml(plan.error):'Proof pages for: <b>'+escHtml(plan.label)+'</b> ('+how[plan.how]+').'+(plan.close?' <b>Close call</b> with '+escHtml(plan.close)+': choose one if you know which fits.':'')+' Save to refresh.')+'</span></label>':'';
  document.getElementById('partnerships-content').innerHTML='<button class="btn btn-ghost" onclick="loadPartnerships(true)">← All partnerships</button><h2>'+escHtml(r.company)+'</h2><p>'+escHtml(r.city||r.region)+' · '+escHtml(r.id)+(site?' · <a target="_blank" rel="noopener" href="'+site+'">Company website ↗</a>':'')+'</p>'+
    (a.stopped?'<div class="partner-panel">This account has replied or stopped. No further cold outreach can be queued.</div>':'')+
    '<div class="partner-form">'+f('company','Company')+f('city','City / delivery location')+f('region','Region')+f('size_claimed','Published size / stage')+f('target_person','Decision maker')+f('target_role','Role')+f('target_profile','LinkedIn profile')+f('direct_email','Direct business email')+f('business_inbox','Company inbox (separate)')+f('contact_route','Contact evidence URL')+f('research_date','Research date (YYYY-MM-DD)')+f('stage','Working stage')+f('next_action','Next action',true)+f('followup_date','Follow-up date (YYYY-MM-DD)')+f('loom_url','Actual Loom/video URL')+f('research_notes','Research notes',true,true)+f('research_sources','Research source URLs (separate with |)',true,true)+f('partnership_angle','Relevant component / partnership angle',true,true)+'</div>'+
    '<p class="muted">Linked contact email verification: '+escHtml(a.email_verification||'not verified')+'. Updating the address resets verification. General inboxes are not used as a named contact.</p><div class="partner-toolbar"><button class="btn btn-primary" onclick="savePartner()">Save account</button><button class="btn btn-secondary" onclick="verifyPartnerEmail(this)">Check with configured email verifier</button><small>May use your verifier credits. Inconclusive checks stay unverified.</small></div>'+
    '<div class="partner-panel"><h3>LinkedIn connection</h3><p>Open the researched profile, complete the invitation on LinkedIn, then record what happened. Acceptance is recorded separately.</p><div class="partner-toolbar">'+(profile?'<a class="btn btn-secondary" target="_blank" rel="noopener noreferrer" href="'+profile+'">Open profile ↗</a>':'<span>Add the person-specific profile first.</span>')+[['linkedin_requested','Record request sent'],['linkedin_accepted','Record acceptance'],['linkedin_declined','Record decline']].map(([k,l])=>'<button class="btn btn-secondary" onclick="recordPartnerEvent('+jsq(k)+')">'+l+'</button>').join('')+'</div></div>'+
    '<div class="partner-panel"><h3>Personalized brief</h3><div class="partner-form">'+typeField+f('pdf_observation','What you observed (sourced)',true,true)+f('pdf_proposal','Component we could own',true,true)+f('pdf_pilot','Proposed first engagement',true,true)+'</div><button class="btn btn-secondary" onclick="generatePartnerBrief(this)">Save and generate PDF</button><div class="partner-assets">'+a.assets.map(x=>'<div>Version '+x.version+' · '+(x.approved?'Reviewed':'Awaiting review')+' <button class="btn btn-ghost btn-sm" onclick="partnerAsset('+jsq(a.id)+','+jsq(x.id)+',\'html\')">Preview</button><button class="btn btn-ghost btn-sm" onclick="partnerAsset('+jsq(a.id)+','+jsq(x.id)+',\'pdf\')">Download PDF</button>'+(!x.approved?'<button class="btn btn-secondary btn-sm" onclick="approvePartnerAsset('+jsq(x.id)+')">Approve reviewed version</button>':'')+'</div>').join('')+'</div></div>'+
    '<div class="partner-panel"><h3>Personal email</h3><div class="partner-form">'+f('email_subject','Subject',true)+f('email_body','Body',true,true)+'</div>'+partnerReview(r)+'<div class="partner-toolbar"><button class="btn btn-secondary" onclick="generatePartnerEmail(this)">Save and draft with Harvey + Humanizer + review</button><select class="form-input" id="partner-attachment"><option value="">Email only</option>'+a.assets.filter(x=>x.approved).map(x=>'<option value="'+escHtml(x.id)+'">Attach reviewed PDF v'+x.version+'</option>').join('')+'</select><button class="btn btn-primary" onclick="queuePartnerEmail(this)">Save and queue in Outbox</button></div><p class="muted">Queued emails wait for your Outbox approval and direct-email verification. No email is sent by these buttons.</p></div>'+
    '<div class="partner-panel"><h3>Optional personalized video</h3><button class="btn btn-secondary" onclick="generatePartnerVideo()">Save and prepare recording script</button><div class="partner-form">'+f('video_script','60–90 second recording plan',true,true)+'</div>'+(loom?'<a href="'+loom+'" target="_blank" rel="noopener">Open saved recording ↗</a>':'<p class="muted">Record the walkthrough, then save its actual link above.</p>')+'</div>'+
    '<div class="partner-panel"><h3>Conversation and next steps</h3><div class="partner-toolbar">'+[['human_reply','Record reply'],['positive_reply','Positive partnership reply'],['call_booked','Call booked'],['brief_received','Brief received'],['do_not_contact','Do not contact']].map(([k,l])=>'<button class="btn btn-secondary" onclick="recordPartnerEvent('+jsq(k)+')">'+l+'</button>').join('')+'</div><h4>Email history</h4>'+a.emails.map(e=>'<p>'+escHtml(e.status)+' · '+escHtml(e.subject)+' · '+escHtml(e.sent_at||'not sent')+(e.asset_id?' · PDF attached':'')+(e.error?' · '+escHtml(e.error):'')+'</p>').join('')+'<h4>Recorded activity</h4>'+a.events.map(e=>'<p>'+escHtml(e.occurred_at)+' · '+escHtml(e.kind)+'<br><small>'+escHtml(e.detail)+'</small></p>').join('')+'</div>';
}

async function savePartner(refresh=true){
  const changes={};document.querySelectorAll('[data-partner-field]').forEach(el=>changes[el.dataset.partnerField]=el.value);
  const result=await partnerApi('/api/partnerships/'+partnerAccount.id,{method:'POST',body:JSON.stringify(changes)});
  if(!result?.id){showToast(failMsg(result,'Could not save account.'));return false;}
  partnerAccount=result;if(refresh){showToast('Account saved.');await openPartner(result.id);}return true;
}
async function recordPartnerEvent(kind){
  if(!await savePartner(false))return;
  const result=await partnerApi('/api/partnerships/'+partnerAccount.id+'/events',{method:'POST',body:JSON.stringify({kind,idempotency_key:crypto.randomUUID(),detail:'Recorded by the operator in the dashboard.'})});
  if(result?.id){showToast('Activity recorded.');openPartner(result.id);}else showToast(failMsg(result,'Could not record activity.'));
}
async function generatePartnerBrief(button){
  button.disabled=true;button.textContent='Generating…';
  if(!await savePartner(false)){button.disabled=false;button.textContent='Generate brief';return;}
  const result=await partnerApi('/api/partnerships/'+partnerAccount.id+'/brief',{method:'POST',body:'{}'});
  if(result?.asset_id){showToast(result.files?.folder?'Brief generated and saved to '+result.files.folder+'. Preview it before approval.':(result.files_error||'Brief generated. Preview it before approval.'));openPartner(partnerAccount.id);}else{showToast(failMsg(result,'PDF generation failed.'));button.disabled=false;button.textContent='Generate brief';}
}
async function approvePartnerAsset(id){
  if(!await savePartner(false))return;
  const result=await partnerApi('/api/partnerships/'+partnerAccount.id+'/assets/'+id+'/approve',{method:'POST',body:'{}'});
  if(result?.success){showToast('PDF version reviewed.');openPartner(partnerAccount.id);}else showToast(failMsg(result,'Could not approve brief.'));
}
async function queuePartnerEmail(button){
  const asset=document.getElementById('partner-attachment').value;button.disabled=true;
  if(!await savePartner(false)){button.disabled=false;return;}
  const result=await partnerApi('/api/partnerships/'+partnerAccount.id+'/queue-email',{method:'POST',body:JSON.stringify({asset_id:asset})});
  if(result?.outbox_id){showToast('Queued for review in Outbox.');openPartner(partnerAccount.id);}else{button.disabled=false;showToast(failMsg(result,'Could not queue email.'));}
}
// The reviewing model's verdict on the current draft (cleared when the text is edited).
function partnerReview(r){
  const v=r&&r.email_review;if(!v||!v.verdict)return '';
  const label={passed:'Checked: no problems found',revised:'Checked: fixed by the reviewer',flagged:'Checked: problems remain',unreviewed:'Not checked: the reviewer gave no answer'}[v.verdict]||v.verdict;
  const list=(xs)=>(xs||[]).map(x=>'<li>'+escHtml(x)+'</li>').join('');
  return '<div class="desk-review"><div><span class="tag '+(v.verdict==='flagged'?'t-bad':v.verdict==='unreviewed'?'':'t-good')+'">'+escHtml(label)+'</span></div>'+
    (v.issues&&v.issues.length?'<div class="k">Still open</div><ul>'+list(v.issues)+'</ul>':'')+
    (v.fixed&&v.fixed.length?'<div class="k">Fixed in the draft above</div><ul>'+list(v.fixed)+'</ul>':'')+'</div>';
}
async function generatePartnerEmail(button){
  button.disabled=true;if(!await savePartner(false)){button.disabled=false;return;}
  const result=await partnerApi('/api/partnerships/'+partnerAccount.id+'/draft',{method:'POST',body:'{}'});
  if(result?.id){showToast('Draft prepared for your review.');openPartner(result.id);}else{button.disabled=false;showToast(failMsg(result,'Writer unavailable. You can edit the email manually.'));}
}
async function generatePartnerVideo(){
  if(!await savePartner(false))return;
  const result=await partnerApi('/api/partnerships/'+partnerAccount.id+'/video-script',{method:'POST',body:'{}'});
  if(result?.id)openPartner(result.id);else showToast(failMsg(result,'Could not prepare script.'));
}
async function verifyPartnerEmail(button){
  button.disabled=true;if(!await savePartner(false)){button.disabled=false;return;}
  const result=await partnerApi('/api/partnerships/'+partnerAccount.id+'/verify-email',{method:'POST',body:'{}'});
  if(result?.id){showToast('Verification result: '+(result.email_verification||'not verified'));openPartner(result.id);}else{button.disabled=false;showToast(failMsg(result,'Could not check the address.'));}
}
async function partnerAsset(aid,id,format){
  let popup=format==='html'?window.open('about:blank','_blank'):null;
  try{
    const response=await authFetch('/api/partnerships/'+aid+'/assets/'+id+'/'+format);
    if(await lockIfRefused(response)||!response.ok){popup?.close();showToast('Could not open this asset.');return;}
    if(format==='html'){
      if(popup){popup.document.open();popup.document.write(await response.text());popup.document.close();}
      else showToast('Allow the preview popup, then try again.');
    }else{
      const url=URL.createObjectURL(await response.blob());const a=document.createElement('a');a.href=url;a.download='partnership-brief.pdf';a.click();setTimeout(()=>URL.revokeObjectURL(url),10000);
    }
  }catch{popup?.close();showToast('Could not open asset.');}
}

// Companies tab: move a company out of the cold-email pipeline into Partnerships.
function partnerCompanyAction(companyId, accountId) {
  return accountId
    ? '<button class="btn btn-secondary btn-sm" onclick="openPartnerTab(' + jsq(accountId) + ')">Open in Partnerships</button>'
    : '<button class="btn btn-secondary btn-sm" onclick="promoteCompany(' + jsq(companyId) + ')">Move to Partnerships</button>';
}

function openPartnerTab(id) {
  const btn = [...document.querySelectorAll('nav button')].find(b => (b.getAttribute('onclick') || '').includes("'partnerships'"));
  partnerAccount = {id};   // loadPartnerships leaves an open account alone
  showTab('partnerships', btn);
  openPartner(id);
}

async function promoteCompany(companyId) {
  if (!confirm('Move this company to Partnerships?\n\nCold emails already drafted for it are cancelled and no more are written. You write to them one to one from the Partnerships tab.')) return;
  const result = await partnerApi('/api/partnerships/promote', {method: 'POST', body: JSON.stringify({company_id: companyId})});
  if (result?.id) { showToast('Moved to Partnerships. Add your research before queuing an email.'); openPartnerTab(result.id); }
  else showToast(failMsg(result, 'Could not move this company.'));
}
