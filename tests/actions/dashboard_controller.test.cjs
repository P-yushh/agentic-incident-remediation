/* Controller regressions, not browser rendering tests. No network, credentials or npm packages. */
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const crypto = require('node:crypto');
const specs = JSON.parse(fs.readFileSync(0, 'utf8'));
const assets = path.resolve(__dirname, '../../src/agentic_incident_remediation/api');
const html = fs.readFileSync(path.join(assets, 'actions-dashboard.html'), 'utf8');
const controller = fs.readFileSync(path.join(assets, 'actions-dashboard.js'), 'utf8');

class Element {
  constructor(tag, document) { this.tagName = tag.toUpperCase(); this.document = document; this.children = []; this.dataset = {}; this.attrs = {}; this.events = {}; this.value = ''; this.disabled = false; this.hidden = false; this.checked = false; this._text = ''; }
  set textContent(text) { this._text = String(text); this.children = []; }
  get textContent() { return this._text + this.children.map(c => c.textContent).join(''); }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; this._text = ''; if (this.tagName === 'SELECT') this.value = children[0]?.value || ''; }
  setAttribute(name, value) { this.attrs[name] = String(value); }
  getAttribute(name) { return this.attrs[name] ?? null; }
  removeAttribute(name) { delete this.attrs[name]; }
  addEventListener(name, handler) { (this.events[name] ||= []).push(handler); }
  async emit(name, extra = {}) { for (const handler of this.events[name] || []) await handler({preventDefault(){}, ...extra}); }
  focus() { this.document.activeElement = this; }
  showModal() { this.open = true; }
  close() { this.open = false; }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  querySelectorAll(selector) { return this.children.flatMap(c => [c, ...c.querySelectorAll(selector)]).filter(c => selector === 'button' ? c.tagName === 'BUTTON' : selector === '[data-run-timer]' ? !!c.dataset.runTimer : false); }
}
const flush = () => new Promise(resolve => setImmediate(resolve));

function runRecord(scenario = specs[0].id, extra = {}) {
  return {id:'11111111-1111-4111-8111-111111111111', repository:'example/demo', scenario, owned:false,
    run_id:123, run_url:'https://github.com/example/demo/actions/runs/123', head_sha:'a'.repeat(40),
    created_at:'2026-10-18T10:00:00Z', started_at:'2026-10-18T10:00:00Z', finished_at:null,
    stage:'awaiting_review', steps:[], review:null, receipt:null, notice:null, ...extra};
}

