'use strict';

const byId = (id) => document.getElementById(id);
const view = { runs: new Map(), scenarios: new Map(), diffs: new Map(), diffLoading: new Set(), section: 'incidents', selected: null, csrf: '', ready: false, ollamaEnabled: false, ollamaSupported: false, readinessMessage: 'Checking availability…', busy: false, timer: null, failures: 0, evidenceChecks: 0 };
const finished = new Set(['completed', 'failed', 'cancelled', 'rejected']);
const simulator = byId('simulator');
const labels = {
  dispatching: 'Sending request', dispatch_unknown: 'Dispatch outcome uncertain', queued: 'Queued',
  local_generating: 'Generating locally · Ollama',
  preparing: 'Generating and validating', awaiting_review: 'Awaiting GitHub review',
  publishing: 'Publishing approved PR', completed: 'Workflow succeeded', failed: 'Workflow failed',
  cancelled: 'Workflow cancelled', rejected: 'Rejected by reviewer',
};

function setLink(id, url) {
  const element = byId(id); element.hidden = !url;
  if (url) element.href = url; else element.removeAttribute('href');
}

function selectedScenario() { return view.scenarios.get(byId('type').value); }
function scenarioFor(run) { return view.scenarios.get(run.review?.scenario || run.scenario); }
function updateScenario() {
  const spec = selectedScenario(); if (!spec) return;
  byId('service').value = spec.service; byId('title').value = spec.title;
  renderReadiness();
}
byId('type').addEventListener('change', updateScenario);
const refreshButton = byId('refresh-run');
refreshButton.addEventListener('click', () => poll(true));
const progress = byId('actions-progress');
function renderReadiness() {
  const active = [...view.runs.values()].some((run) => run.owned && !finished.has(run.stage));
  const local = byId('generation-mode').value === 'ollama_local';
  const localReady = !local || (view.ollamaEnabled && view.ollamaSupported);
  byId('ollama-settings').hidden = !local;
  byId('simulator-readiness').textContent = !localReady ? 'Experimental Ollama handoff is not published on GitHub main yet. Publish the reviewed workflow before testing.' : view.ready
    ? active ? 'An incident is already in progress. Finish or cancel it before starting another.' : '' : view.readinessMessage;
  byId('simulator-readiness').hidden = view.ready && localReady && !active;
  byId('submit-incident').disabled = !view.ready || !localReady || !selectedScenario() || view.busy || active;
}
byId('generation-mode').addEventListener('change', renderReadiness);
function applyTheme(theme) {
  document.documentElement.dataset.appearance = theme;
  const dark = theme === 'dark';
  for (const [id, hidden] of [['theme-moon', dark], ['theme-sun', !dark]]) {
    if (hidden) byId(id).setAttribute('hidden', ''); else byId(id).removeAttribute('hidden');
  }
  byId('theme-toggle').setAttribute('aria-label', `Switch to ${dark ? 'light' : 'dark'} theme`);
  byId('theme-toggle').setAttribute('title', `Switch to ${dark ? 'light' : 'dark'} theme`);
}
let savedTheme;
try { savedTheme = localStorage.getItem('air-appearance'); } catch { /* Theme still works without storage. */ }
applyTheme(savedTheme === 'dark' ? 'dark' : 'light');
byId('theme-toggle').addEventListener('click', () => {
  const theme = document.documentElement.dataset.appearance === 'dark' ? 'light' : 'dark';
  applyTheme(theme);
  try { localStorage.setItem('air-appearance', theme); } catch { /* Optional preference only. */ }
});
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
function visibleRuns() {
  return [...view.runs.values()].filter((run) => view.section !== 'proposals' || (run.stage === 'awaiting_review' && run.review && !run.receipt && !run.review_rejection));
}
function navigate(section) {
  view.section = section;
  const proposals = section === 'proposals';
  for (const name of ['incidents', 'proposals']) {
    byId(`nav-${name}`).className = 'air-nav-item' + (name === section ? ' is-current' : '');
    if (name === section) byId(`nav-${name}`).setAttribute('aria-current', 'page');
    else byId(`nav-${name}`).removeAttribute('aria-current');
  }
  byId('breadcrumb').textContent = proposals ? '/ Proposals' : '/ Incidents';
  byId('page-heading').textContent = proposals ? 'Proposal workspace' : 'Incident workspace';
  byId('page-description').textContent = proposals ? 'Validated changes awaiting human review.' : 'From an infrastructure alert to a reviewed change.';
  byId('queue-heading').textContent = proposals ? 'Proposals' : 'Incident queue';
  byId('queue-empty').textContent = proposals ? 'No proposals awaiting review.' : 'No incidents yet. Simulate an incident to get started.';
  const runs = visibleRuns();
  clearTimeout(view.timer);
  if (!runs.some((run) => run.id === view.selected)) view.selected = runs[0]?.id || null;
  byId('workspace-empty').hidden = !!runs.length || !proposals;
  byId('execution-detail').hidden = !runs.length && proposals;
  renderQueue(); showTab(proposals ? 'proposal' : 'investigation');
  if (view.selected) { select(view.selected); byId('execution').focus(); }
}
byId('nav-incidents').addEventListener('click', () => navigate('incidents'));
byId('nav-proposals').addEventListener('click', () => navigate('proposals'));
byId('nav-policy').addEventListener('click', () => byId('policy').showModal());
byId('footer-policy').addEventListener('click', () => byId('policy').showModal());
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

