'use strict';

const byId = (id) => document.getElementById(id);
const view = { runs: new Map(), scenarios: new Map(), selected: null, csrf: '', ready: false, busy: false, timer: null, failures: 0 };
const finished = new Set(['completed', 'failed', 'cancelled']);
const simulator = byId('simulator');
const labels = {
  dispatching: 'Sending request', dispatch_unknown: 'Dispatch outcome uncertain', queued: 'Queued',
  preparing: 'Generating and validating', awaiting_review: 'Awaiting GitHub review',
  publishing: 'Publishing approved PR', completed: 'Workflow succeeded', failed: 'Workflow failed',
  cancelled: 'Workflow cancelled',
};

function setLink(id, url) {
  const element = byId(id); element.hidden = !url;
  if (url) element.href = url; else element.removeAttribute('href');
}

const consent = byId('live-consent');
function selectedScenario() { return view.scenarios.get(byId('type').value); }
function scenarioFor(run) { return view.scenarios.get(run.review?.scenario || run.scenario); }
function resetConsent() {
  consent.checked = false; byId('submit-incident').disabled = true;
}
function updateScenario() {
  resetConsent();
  const spec = selectedScenario(); if (!spec) return;
  byId('service').value = spec.service; byId('title').value = spec.title;
  byId('description').value = spec.description;
}
byId('type').addEventListener('change', updateScenario);
consent.addEventListener('change', () => { byId('submit-incident').disabled = !consent.checked || !view.ready || view.busy; });
const refreshButton = byId('refresh-run');
refreshButton.addEventListener('click', () => poll(true));
const progress = byId('actions-progress');
const readiness = byId('actions-readiness');
function renderReadiness() {
  byId('simulator-readiness').textContent = view.ready
    ? 'Ready. Starting requires deliberate consent below.' : readiness.textContent;
  byId('submit-incident').disabled = !view.ready || !consent.checked || view.busy;
}
const importForm = byId('import-run-form');
const importInput = byId('import-run');
const importButton = importForm.querySelector('button');
const tabs = [...document.querySelectorAll('[data-tab]')];
function showTab(name, focus = false) {
  for (const tab of tabs) {
    const selected = tab.dataset.tab === name;
    tab.setAttribute('aria-selected', String(selected)); tab.tabIndex = selected ? 0 : -1;
    byId(tab.getAttribute('aria-controls')).hidden = !selected;
    if (selected && focus) tab.focus();
  }
}
tabs.forEach((tab, index) => {
  tab.addEventListener('click', () => showTab(tab.dataset.tab));
  tab.addEventListener('keydown', (event) => {
    const next = event.key === 'Home' ? 0 : event.key === 'End' ? tabs.length - 1
      : event.key === 'ArrowRight' ? (index + 1) % tabs.length
      : event.key === 'ArrowLeft' ? (index + tabs.length - 1) % tabs.length : -1;
    if (next < 0) return; event.preventDefault(); showTab(tabs[next].dataset.tab, true);
  });
});
byId('nav-incidents').addEventListener('click', () => { showTab('investigation'); byId('execution').focus(); });
byId('nav-proposals').addEventListener('click', () => { showTab('proposal'); byId('execution').focus(); });
byId('nav-policy').addEventListener('click', () => byId('policy').showModal());
byId('close-policy').addEventListener('click', () => byId('policy').close());

function textElement(tag, text, className) {
  const element = document.createElement(tag); element.textContent = text;
  if (className) element.className = className; return element;
}
function evidenceItem(title, detail) {
  const item = document.createElement('li'); item.append(textElement('h3', title), textElement('p', detail)); return item;
}
function activityItem(title, detail, timestamp) {
  const item = document.createElement('li'); item.className = 'air-event';
  item.append(textElement('h3', title), textElement('p', detail));
  if (timestamp) {
    const time = textElement('time', new Date(timestamp).toLocaleString()); time.dateTime = timestamp; item.append(time);
  }
  return item;
}