async function harness({runs = [], ready = true, ollamaEnabled = false, ollamaSupported = false, failDispatch = false, failDiff = false, diffLines = null, saved = null, appearance = null, confirm = false} = {}) {
  const nodes = new Map(); const document = {activeElement:null};
  document.createElement = tag => new Element(tag, document);
  document.documentElement = document.createElement('html');
  document.getElementById = id => { assert.ok(nodes.has(id), `missing shell ID ${id}`); return nodes.get(id); };
  for (const match of html.matchAll(/<([a-z][a-z0-9]*)\b([^>]*\bid="([^"]+)"[^>]*)>/g)) {
    const element = new Element(match[1], document); element.id = match[3];
    for (const attr of match[2].matchAll(/([\w-]+)(?:="([^"]*)")?/g)) {
      const [ , name, value = '' ] = attr; element.attrs[name] = value;
      if (name.startsWith('data-')) element.dataset[name.slice(5)] = value;
      if (['disabled','hidden','checked'].includes(name)) element[name] = true;
      if (name === 'value') element.value = value;
    }
    nodes.set(element.id, element);
  }
  const get = id => document.getElementById(id);
  get('generation-mode').value = 'gemini';
  get('ollama-output-tokens').value = '8192'; get('ollama-timeout').value = '600';
  document.querySelectorAll = selector => selector === '[data-tab]' ? [...nodes.values()].filter(n => n.dataset.tab)
    : selector === '[data-run-timer]' ? get('queue').querySelectorAll(selector) : [];
  const requests = []; const storage = new Map(saved ? [['air-actions-pending-request', saved]] : []);
  if (appearance) storage.set('air-appearance',appearance);
  const records = new Map(runs.map(r => [r.id, r])); let fail = failDispatch;
  const context = vm.createContext({document, console, crypto, Date,
    Option: function(text, value) { const option = document.createElement('option'); option.textContent = text; option.value = value; return option; },
    setTimeout:()=>1, clearTimeout:()=>{}, setInterval:()=>1,
    window:{confirm:()=>confirm},
    localStorage:{getItem:key=>storage.get(key)||null, setItem:(key,value)=>storage.set(key,value), removeItem:key=>storage.delete(key)},
    fetch: async (url, options = {}) => {
      requests.push({url, ...options}); const method = options.method || 'GET'; let payload;
      if (url === '/api/actions/session') payload = {csrf:'synthetic-nonce',repository:'example/demo'};
      else if (url === '/api/actions/generation-modes') payload = {experimental_ollama:ollamaEnabled};
      else if (url === '/api/actions/scenarios') payload = specs;
      else if (url === '/api/actions/readiness') payload = {ready,ollama_supported:ollamaSupported,message:ready?'Ready':'Publish the workflow to main first'};
      else if (url === '/api/actions/runs' && method === 'GET') payload = [...records.values()];
      else if (url === '/api/actions/runs' && method === 'POST') {
        if (fail) return {ok:false,json:async()=>({detail:'synthetic dispatch connection failure'})};
        const body = JSON.parse(options.body); payload = runRecord(body.scenario,{id:body.request_id,owned:true,stage:'queued'}); records.set(payload.id,payload);
      } else if (url.endsWith('/diff')) {
        if (failDiff) return {ok:false,json:async()=>({detail:'comparison unavailable'})};
        const record=records.get(url.split('/')[4]);
        const lines=diffLines || [{kind:'header',text:'@@ -1,2 +1,2 @@'},{kind:'context',text:'unchanged context',old_line:1,new_line:1},{kind:'removed',text:'old <script>literal text</script>',old_line:2,new_line:null},{kind:'added',text:'new reference',old_line:null,new_line:2}];
        payload={baseline_source:'synthetic_reference',baseline_label:'Synthetic reference baseline — not captured from this run or a real cluster',artifact_sha256:record.review.artifact_sha256,additions:lines.filter(l=>l.kind==='added').length,deletions:lines.filter(l=>l.kind==='removed').length,lines};
      } else if (url.endsWith('/cancel')) {
        const id = url.split('/')[4]; payload = {...records.get(id),notice:'Cancellation requested'}; records.set(id,payload);
      } else if (url === '/api/actions/import') { payload = runs[0]; records.set(payload.id,payload); }
      else payload = records.get(url.split('/')[4]);
      assert.ok(payload, `unhandled request ${method} ${url}`);
      return {ok:true,json:async()=>payload};
    }});
  vm.runInContext(controller, context); await flush(); await flush();
  assert.equal(get('queue-error').textContent,'');
  return {get,requests,storage,records,context,document,allowDispatch:()=>{fail=false;},
    change:async id=>{get('type').value=id; await get('type').emit('change');},
    submit:async()=>{await get('incident-form').emit('submit'); await flush();}};
}

test('empty history and scenario preview do not dispatch or spend inference', async()=>{
  const h=await harness({ready:false});
  assert.equal(h.get('queue-count').textContent,'00'); assert.equal(h.get('queue-empty').hidden,false);
  assert.equal(h.get('type').children.length,3); assert.equal(h.get('open-simulator').disabled,false);
  await h.get('open-simulator').emit('click');
  assert.equal(h.get('simulator').open,true); assert.equal(h.get('submit-incident').disabled,true);
  assert.match(h.get('simulator-readiness').textContent,/Publish/);
  assert.equal(h.get('simulator-readiness').hidden,false);
  assert.equal(h.requests.filter(r=>r.method==='POST').length,0);
});

