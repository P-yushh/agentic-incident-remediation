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

async function harness({runs = [], ready = true, failDispatch = false, saved = null, confirm = false} = {}) {
  const nodes = new Map(); const document = {activeElement:null};
  document.createElement = tag => new Element(tag, document);
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
  get('import-run-form').append(document.createElement('button'));
  document.querySelectorAll = selector => selector === '[data-tab]' ? [...nodes.values()].filter(n => n.dataset.tab)
    : selector === '[data-run-timer]' ? get('queue').querySelectorAll(selector) : [];
  const requests = []; const storage = new Map(saved ? [['air-actions-pending-request', saved]] : []);
  const records = new Map(runs.map(r => [r.id, r])); let fail = failDispatch;
  const context = vm.createContext({document, console, crypto, Date,
    Option: function(text, value) { const option = document.createElement('option'); option.textContent = text; option.value = value; return option; },
    setTimeout:()=>1, clearTimeout:()=>{}, setInterval:()=>1,
    window:{confirm:()=>confirm},
    localStorage:{getItem:key=>storage.get(key)||null, setItem:(key,value)=>storage.set(key,value), removeItem:key=>storage.delete(key)},
    fetch: async (url, options = {}) => {
      requests.push({url, ...options}); const method = options.method || 'GET'; let payload;
      if (url === '/api/actions/session') payload = {csrf:'synthetic-nonce',repository:'example/demo'};
      else if (url === '/api/actions/scenarios') payload = specs;
      else if (url === '/api/actions/readiness') payload = {ready,message:ready?'Ready':'Publish the workflow to main first'};
      else if (url === '/api/actions/runs' && method === 'GET') payload = [...records.values()];
      else if (url === '/api/actions/runs' && method === 'POST') {
        if (fail) return {ok:false,json:async()=>({detail:'synthetic dispatch connection failure'})};
        const body = JSON.parse(options.body); payload = runRecord(body.scenario,{id:body.request_id,owned:true,stage:'queued'}); records.set(payload.id,payload);
      } else if (url.endsWith('/cancel')) {
        const id = url.split('/')[4]; payload = {...records.get(id),notice:'Cancellation requested'}; records.set(id,payload);
      } else if (url === '/api/actions/import') { payload = runs[0]; records.set(payload.id,payload); }
      else payload = records.get(url.split('/')[4]);
      assert.ok(payload, `unhandled request ${method} ${url}`);
      return {ok:true,json:async()=>payload};
    }});
  vm.runInContext(controller, context); await flush(); await flush();
  assert.notEqual(get('runtime-mode').textContent,'Actions session unavailable',get('actions-readiness').textContent);
  return {get,requests,storage,records,context,allowDispatch:()=>{fail=false;},
    change:async id=>{get('type').value=id; await get('type').emit('change');},
    consent:async()=>{get('live-consent').checked=true; await get('live-consent').emit('change');},
    submit:async()=>{await get('incident-form').emit('submit'); await flush();}};
}

test('empty history and scenario preview do not dispatch or spend inference', async()=>{
  const h=await harness({ready:false});
  assert.equal(h.get('queue-count').textContent,'00'); assert.equal(h.get('queue-empty').hidden,false);
  assert.equal(h.get('type').children.length,3); assert.equal(h.get('open-simulator').disabled,false);
  await h.get('open-simulator').emit('click'); await h.consent();
  assert.equal(h.get('simulator').open,true); assert.equal(h.get('submit-incident').disabled,true);
  assert.match(h.get('simulator-readiness').textContent,/Publish/);
  assert.equal(h.requests.filter(r=>r.method==='POST').length,0);
});

test('scenario changes update read-only inputs and reset consent', async()=>{
  const h=await harness();
  for (const spec of specs) {
    await h.consent(); await h.change(spec.id);
    assert.equal(h.get('service').value,spec.service); assert.equal(h.get('description').value,spec.description);
    assert.equal(h.get('live-consent').checked,false); assert.equal(h.get('submit-incident').disabled,true);
  }
});

for (const spec of specs) test(`explicit consent dispatches only selected scenario: ${spec.id}`, async()=>{
  const h=await harness(); await h.change(spec.id); await h.submit();
  assert.equal(h.requests.filter(r=>r.method==='POST').length,0);
  await h.consent(); await h.submit();
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
  assert.equal(h.get('artifact').textContent,review.content); assert.equal(h.get('proposal-download').textContent,'Download Terraform');
  assert.equal(h.get('elapsed').textContent,'00:10'); assert.match(h.get('validation-details').textContent,/terraform_validate/);
});

test('failure panel shows job, safe reason and link without claiming validation', async()=>{
  const h=await harness({runs:[runRecord(specs[0].id,{stage:'failed',failure_job:'prepare',failure_step:'Build real Kubernetes validator without model credentials',failure_reason:'Validator image build failed before model inference.',failure_url:'https://github.com/example/demo/actions/runs/123/job/456',finished_at:'2026-10-18T10:00:38Z'})]});
  assert.equal(h.get('run-failure').hidden,false); assert.match(h.get('failure-heading').textContent,/Build real/);
  assert.match(h.get('failure-detail').textContent,/before model inference/); assert.match(h.get('failure-link').href,/job\/456/);
  assert.equal(h.get('publication').hidden,true);
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
  const h=await harness({failDispatch:true}); await h.consent(); await h.submit();
  const first=JSON.parse(h.requests.find(r=>r.method==='POST').body).request_id;
  assert.ok(h.storage.size); await h.change(specs[1].id); await h.consent(); await h.submit();
  assert.equal(h.requests.filter(r=>r.method==='POST').length,1); assert.match(h.get('simulator-error').textContent,/another scenario/);
  await h.change(specs[0].id); await h.consent(); h.allowDispatch(); await h.submit();
  assert.equal(JSON.parse(h.requests.filter(r=>r.method==='POST')[1].body).request_id,first);
});

test('a new controller restores saved run history without a POST', async()=>{
  const record=runRecord(specs[1].id,{stage:'completed',finished_at:'2026-10-18T10:00:10Z'});
  const h=await harness({runs:[record]}); assert.equal(h.get('queue-count').textContent,'01');
  const reloaded=await harness({runs:[record]}); assert.equal(reloaded.get('execution').textContent,specs[1].title);
  assert.equal(reloaded.requests.filter(r=>r.method==='POST').length,0);
});