async function request(url, options = {}) {
  const response = await fetch(url, {
    ...options, headers: { 'content-type': 'application/json', 'x-air-csrf': view.csrf },
  });
  const payload = await response.json();
  if (!response.ok) {
    const detail = Array.isArray(payload.detail) ? payload.detail.map((item) => item.msg).join('; ') : payload.detail;
    throw new Error(detail || 'The request did not complete. No automatic dispatch retry was made.');
  }
  return payload;
}
function age(run) {
  const start = run.review?.occurred_at || run.started_at || run.created_at;
  const stop = run.review?.requested_at || run.finished_at;
  const seconds = Math.max(0, Math.floor(((stop ? Date.parse(stop) : Date.now()) - Date.parse(start)) / 1000));
  const hours = Math.floor(seconds / 3600); const minutes = Math.floor(seconds / 60) % 60;
  const text = (hours ? [hours, minutes, seconds % 60] : [minutes, seconds % 60]).map((value) => String(value).padStart(2, '0')).join(':');
  return { text, note: run.review ? ' · alert age, stopped at review' : stop ? ' · run elapsed, stopped' : ' · run elapsed (alert not available yet)' };
}
function tickTimers() {
  document.querySelectorAll('[data-run-timer]').forEach((element) => {
    const run = view.runs.get(element.dataset.runTimer); if (!run) return;
    const elapsed = age(run); element.textContent = elapsed.text + elapsed.note;
  });
  const run = view.runs.get(view.selected);
  if (run) { const elapsed = age(run); byId('elapsed-label').textContent = run.review ? 'Alert age' : 'Run elapsed'; byId('elapsed').textContent = elapsed.text; byId('elapsed-note').textContent = elapsed.note; }
}
function renderQueue() {
  const focused = document.activeElement;
  const focusedRun = focused?.dataset.run;
  const focusedAction = focused?.dataset.action;
  byId('queue-empty').hidden = view.runs.size > 0;
  const items = [...view.runs.values()].sort((a, b) => Date.parse(b.created_at) - Date.parse(a.created_at)).map((run) => {
    const item = document.createElement('li'); item.className = 'air-queue-item';
    const button = document.createElement('button'); button.type = 'button'; button.className = 'air-incident';
    button.dataset.run = run.id; button.dataset.action = 'select';
    button.setAttribute('aria-pressed', String(run.id === view.selected));
    const meta = textElement('span', run.run_id ? `RUN ${run.run_id}` : `REQUEST ${run.id.slice(0, 8)}`, 'air-incident-top');
    const spec = scenarioFor(run);
    const title = textElement('span', spec?.title || 'Scenario not yet verified', 'air-incident-title');
    const detail = textElement('span', `${spec?.service || 'Service not yet verified'} · ${labels[run.stage]}`, 'air-incident-service');
    const source = textElement('span', run.owned ? 'Dashboard-started · synthetic' : 'Read-only import · synthetic', 'air-incident-service');
    const timer = document.createElement('time'); timer.className = 'air-timer'; timer.dataset.runTimer = run.id;
    button.append(meta, title, detail, source, timer); button.addEventListener('click', () => select(run.id)); item.append(button);
    if (run.owned && run.run_id && !finished.has(run.stage)) {
      const cancel = document.createElement('button'); cancel.type = 'button'; cancel.className = 'air-button reject';
      cancel.dataset.run = run.id; cancel.dataset.action = 'cancel';
      cancel.textContent = 'Cancel workflow'; cancel.disabled = view.busy;
      cancel.addEventListener('click', async () => {
        if (!window.confirm('Cancel this GitHub run? In-flight operations may finish, and an existing PR will NOT be undone.')) return;
        view.busy = true; renderQueue();
        try { remember(await request(`/api/actions/runs/${run.id}/cancel`, { method: 'POST', body: '{}' })); }
        catch (error) { byId('queue-error').textContent = error.message; }
        finally { view.busy = false; renderQueue(); }
      });
      item.append(cancel);
    }
    return item;
  });
  byId('queue').replaceChildren(...items); byId('queue-count').textContent = String(items.length).padStart(2, '0'); tickTimers();
  // Polling must not discard keyboard focus when rebuilding incident cards.
  if (focusedRun) {
    const target = [...byId('queue').querySelectorAll('button')].find((button) => button.dataset.run === focusedRun && button.dataset.action === focusedAction);
    if (target && !target.disabled) target.focus({ preventScroll: true });
  }
  const active = [...view.runs.values()].some((run) => run.owned && !finished.has(run.stage));
  byId('open-simulator').disabled = active || view.busy;
}
function render(run) {
  const spec = scenarioFor(run);
  byId('status').textContent = labels[run.stage];
  byId('status').className = 'air-label ' + (run.stage === 'completed' ? 'good' : ['failed', 'cancelled', 'dispatch_unknown'].includes(run.stage) ? 'red' : 'warn');
  byId('run-label').textContent = run.run_id ? `RUN ${run.run_id}` : `REQUEST ${run.id.slice(0, 8)}`;
  byId('execution').textContent = spec?.title || 'Scenario not yet verified';
  byId('scenario-label').textContent = `Synthetic · ${spec?.validator === 'terraform_validate' ? 'Terraform' : spec ? 'Kubernetes' : 'unverified scenario'}`;
  byId('execution-description').textContent = `${spec?.service || 'Unknown service'} · ${run.owned ? 'dashboard-started' : 'read-only import'} · source ${run.head_sha.slice(0, 8) || 'pending'} · no live monitoring`;
  byId('investigation-title').textContent = spec?.label || 'No verified scenario evidence';
  byId('run-failure').hidden = run.stage !== 'failed';
  byId('failure-heading').textContent = run.failure_job ? `${run.failure_job} · ${run.failure_step || 'Job failed'}` : 'Workflow failed';
  byId('failure-detail').textContent = run.failure_reason || run.notice || 'No failure details are available. Inspect the GitHub run before retrying.';
  setLink('failure-link', run.failure_url || run.run_url);
  byId('workspace-message').textContent = run.notice || (run.stage === 'awaiting_review'
    ? 'Inspect the verified proposal, then approve or reject in GitHub. “Approve and deploy” authorizes PR creation only.'
    : 'Progress is polled from GitHub jobs, not streamed from individual agents.');
  const phases = ['queued', 'preparing', 'awaiting_review', 'publishing', 'completed'];
  const phaseNames = ['Queued', 'Generate & validate', 'Human review', 'Publish PR', 'Complete'];
  const position = phases.indexOf(run.stage);
  progress.replaceChildren(...phases.map((phase, index) => {
    const item = document.createElement('li');
    item.className = 'air-step' + (position > index || run.stage === 'completed' ? ' done' : run.stage === phase ? ' active' : '');
    item.append(textElement('span', position > index || run.stage === 'completed' ? '✓' : String(index + 1), 'air-step-circle'), textElement('span', phaseNames[index]));
    if (run.stage === phase) item.setAttribute('aria-current', 'step'); return item;
  }));
  byId('job-step').textContent = run.steps.length ? `Current job step: ${run.steps.join('; ')}` : `Workflow status: ${labels[run.stage]}`;
  const review = run.review;
  byId('artifact').textContent = review?.content || 'A verified proposal will appear after preparation. Missing or expired evidence is not treated as a validation pass.';
  byId('artifact-path').textContent = review?.artifact_path || 'No verified artifact yet';
  byId('validation-status').textContent = review ? '✓ Verified validation passed' : 'Awaiting verified evidence';
  byId('validation-status').className = 'air-label ' + (review ? 'good' : 'warn');
  byId('proposal-summary').textContent = review
    ? `Generated ${review.validation_tool === 'terraform_validate' ? 'Terraform configuration' : 'Deployment'} for the selected synthetic scenario. Full proposed file shown below; no baseline diff or deployment is implied.`
    : 'Preparation must finish and its artifact must pass verification before a proposal is shown.';
  byId('validation-details').replaceChildren(...(review ? [textElement('span', `✓ ${review.validation_tool} passed`), textElement('span', '✓ Artifact checksum verified'), textElement('span', '◇ Not applied')] : []));
  byId('provenance').hidden = !review;
  byId('artifact-hash').textContent = review?.artifact_sha256 || '';
  byId('execution-id').textContent = review?.execution_id || '';
  byId('model-calls').textContent = review ? `${review.model_calls} / 2 (recorded by workflow)` : '';
  byId('evidence-list').replaceChildren(
    evidenceItem('Incident source', 'Fixed synthetic scenario, not a real-time alert. No live metrics, pod logs, or cluster events were collected.'),
    evidenceItem('Requested remediation', spec ? `${spec.description} These are scenario inputs, not observed resource state.` : 'Scenario inputs are not available until verified evidence is loaded.'),
    evidenceItem('Validation evidence', review ? `Verified workflow artifact records a passing ${review.validation_tool} check. Schema validation is not a runtime recovery test.` : 'No verified review artifact is available for this run.'),
    evidenceItem('Source provenance', `Repository: ${run.repository}. Source commit: ${run.head_sha || 'pending'}. ${run.owned ? 'Started through this dashboard.' : 'Imported read-only; cancellation is disabled.'}`),
  );
  const events = [];
  if (run.owned) events.push(activityItem('Run requested', 'Saved in local history before dispatch.', run.created_at));
  if (run.started_at) events.push(activityItem('GitHub run started', 'Preparation runs Gemini and validators on the remote runner.', run.started_at));
  if (review) events.push(activityItem('Verified proposal ready for review', `${review.validation_tool} passed; alert-age timer stopped at the native review request.`, review.requested_at));
  if (run.receipt) events.push(activityItem('Verified publication receipt', `PR #${run.receipt.pull_request.number} created after review by ${run.receipt.reviewer}. Zero additional model calls for publication.`));
  if (run.finished_at) events.push(activityItem(labels[run.stage], 'GitHub workflow completion time; not an infrastructure recovery time.', run.finished_at));
  if (!run.owned) events.push(activityItem('Run imported read-only', 'Saved in local history. Viewing history does not call a model.', run.created_at));
  if (run.notice) events.push(activityItem('Run notice', run.notice));
  byId('activity-list').replaceChildren(...events);
  byId('decision-title').textContent = run.receipt ? 'Human-reviewed PR created' : run.stage === 'awaiting_review' ? 'Ready for human review' : 'Human-reviewed changes only';
  byId('decision-note').textContent = run.receipt ? 'Review the PR independently before any merge or deployment.' : 'Approval and rejection take place in GitHub, not in this dashboard.';
  byId('github-review').textContent = run.stage === 'awaiting_review' ? 'Review in GitHub ↗' : 'Open GitHub run ↗';
  byId('publication').hidden = !run.receipt && run.stage !== 'completed';
  byId('publication-title').textContent = run.receipt ? `Remediation PR #${run.receipt.pull_request.number}` : 'Publication evidence unavailable';
  byId('result').textContent = run.receipt
    ? `Real PR #${run.receipt.pull_request.number} created after review by ${run.receipt.reviewer}. Publication used 0 additional model calls. No merge or deployment performed.`
    : run.stage === 'completed' ? 'GitHub reports success. A verified publication receipt has not been loaded.' : '';
  setLink('github-review', run.run_url);
  setLink('proposal-download', run.review ? `/api/actions/runs/${run.id}/proposal` : null);
  byId('proposal-download').textContent = review?.validation_tool === 'terraform_validate' ? 'Download Terraform' : 'Download YAML';
  setLink('receipt-download', run.receipt ? `/api/actions/runs/${run.id}/receipt` : null);
  setLink('published-pr', run.receipt?.pull_request.url);
  refreshButton.disabled = false; tickTimers();
}
function remember(run) {
  view.runs.set(run.id, run); renderQueue(); if (view.selected === run.id) render(run);
}
function select(identifier) {
  clearTimeout(view.timer); view.selected = identifier; view.failures = 0;
  byId('execution-error').textContent = ''; renderQueue(); render(view.runs.get(identifier)); poll(true);
}
async function poll(manual = false) {
  clearTimeout(view.timer); const identifier = view.selected; if (!identifier) return;
  try {
    const run = await request(`/api/actions/runs/${identifier}`); remember(run); view.failures = 0;
    if (view.selected === identifier) byId('execution-error').textContent = '';
  } catch (error) {
    view.failures += 1; if (view.selected === identifier) byId('execution-error').textContent = error.message;
  }
  const run = view.runs.get(identifier);
  if (view.selected === identifier && run?.run_id && !finished.has(run.stage)) {
    view.timer = setTimeout(poll, Math.min(60000, 5000 * (2 ** Math.min(view.failures, 4))));
  }
}
byId('open-simulator').addEventListener('click', () => {
  resetConsent();
  renderReadiness();
  byId('simulator-error').textContent = ''; simulator.showModal();
});
for (const id of ['close-simulator', 'cancel-simulator']) byId(id).addEventListener('click', () => simulator.close());
byId('incident-form').addEventListener('submit', async (event) => {
  event.preventDefault(); if (!consent.checked || !view.ready || view.busy || !selectedScenario()) return;
  const scenario = selectedScenario().id;
  view.busy = true; byId('submit-incident').disabled = true; byId('type').disabled = true;
  try {
    const saved = localStorage.getItem('air-actions-pending-request');
    let pending;
    try { pending = saved ? JSON.parse(saved) : null; }
    catch { pending = { request_id: saved, scenario: 'kubernetes_oom_killed' }; }
    if (pending && pending.scenario !== scenario) throw new Error('An unresolved request belongs to another scenario. Inspect that run before starting a different case.');
    const identifier = pending?.request_id || crypto.randomUUID();
    localStorage.setItem('air-actions-pending-request', JSON.stringify({ request_id: identifier, scenario }));
    const run = await request('/api/actions/runs', { method: 'POST', body: JSON.stringify({ request_id: identifier, confirm_live_calls: true, scenario }) });
    localStorage.removeItem('air-actions-pending-request'); remember(run); simulator.close(); select(run.id);
  } catch (error) {
    byId('simulator-error').textContent = `${error.message} The same request ID is retained; there is no automatic dispatch retry.`;
  } finally { view.busy = false; renderQueue(); byId('type').disabled = false; resetConsent(); }
});
importForm.addEventListener('submit', async (event) => {
  event.preventDefault(); const runId = Number(importInput.value.trim());
  if (!Number.isSafeInteger(runId) || runId < 1 || view.busy) return;
  view.busy = true; importButton.disabled = true; byId('queue-error').textContent = '';
  try { const run = await request('/api/actions/import', { method: 'POST', body: JSON.stringify({ run_id: runId }) }); remember(run); select(run.id); }
  catch (error) { byId('queue-error').textContent = error.message; }
  finally { view.busy = false; importButton.disabled = false; renderQueue(); }
});

async function initialize() {
  try {
    const session = await request('/api/actions/session'); view.csrf = session.csrf;
    const scenarios = await request('/api/actions/scenarios');
    for (const spec of scenarios) view.scenarios.set(spec.id, spec);
    byId('type').replaceChildren(...scenarios.map((spec) => new Option(spec.label, spec.id)));
    byId('type').disabled = false; updateScenario();
    byId('runtime-mode').textContent = `ACTIONS · ${session.repository} · Live Gemini + validators on GitHub · Connection details`;
    const runs = await request('/api/actions/runs'); for (const run of runs) view.runs.set(run.id, run);
    renderQueue(); if (runs.length) select(runs[0].id);
    try {
      const status = await request('/api/actions/readiness'); view.ready = status.ready;
      readiness.textContent = `${status.message}. Provider billing/quota and token write permission are not verified by this read-only check.`;
    } catch (error) { readiness.textContent = `Starting is disabled: ${error.message} Existing runs can still be inspected.`; }
    renderQueue();
  } catch (error) { byId('runtime-mode').textContent = 'Actions session unavailable'; readiness.textContent = error.message; }
  finally { renderReadiness(); }
}
setInterval(tickTimers, 1000);
initialize();