test('experimental Ollama is selectable only when enabled and handoff is published', async()=>{
  const h=await harness({ollamaEnabled:true,ollamaSupported:false});
  h.get('generation-mode').value='ollama_local'; await h.get('generation-mode').emit('change');
  assert.equal(h.get('ollama-settings').hidden,false);
  assert.equal(h.get('submit-incident').disabled,true);
  assert.match(h.get('simulator-readiness').textContent,/not published/);
  await h.submit(); assert.equal(h.requests.filter(r=>r.method==='POST').length,0);
  const ready=await harness({ollamaEnabled:true,ollamaSupported:true});
  ready.get('generation-mode').value='ollama_local'; await ready.get('generation-mode').emit('change');
  assert.equal(ready.get('submit-incident').disabled,false);
  await ready.submit();
  const body=JSON.parse(ready.requests.find(r=>r.method==='POST').body);
  assert.equal(body.generation_mode,'ollama_local');
  assert.equal(body.ollama_output_tokens,8192); assert.equal(body.ollama_timeout_seconds,600);
});

test('local generation shows an active phase and local failures do not claim a GitHub job failed', async()=>{
  const local=runRecord(specs[0].id,{owned:true,run_id:null,run_url:null,stage:'local_generating',generation_mode:'ollama_local'});
  const h=await harness({runs:[local]});
  assert.match(h.get('status').textContent,/Generating locally/);
  assert.match(h.get('actions-progress').children[1].className,/active/);
  assert.match(h.get('execution-description').textContent,/Ollama/);
  assert.match(h.get('queue').textContent,/Cancel generation/);
  const failed=await harness({runs:[{...local,stage:'failed',failure_origin:'local',failure_phase:'preparing',failure_reason:'Local model timed out'}]});
  assert.equal(failed.get('decision-title').textContent,'Local generation or handoff failed');
  assert.match(failed.get('decision-note').textContent,/Local model timed out/);
});

test('scenario changes update read-only inputs; preview does not dispatch and no checkbox exists', async()=>{
  const h=await harness();
  for (const spec of specs) {
    await h.change(spec.id);
    assert.equal(h.get('service').value,spec.service); assert.equal(h.get('title').value,spec.title);
    assert.equal(h.get('submit-incident').disabled,false);
  }
  assert.doesNotMatch(html,/live-consent|type="checkbox"/);
  assert.equal(h.requests.filter(r=>r.method==='POST').length,0);
});

test('simulator has no scope preview, concise Simulate action, and accurate simulation wording', async()=>{
  const h=await harness();
  await h.get('open-simulator').emit('click');
  assert.equal(h.get('simulator-readiness').hidden,true);
  assert.equal(h.get('simulator-readiness').textContent,'');
  assert.doesNotMatch(html.match(/<p[^>]*id="simulator-note"[^>]*>([\s\S]*?)<\/p>/)[1],/Gemini|A real PR requires separate approval|Provider quota and billing/);
  assert.doesNotMatch(html,/scenario-details|id="description"|Remediation scope/);
  assert.match(html,/Choose a scenario to simulate\. This will create a simulated incident\./);
  assert.match(html,/id="submit-incident"[^>]*>Simulate<\/button>/);
  await h.get('close-simulator').emit('click'); await h.get('open-simulator').emit('click');
  assert.equal(h.get('simulator-readiness').hidden,true);
  assert.equal(h.requests.filter(r=>r.method==='POST').length,0);
});