function renderProposal(run) {
  const review = run.review;
  byId('code-view').className = 'air-diff' + (review ? ' is-comparison' : '');
  byId('diff-count').textContent = ''; byId('diff-source').textContent = '';
  if (!review) {
    byId('artifact').textContent = 'A verified proposal will appear after preparation. Missing or expired evidence is not treated as a validation pass.';
    return;
  }
  const key = `${run.id}:${review.artifact_sha256}`;
  const comparison = view.diffs.get(key);
  if (comparison) {
    byId('diff-source').textContent = 'Compared with reference baseline · exact proposed file available to download.';
    byId('comparison-source').textContent = comparison.baseline_label;
    byId('diff-count').textContent = `+${comparison.additions} added · −${comparison.deletions} removed`;
    byId('artifact').replaceChildren(...comparison.lines.filter((line) => !(line.kind === 'header' && line.text.startsWith('@@'))).map((line) => {
      const row = document.createElement('span'); row.className = `diff-line ${line.kind}`;
      const marker = line.kind === 'added' ? '+' : line.kind === 'removed' ? '-' : line.kind === 'context' ? ' ' : '';
      row.append(textElement('span', marker, 'diff-marker'), textElement('span', line.text, 'diff-text'));
      return row;
    }));
    if (!comparison.additions && !comparison.deletions) {
      byId('code-view').className = 'air-diff';
      byId('artifact').textContent = review.content;
      byId('diff-source').textContent += ' · No differences. Showing the exact proposed file.';
    }
    return;
  }
  byId('artifact').textContent = 'Loading comparison…';
  byId('diff-source').textContent = 'Comparing with reference baseline…';
  if (view.diffLoading.has(key)) return;
  view.diffLoading.add(key);
  request(`/api/actions/runs/${run.id}/diff`).then((result) => {
    if (result.artifact_sha256 !== review.artifact_sha256 || result.baseline_source !== 'synthetic_reference') throw new Error('Comparison does not match the verified proposal.');
    view.diffs.set(key, result);
    const current = view.runs.get(view.selected);
    if (current?.id === run.id && current.review?.artifact_sha256 === review.artifact_sha256) renderProposal(current);
  }).catch(() => {
    const current = view.runs.get(view.selected);
    if (current?.id === run.id && current.review?.artifact_sha256 === review.artifact_sha256) {
      byId('code-view').className = 'air-diff';
      byId('artifact').textContent = review.content;
      byId('diff-source').textContent = 'Comparison unavailable. Showing the exact proposed file without highlights; no comparison is claimed.';
    }
  }).finally(() => view.diffLoading.delete(key));
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
  return { text, note: run.review ? ' · stopped at review' : stop ? ' · stopped' : ' · in progress' };
}
function tickTimers() {
  document.querySelectorAll('[data-run-timer]').forEach((element) => {
    const run = view.runs.get(element.dataset.runTimer); if (!run) return;
    const elapsed = age(run); element.textContent = elapsed.text; element.setAttribute('title', elapsed.note.slice(3));
  });
  const run = view.runs.get(view.selected);
  if (run) { const elapsed = age(run); byId('elapsed-label').textContent = run.review ? 'Alert age' : 'Run elapsed'; byId('elapsed').textContent = elapsed.text; byId('elapsed-note').textContent = elapsed.note; }
}
function incidentAlias(run) {
  const key = run.id.replaceAll('-', '').toUpperCase();
  let length = 6;
  while (length < key.length && [...view.runs.values()].some((other) => other.id !== run.id && other.id.replaceAll('-', '').toUpperCase().slice(0, length) === key.slice(0, length))) length += 2;
  return `INC-${key.slice(0, length)}`;
}
function renderQueue() {
  const focused = document.activeElement;
  const focusedRun = focused?.dataset.run;
  const focusedAction = focused?.dataset.action;
  byId('queue-empty').hidden = visibleRuns().length > 0;
  const items = visibleRuns().sort((a, b) => Date.parse(b.created_at) - Date.parse(a.created_at)).map((run) => {
    const item = document.createElement('li'); item.className = 'air-queue-item';
    const button = document.createElement('button'); button.type = 'button'; button.className = 'air-incident';
    button.dataset.run = run.id; button.dataset.action = 'select';
    button.setAttribute('aria-pressed', String(run.id === view.selected));
    const meta = textElement('span', incidentAlias(run), 'air-incident-top');
    const spec = scenarioFor(run);
    const title = textElement('span', spec?.title || 'Scenario not yet verified', 'air-incident-title');
    const detail = textElement('span', `${spec?.service || 'Unknown service'} · ${spec?.validator === 'terraform_validate' ? 'Terraform' : spec ? 'Kubernetes' : 'Pending'}`, 'air-incident-service');
    const stage = textElement('span', labels[run.stage], 'air-queue-stage' + (['failed', 'rejected'].includes(run.stage) ? ' red' : run.stage === 'completed' ? ' good' : ''));
    const timer = document.createElement('time'); timer.className = 'air-timer'; timer.dataset.runTimer = run.id;
    const state = document.createElement('span'); state.className = 'air-incident-state'; state.append(stage, timer);
    button.append(meta, title, detail, state); button.addEventListener('click', () => select(run.id)); item.append(button);
    if (run.owned && (run.run_id || run.stage === 'local_generating') && !finished.has(run.stage)) {
      const cancel = document.createElement('button'); cancel.type = 'button'; cancel.className = 'air-button reject';
      cancel.dataset.run = run.id; cancel.dataset.action = 'cancel';
      cancel.textContent = run.stage === 'local_generating' ? 'Cancel generation' : 'Cancel workflow'; cancel.disabled = view.busy;
      cancel.addEventListener('click', async () => {
        if (!window.confirm(run.stage === 'local_generating' ? 'Cancel this local incident? The in-flight model request may finish, but its output will not be sent to GitHub.' : 'Cancel this GitHub run? In-flight operations may finish, and an existing PR will NOT be undone.')) return;
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
  const rejected = run.stage === 'rejected';
  const rejectionDetail = rejected
    ? `${run.review_rejection.reviewer} rejected this proposal in GitHub. Publication was stopped at human review; no remediation PR was created by this run. This is a review decision, not a technical failure.` : '';
  byId('status').textContent = labels[run.stage];
  const checked = run.last_synced_at ? `Last GitHub check: ${new Date(run.last_synced_at).toLocaleString()}.` : 'No recent GitHub check recorded.';
  byId('sync-status').textContent = run.sync_error ? `Cached status. ${checked} ${run.sync_error}` : checked;
  byId('status').className = 'air-label ' + (run.stage === 'completed' ? 'good' : ['failed', 'cancelled', 'dispatch_unknown', 'rejected'].includes(run.stage) ? 'red' : 'warn');
  byId('run-label').textContent = incidentAlias(run);
  byId('execution').textContent = spec?.title || 'Scenario not yet verified';
  byId('scenario-label').textContent = spec?.validator === 'terraform_validate' ? 'Terraform' : spec ? 'Kubernetes' : 'Pending classification';
  byId('execution-description').textContent = `${spec?.service || 'Unknown service'} · ${run.repository}${run.generation_mode === 'ollama_local' ? ' · Ollama · experimental' : ''}`;
  byId('investigation-title').textContent = spec?.label || 'No verified scenario evidence';
  byId('run-failure').hidden = run.stage !== 'failed';
  byId('failure-heading').textContent = run.failure_job ? `${run.failure_job} · ${run.failure_step || 'Job failed'}` : 'Workflow failed';
  byId('failure-detail').textContent = run.failure_reason || run.notice || 'No failure details are available. Inspect the GitHub run before retrying.';
  setLink('failure-link', run.failure_url || run.run_url);
  byId('run-rejection').hidden = !rejected;
  byId('rejection-detail').textContent = rejectionDetail;
  setLink('rejection-link', rejected ? run.run_url : null);
  byId('workspace-message').textContent = run.notice || (run.stage === 'awaiting_review'
    ? run.review ? 'Inspect the verified proposal, then approve or reject in GitHub. “Approve and deploy” authorizes PR creation only.' : 'GitHub is waiting for human review, but proposal evidence is not yet verified here. Inspect the original artifact in GitHub before approving.'
    : rejected ? 'Review complete · proposal retained for inspection · publication not authorized.'
    : 'Progress is polled from GitHub jobs, not streamed from individual agents.');
  const phases = ['queued', 'preparing', 'awaiting_review', 'publishing', 'completed'];
  const phaseNames = ['Queued', 'Generate & validate', 'Human review', 'Publish PR', 'Complete'];
  const position = rejected ? 2 : run.stage === 'local_generating' ? 1 : phases.indexOf(run.stage);
  const failurePosition = run.stage === 'failed' ? phases.indexOf(run.failure_phase) : -1;
  progress.replaceChildren(...phases.map((phase, index) => {
    const item = document.createElement('li');
    const passed = position > index || run.stage === 'completed' || (run.stage === 'failed' && (run.completed_phases || []).includes(phase));
    const failedHere = failurePosition === index;
    const notRun = (rejected && index > 2) || (failurePosition >= 0 && index > failurePosition);
    const activePhase = run.stage === phase || (run.stage === 'local_generating' && index === 1);
    item.className = 'air-step' + (failedHere ? ' failed' : passed ? ' done' : rejected && index === 2 ? ' rejected' : notRun ? ' not-run' : activePhase ? ' active' : '');
    item.append(textElement('span', passed ? '✓' : rejected && index === 2 ? '×' : String(index + 1), 'air-step-circle'), textElement('span', failedHere ? `${phaseNames[index]} · failed` : rejected && index === 2 ? 'Review rejected' : notRun ? `${phaseNames[index]} · not run` : phaseNames[index]));
    if (activePhase) item.setAttribute('aria-current', 'step'); return item;
  }));
  byId('job-step').textContent = run.steps.length ? `Current job step: ${run.steps.join('; ')}` : `Workflow status: ${labels[run.stage]}`;
  const review = run.review;
  renderProposal(run);
  byId('artifact-path').textContent = review?.artifact_path || 'No verified artifact yet';
  byId('validation-status').textContent = review ? '✓ Verified validation passed' : 'Awaiting verified evidence';
  byId('validation-status').className = 'air-label ' + (review ? 'good' : 'warn');
  byId('proposal-summary').textContent = review
    ? `${review.validation_tool === 'terraform_validate' ? 'Terraform configuration' : 'Deployment configuration'} validated with ${review.validation_tool}. Review the highlighted changes before approving publication.`
    : ['awaiting_review', 'publishing', 'completed', 'rejected'].includes(run.stage)
      ? 'GitHub has finished preparation, but the proposal file has not been loaded and verified. Read-only evidence checks will continue unless the evidence is invalid or expired.'
      : 'Preparation must finish and its artifact must pass verification before a proposal is shown.';
  byId('validation-details').replaceChildren(...(review ? [textElement('span', `✓ ${review.validation_tool} passed`), textElement('span', '✓ Artifact checksum verified'), textElement('span', '◇ Not applied')] : []));
  byId('validation-counts').textContent = review
    ? review.validation_attempts != null && review.validation_passes != null
      ? `${review.validation_attempts} validation attempt${review.validation_attempts === 1 ? '' : 's'} · ${review.retry_count == null ? 'Retries not recorded' : `${review.retry_count} retr${review.retry_count === 1 ? 'y' : 'ies'}`} · ${review.validation_passes} passed`
      : 'Validation attempt and retry counts were not recorded in this saved evidence.'
    : 'Validation counts will appear after verified evidence is available.';
  byId('provenance').hidden = !review;
  byId('artifact-hash').textContent = review?.artifact_sha256 || '';
  byId('execution-id').textContent = review?.execution_id || '';
  byId('github-run-id').textContent = run.run_id ? `${run.run_id} · source ${run.head_sha}` : 'Pending dispatch';
  byId('model-calls').textContent = review ? review.generation_mode === 'ollama_local' ? 'Local proposal · 0 hosted inference calls on GitHub' : `${review.model_calls} / 2 (recorded by workflow)` : '';
  byId('evidence-list').replaceChildren(
    evidenceItem('Incident source', 'Simulator request. See approval policy for execution scope and data sources.'),
    evidenceItem('Requested remediation', spec ? spec.description : 'Incident details are not available until verified evidence is loaded.'),
    evidenceItem('Validation evidence', review ? `Verified workflow artifact records a passing ${review.validation_tool} check. Schema validation is not a runtime recovery test.` : 'No verified review artifact is available for this run.'),
    evidenceItem('Source provenance', `Repository: ${run.repository}. Source commit: ${run.head_sha || 'pending'}. ${run.owned ? 'Started through this dashboard.' : 'Imported read-only; cancellation is disabled.'}`),
  );
  const events = [];
  if (run.owned) events.push(activityItem('Run requested', 'Saved in local history before dispatch.', run.created_at));
  if (run.started_at) events.push(activityItem('GitHub run started', run.generation_mode === 'ollama_local' ? 'Local Ollama output is validated on GitHub without hosted inference.' : 'Preparation runs Gemini and validators on the remote runner.', run.started_at));
  if (review) events.push(activityItem('Verified proposal ready for review', `${review.validation_tool} passed; alert-age timer stopped at the native review request.`, review.requested_at));
  if (run.receipt) events.push(activityItem('Verified publication receipt', `PR #${run.receipt.pull_request.number} created after review by ${run.receipt.reviewer}. Zero additional model calls for publication.`));
  if (run.finished_at) events.push(activityItem(labels[run.stage], rejected ? `${rejectionDetail} Timestamp is workflow completion, not the exact review-decision time.` : 'GitHub workflow completion time; not an infrastructure recovery time.', run.finished_at));
  if (!run.owned) events.push(activityItem('Run imported read-only', 'Saved in local history. Viewing history does not call a model.', run.created_at));
  if (run.notice) events.push(activityItem('Run notice', run.notice));
  byId('activity-list').replaceChildren(...events);
  byId('verdict-status').textContent = rejected ? 'Rejected' : run.receipt ? 'Approved · PR created' : run.stage === 'awaiting_review' ? 'Awaiting review' : run.stage === 'failed' ? 'Failed' : run.stage === 'cancelled' ? 'Cancelled' : run.stage === 'completed' ? 'Publication unverified' : 'WIP';
  byId('verdict-status').className = 'air-label ' + (run.receipt ? 'good' : ['failed', 'rejected'].includes(run.stage) ? 'red' : 'warn');
  byId('decision-title').textContent = run.stage === 'failed' ? (run.failure_origin === 'local' ? 'Local generation or handoff failed' : run.run_id ? 'Execution failed on GitHub' : 'Could not start GitHub execution') : rejected ? 'Proposal rejected · publication blocked' : run.receipt ? 'Human-reviewed PR created' : run.stage === 'awaiting_review' ? 'Ready for human review' : 'Human-reviewed changes only';
  if (run.stage === 'awaiting_review' && !review) byId('decision-title').textContent = 'Proposal evidence unavailable';
  byId('decision-note').textContent = run.stage === 'failed' ? (run.failure_reason || run.notice || 'GitHub execution did not complete. Inspect the failed job; the underlying cause is unavailable.') : rejected ? 'The safety gate worked: nothing was published, merged or deployed by this run.' : run.receipt ? 'Review the PR independently before any merge or deployment.' : 'Approval and rejection take place in GitHub, not in this dashboard.';
  byId('github-review').textContent = run.stage === 'awaiting_review' ? 'Review in GitHub ↗' : 'Open GitHub run ↗';
  if (run.stage === 'awaiting_review' && !review) byId('github-review').textContent = 'Inspect GitHub evidence ↗';
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
  view.runs.set(run.id, run);
  if (view.section === 'proposals' && view.selected === run.id && !visibleRuns().some((item) => item.id === run.id)) { navigate('proposals'); return; }
  renderQueue(); if (view.selected === run.id) render(run);
}
function select(identifier) {
  clearTimeout(view.timer); view.selected = identifier; view.failures = 0; view.evidenceChecks = 0;
  byId('execution-error').textContent = ''; renderQueue(); render(view.runs.get(identifier)); poll(true);
}
function needsEvidence(run) {
  if (!run?.run_id || ['invalid', 'expired'].includes(run.evidence_status)) return false;
  if (run.stage === 'completed') return !run.review || !run.receipt;
  if (['awaiting_review', 'publishing', 'rejected'].includes(run.stage)) return !run.review;
  return run.evidence_status === 'unavailable';
}
async function poll(manual = false) {
  clearTimeout(view.timer); const identifier = view.selected; if (!identifier) return;
  if (manual) view.evidenceChecks = 0;
  try {
    const run = await request(`/api/actions/runs/${identifier}`); remember(run);
    if (view.selected !== identifier) return;
    view.failures = run.sync_error || run.evidence_status === 'unavailable' ? view.failures + 1 : 0;
    byId('execution-error').textContent = run.sync_error || '';
  } catch (error) {
    if (view.selected !== identifier) return;
    view.failures += 1;
    byId('execution-error').textContent = `${error.message} Displayed status is cached. Only read-only updates will be checked again.`;
    byId('sync-status').textContent = 'Cached status. The latest GitHub check did not complete.';
  }
  const run = view.runs.get(identifier);
  const active = run && !finished.has(run.stage) && (run.run_id || run.stage === 'local_generating' || run.stage === 'dispatching');
  const evidencePending = needsEvidence(run) || !!run?.sync_error || view.failures > 0;
  if (!active && evidencePending) view.evidenceChecks += 1;
  if (view.selected === identifier && (active || (evidencePending && view.evidenceChecks <= 10))) {
    view.timer = setTimeout(() => poll(false), Math.min(60000, 5000 * (2 ** Math.min(view.failures, 4))));
  } else if (evidencePending && view.evidenceChecks > 10) {
    byId('execution-error').textContent = 'Automatic read-only checks paused after repeated attempts. Select Refresh to check again. No workflow was restarted.';
  }
}
byId('open-simulator').addEventListener('click', () => {
  renderReadiness();
  byId('simulator-error').textContent = ''; simulator.showModal();
});
for (const id of ['close-simulator', 'cancel-simulator']) byId(id).addEventListener('click', () => simulator.close());
byId('incident-form').addEventListener('submit', async (event) => {
  event.preventDefault(); renderReadiness(); if (byId('submit-incident').disabled) return;
  const scenario = selectedScenario().id;
  const mode = byId('generation-mode').value;
  const outputTokens = Number(byId('ollama-output-tokens').value || 8192);
  const timeoutSeconds = Number(byId('ollama-timeout').value || 600);
  view.busy = true; byId('submit-incident').disabled = true; byId('type').disabled = true;
  try {
    const saved = localStorage.getItem('air-actions-pending-request');
    let pending;
    try { pending = saved ? JSON.parse(saved) : null; }
    catch { pending = { request_id: saved, scenario: 'kubernetes_oom_killed' }; }
    if (pending && pending.scenario !== scenario) throw new Error('An unresolved request belongs to another scenario. Inspect that run before starting a different case.');
    if (pending && ((pending.mode || 'gemini') !== mode || (mode === 'ollama_local' && (pending.outputTokens !== outputTokens || pending.timeoutSeconds !== timeoutSeconds)))) throw new Error('An unresolved request belongs to another generation mode or budget. Restore the previous selection before retrying.');
    const identifier = pending?.request_id || crypto.randomUUID();
    localStorage.setItem('air-actions-pending-request', JSON.stringify({ request_id: identifier, scenario, mode, outputTokens, timeoutSeconds }));
    const run = await request('/api/actions/runs', { method: 'POST', body: JSON.stringify({ request_id: identifier, confirm_live_calls: true, scenario, generation_mode: mode, ollama_output_tokens: outputTokens, ollama_timeout_seconds: timeoutSeconds }) });
    localStorage.removeItem('air-actions-pending-request'); navigate('incidents'); remember(run); simulator.close(); select(run.id);
  } catch (error) {
    byId('simulator-error').textContent = `${error.message} The same request ID is retained; there is no automatic dispatch retry.`;
  } finally { view.busy = false; renderQueue(); byId('type').disabled = false; renderReadiness(); }
});

async function initialize() {
  try {
    const session = await request('/api/actions/session'); view.csrf = session.csrf;
    try { const modes = await request('/api/actions/generation-modes'); view.ollamaEnabled = modes.experimental_ollama === true; }
    catch { view.ollamaEnabled = false; } // Older local servers keep the default workflow usable.
    const localOption = byId('generation-mode').querySelector('option[value="ollama_local"]');
    if (localOption) localOption.disabled = !view.ollamaEnabled;
    const scenarios = await request('/api/actions/scenarios');
    for (const spec of scenarios) view.scenarios.set(spec.id, spec);
    byId('type').replaceChildren(...scenarios.map((spec) => new Option(spec.label, spec.id)));
    byId('type').disabled = false; updateScenario();
    const runs = await request('/api/actions/runs'); for (const run of runs) view.runs.set(run.id, run);
    renderQueue(); if (runs.length) select(runs[0].id);
    try {
      const status = await request('/api/actions/readiness'); view.ready = status.ready;
      view.ollamaSupported = status.ollama_supported === true;
      view.readinessMessage = `${status.message}. Provider billing/quota and token write permission are not verified by this read-only check.`;
    } catch (error) { view.readinessMessage = `Starting is disabled: ${error.message} Existing runs can still be inspected.`; }
    renderQueue();
  } catch (error) { view.readinessMessage = error.message; byId('queue-error').textContent = error.message; }
  finally { renderReadiness(); }
}
setInterval(tickTimers, 1000);
initialize();