for (const spec of specs) test(`deliberate Start dispatches only selected scenario: ${spec.id}`, async()=>{
  const h=await harness(); await h.change(spec.id);
  assert.equal(h.requests.filter(r=>r.method==='POST').length,0);
  await h.get('open-simulator').emit('click'); await h.submit();
  const writes=h.requests.filter(r=>r.method==='POST'); assert.equal(writes.length,1);
  assert.equal(JSON.parse(writes[0].body).scenario,spec.id); assert.equal(JSON.parse(writes[0].body).confirm_live_calls,true);
  assert.equal(h.get('simulator').open,false); assert.equal(h.get('open-simulator').disabled,true);
});

test('tabs support keyboard navigation; provenance missing never becomes a pass', async()=>{
  const h=await harness({runs:[runRecord(specs[2].id)]});
  await h.get('tab-investigation').emit('click'); assert.equal(h.get('panel-investigation').hidden,false);
  await h.get('tab-investigation').emit('keydown',{key:'End'}); assert.equal(h.get('panel-activity').hidden,false);
  assert.match(h.get('validation-status').textContent,/Awaiting/); assert.equal(h.get('proposal-download').hidden,true);
  assert.equal(h.get('execution').textContent,specs[2].title);
});

test('verified Terraform output is shown as Terraform, not fabricated YAML or metrics', async()=>{
  const review={scenario:specs[2].id,content:'resource "aws_security_group_rule" "https" {}',artifact_path:'remediations/rule.tf',validation_tool:'terraform_validate',artifact_sha256:'c'.repeat(64),execution_id:'fake',model_calls:2,occurred_at:'2026-10-18T10:00:00Z',requested_at:'2026-10-18T10:00:10Z'};
  const h=await harness({runs:[runRecord(specs[2].id,{review})]});
  assert.match(h.get('artifact').textContent,/new reference/); assert.equal(h.get('proposal-download').textContent,'Download Terraform');
  assert.equal(h.get('elapsed').textContent,'00:10'); assert.match(h.get('validation-details').textContent,/terraform_validate/);
});

test('single inline block uses neutral context, red minus and green plus without mode controls or gutters', async()=>{
  const review={scenario:specs[1].id,content:'proposed file\n',artifact_path:'remediations/worker.yaml',validation_tool:'kubeconform',artifact_sha256:'d'.repeat(64),model_calls:2,occurred_at:'2026-10-18T10:00:00Z',requested_at:'2026-10-18T10:00:10Z'};
  const h=await harness({runs:[runRecord(specs[1].id,{review})]});
  const rows=h.get('artifact').children;
  assert.equal(rows.length,3); assert.equal(rows[0].className,'diff-line context'); assert.equal(rows[0].textContent,' unchanged context');
  assert.equal(rows[1].className,'diff-line removed'); assert.match(rows[1].textContent,/-old <script>literal text<\/script>/);
  assert.equal(rows[2].className,'diff-line added'); assert.match(rows[2].textContent,/\+new reference/);
  assert.equal(rows[1].children.length,2); assert.equal(rows[1].children[0].className,'diff-marker');
  assert.doesNotMatch(html,/show-diff|show-full|Proposal display/);
  assert.doesNotMatch(h.get('artifact').textContent,/@@|old_line|new_line/);
  assert.match(h.get('comparison-source').textContent,/Synthetic reference/); assert.match(h.get('diff-count').textContent,/1 added/);
  await h.get('refresh-run').emit('click'); await flush(); assert.equal(h.get('artifact').children.length,3);
  assert.equal(h.requests.filter(r=>r.url.endsWith('/diff')).length,1);
  assert.equal(h.requests.filter(r=>r.method==='POST').length,0);
});

test('failed comparison falls back to the exact file in the same block without claiming highlights', async()=>{
  const review={scenario:specs[1].id,content:'exact proposal',artifact_sha256:'e'.repeat(64)};
  const h=await harness({runs:[runRecord(specs[1].id,{review})],failDiff:true});
  assert.equal(h.get('artifact').textContent,review.content);
  assert.match(h.get('diff-source').textContent,/Comparison unavailable.*without highlights/);
  assert.equal(h.get('code-view').className,'air-diff');
});

test('unchanged proposal shows the exact file with a no-differences note', async()=>{
  const review={scenario:specs[1].id,content:'unchanged proposal\n',artifact_sha256:'f'.repeat(64)};
  const h=await harness({runs:[runRecord(specs[1].id,{review})],diffLines:[]});
  assert.equal(h.get('artifact').textContent,review.content);
  assert.match(h.get('diff-source').textContent,/No differences/);
  assert.match(h.get('diff-count').textContent,/0 added/);
  assert.equal(h.get('code-view').className,'air-diff');
});

test('failure panel shows job, safe reason and link without claiming validation', async()=>{
  const h=await harness({runs:[runRecord(specs[0].id,{stage:'failed',failure_job:'prepare',failure_step:'Build real Kubernetes validator without model credentials',failure_reason:'Validator image build failed before model inference.',failure_url:'https://github.com/example/demo/actions/runs/123/job/456',finished_at:'2026-10-18T10:00:38Z'})]});
  assert.equal(h.get('run-failure').hidden,false); assert.match(h.get('failure-heading').textContent,/Build real/);
  assert.match(h.get('failure-detail').textContent,/before model inference/); assert.match(h.get('failure-link').href,/job\/456/);
  assert.equal(h.get('publication').hidden,true);
});

test('GitHub failure marks its numbered phase red, confirmed earlier phases green and queue failures red', async()=>{
  const record=runRecord(specs[0].id,{stage:'failed',failure_job:'prepare',failure_phase:'preparing',completed_phases:['queued'],failure_reason:'AI provider quota or rate limit reached (HTTP 429).',failure_code:'provider_rate_limit'});
  const h=await harness({runs:[record]});
  assert.equal(h.get('actions-progress').children[0].className,'air-step done');
  assert.equal(h.get('actions-progress').children[1].className,'air-step failed');
  assert.equal(h.get('actions-progress').children[1].children[0].textContent,'2');
  assert.equal(h.get('actions-progress').children[2].className,'air-step not-run');
  assert.equal(h.get('decision-title').textContent,'Execution failed on GitHub');
  assert.match(h.get('decision-note').textContent,/HTTP 429/);
  const state=h.get('queue').querySelectorAll('button')[0].children[3];
  assert.equal(state.children[0].className,'air-queue-stage red');
  h.records.set(record.id,{...record,stage:'failed',failure_job:'publish',failure_phase:'publishing',completed_phases:['queued','preparing']});
  await h.get('refresh-run').emit('click'); await flush();
  assert.equal(h.get('actions-progress').children[3].className,'air-step failed');
  assert.equal(h.get('actions-progress').children[2].className,'air-step'); // Do not invent human approval.
});

test('human rejection has a distinct banner, stopped review phase and no cancel or publish claim', async()=>{
  const h=await harness({runs:[runRecord(specs[1].id,{stage:'rejected',owned:true,review_rejection:{reviewer:'human-reviewer',environment:'remediation-demo-review'},finished_at:'2026-10-18T10:02:00Z'})]});
  assert.equal(h.get('status').textContent,'Rejected by reviewer');
  assert.equal(h.get('run-failure').hidden,true); assert.equal(h.get('run-rejection').hidden,false);
  assert.match(h.get('rejection-detail').textContent,/human-reviewer rejected/);
  assert.match(h.get('rejection-detail').textContent,/not a technical failure/);
  assert.match(h.get('rejection-link').href,/actions\/runs\/123/);
  assert.equal(h.get('actions-progress').children[2].className,'air-step rejected');
  assert.match(h.get('actions-progress').children[3].textContent,/not run/);
  assert.match(h.get('activity-list').textContent,/Rejected by reviewer/);
  assert.match(h.get('decision-title').textContent,/publication blocked/);
  assert.equal(h.get('publication').hidden,true); assert.equal(h.get('open-simulator').disabled,false);
  assert.equal(h.get('queue').querySelectorAll('button').some(b=>b.dataset.action==='cancel'),false);
  assert.equal(h.requests.filter(r=>r.method==='POST').length,0);
  // Switching incidents clears the rejection state and restores a real failure panel.
  const record=h.records.values().next().value;
  h.records.set(record.id,{...record,stage:'failed',review_rejection:null});
  await h.get('refresh-run').emit('click'); await flush();
  assert.equal(h.get('run-rejection').hidden,true); assert.equal(h.get('run-failure').hidden,false);
});

test('cancellation needs confirmation and is not offered for read-only imports', async()=>{
  const h=await harness({runs:[runRecord(specs[0].id,{owned:true})]});
  const cancel=h.get('queue').querySelectorAll('button').find(b=>b.dataset.action==='cancel');
  assert.ok(cancel); await cancel.emit('click'); assert.equal(h.requests.filter(r=>r.method==='POST').length,0);
  const yes=await harness({runs:[runRecord(specs[0].id,{owned:true})],confirm:true});
  await yes.get('queue').querySelectorAll('button').find(b=>b.dataset.action==='cancel').emit('click');
  assert.equal(yes.requests.filter(r=>r.url.endsWith('/cancel')).length,1);
  const imported=await harness({runs:[runRecord()]});
  assert.ok(!imported.get('queue').querySelectorAll('button').some(b=>b.dataset.action==='cancel'));
});

test('uncertain start retains request identity and cannot switch scenarios', async()=>{
  const h=await harness({failDispatch:true}); await h.submit();
  const first=JSON.parse(h.requests.find(r=>r.method==='POST').body).request_id;
  assert.ok(h.storage.size); await h.change(specs[1].id); await h.submit();
  assert.equal(h.requests.filter(r=>r.method==='POST').length,1); assert.match(h.get('simulator-error').textContent,/another scenario/);
  await h.change(specs[0].id); h.allowDispatch(); await h.submit();
  assert.equal(JSON.parse(h.requests.filter(r=>r.method==='POST')[1].body).request_id,first);
});

test('a new controller restores saved run history without a POST', async()=>{
  const record=runRecord(specs[1].id,{stage:'completed',finished_at:'2026-10-18T10:00:10Z'});
  const h=await harness({runs:[record]}); assert.equal(h.get('queue-count').textContent,'01');
  const reloaded=await harness({runs:[record]}); assert.equal(reloaded.get('execution').textContent,specs[1].title);
  assert.equal(reloaded.requests.filter(r=>r.method==='POST').length,0);
});

test('Proposals shows only verified pending human reviews and removes resolved items on refresh', async()=>{
  const pending=runRecord(specs[0].id);
  const verified=runRecord(specs[1].id,{id:'22222222-2222-4222-8222-222222222222',review:{content:'file',artifact_sha256:'a'.repeat(64)}});
  const resolved=['completed','rejected','failed','publishing','cancelled'].map((stage,index)=>runRecord(specs[1].id,{id:`33333333-3333-4333-8333-33333333333${index}`,stage,review:verified.review,review_rejection:stage==='rejected'?{reviewer:'human'}:null}));
  const h=await harness({runs:[pending,verified,...resolved]});
  await h.get('nav-proposals').emit('click'); await flush();
  assert.equal(h.get('queue-count').textContent,'01');
  assert.equal(h.get('execution').textContent,specs[1].title);
  assert.equal(h.get('page-heading').textContent,'Proposal workspace');
  assert.equal(h.get('nav-proposals').getAttribute('aria-current'),'page');
  assert.equal(h.get('nav-incidents').getAttribute('aria-current'),null);
  assert.equal(h.get('panel-proposal').hidden,false);
  assert.equal(h.get('workspace-empty').hidden,true);
  await h.get('nav-incidents').emit('click'); await flush();
  assert.equal(h.get('queue-count').textContent,'07');
  assert.equal(h.get('nav-incidents').getAttribute('aria-current'),'page');
  await h.get('nav-proposals').emit('click'); await flush();
  h.records.set(verified.id,{...verified,stage:'publishing'});
  await h.get('refresh-run').emit('click'); await flush();
  assert.equal(h.get('queue-count').textContent,'00');
  assert.equal(h.get('workspace-empty').hidden,false); assert.equal(h.get('execution-detail').hidden,true);
  const empty=await harness({runs:[pending]});
  await empty.get('nav-proposals').emit('click');
  assert.equal(empty.get('workspace-empty').hidden,false); assert.equal(empty.get('execution-detail').hidden,true);
  assert.equal(empty.get('queue-count').textContent,'00');
  await empty.get('nav-incidents').emit('click');
  assert.equal(empty.get('execution-detail').hidden,false);
  assert.equal(h.requests.filter(r=>r.method==='POST').length,0);
});

test('validation counts are evidence-backed; missing historical counts stay unknown', async()=>{
  const review={content:'file',artifact_sha256:'a'.repeat(64),validation_attempts:3,validation_passes:1,retry_count:2};
  const h=await harness({runs:[runRecord(specs[0].id,{review})]});
  assert.equal(h.get('validation-counts').textContent,'3 validation attempts · 2 retries · 1 passed');
  h.records.set(h.records.keys().next().value,runRecord(specs[0].id,{review:{...review,validation_attempts:null,validation_passes:null,retry_count:null}}));
  await h.get('refresh-run').emit('click'); await flush();
  assert.match(h.get('validation-counts').textContent,/not recorded/);
});

test('short incident aliases are distinct, queue is concise and removed controls stay absent', async()=>{
  const one=runRecord(specs[0].id,{id:'abcdef01-1111-4111-8111-111111111111'});
  const two=runRecord(specs[1].id,{id:'abcdef02-1111-4111-8111-111111111111'});
  const h=await harness({runs:[one,two]});
  const cards=h.get('queue').querySelectorAll('button');
  assert.equal(cards.length,2);
  assert.match(cards[0].children[0].textContent,/^INC-[A-Z0-9]{6,8}$/);
  assert.notEqual(cards[0].children[0].textContent,cards[1].children[0].textContent);
  assert.match(cards[0].textContent,/Awaiting GitHub review/);
  assert.doesNotMatch(cards[0].textContent,/synthetic|Dashboard-started|Read-only import|RUN 123/);
  assert.doesNotMatch(html,/import-run-form|Load existing run|air-side-bottom|ACTIONS · SYNTHETIC/);
  assert.ok(html.indexOf('id="run-rejection"')>html.indexOf('aria-label="Verdict"'));
});

test('theme toggle switches sun and moon, persists appearance and restores it', async()=>{
  const h=await harness();
  assert.equal(h.document.documentElement.dataset.appearance,'light');
  assert.equal(h.get('theme-moon').getAttribute('hidden'),null); assert.equal(h.get('theme-sun').getAttribute('hidden'),'');
  await h.get('theme-toggle').emit('click');
  assert.equal(h.document.documentElement.dataset.appearance,'dark');
  assert.equal(h.storage.get('air-appearance'),'dark'); assert.equal(h.get('theme-sun').getAttribute('hidden'),null);
  assert.equal(h.get('theme-moon').getAttribute('hidden'),'');
  assert.equal(h.get('theme-toggle').getAttribute('aria-label'),'Switch to light theme');
  const restored=await harness({appearance:h.storage.get('air-appearance')});
  assert.equal(restored.document.documentElement.dataset.appearance,'dark');
  await restored.get('theme-toggle').emit('click');
  assert.equal(restored.document.documentElement.dataset.appearance,'light');
  await h.get('footer-policy').emit('click'); assert.equal(h.get('policy').open,true);
  assert.equal(h.requests.filter(r=>r.method==='POST').length,0);
});
