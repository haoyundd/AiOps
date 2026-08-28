import { api, streamSSE } from './api.js';

let incidents = []; let activeId = ''; let activeStream;
const list = document.querySelector('#incidentList'); const detail = document.querySelector('#incidentDetail');
const fmt = (value) => value ? new Intl.DateTimeFormat('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }).format(new Date(value)) : '—';
const escapeHtml = (value = '') => String(value).replace(/[&<>'"]/g, (char) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' }[char]));

export async function loadIncidents(status = '') {
  const query = status ? `?status=${status}` : '';
  const data = await api(`/incidents${query}`); incidents = data.items;
  renderCounts(); renderList(); fillChatOptions();
  if (activeId && incidents.some((item) => item.id === activeId)) await selectIncident(activeId);
}
function renderCounts() {
  const count = (values) => incidents.filter((item) => values.includes(item.status)).length;
  document.querySelector('#receivedCount').textContent = count(['RECEIVED']);
  document.querySelector('#diagnosingCount').textContent = count(['DIAGNOSING']);
  document.querySelector('#diagnosedCount').textContent = count(['DIAGNOSED', 'RESOLVED']);
  document.querySelector('#inconclusiveCount').textContent = count(['INCONCLUSIVE', 'FAILED']);
}
function renderList() {
  if (!incidents.length) { list.innerHTML = '<div class="empty-state"><b>还没有事件</b><span>Alertmanager 发送第一条真实告警后，它会出现在这里。</span></div>'; return; }
  list.innerHTML = incidents.map((item) => `<button class="incident-row ${item.id === activeId ? 'is-active' : ''}" data-id="${item.id}"><i class="severity ${item.severity}"></i><span><b>${escapeHtml(item.alert_name)}</b><p>${escapeHtml(item.service_name)} · ${escapeHtml(item.description || '等待证据')}</p><em class="status-tag">${item.status}</em></span><time>${fmt(item.updated_at)}</time></button>`).join('');
  list.querySelectorAll('[data-id]').forEach((button) => button.addEventListener('click', () => selectIncident(button.dataset.id)));
}
async function selectIncident(id) {
  activeId = id; renderList(); const incident = await api(`/incidents/${id}`);
  const latest = [...incident.diagnoses].sort((a, b) => new Date(b.created_at) - new Date(a.created_at))[0];
  let diagnosis = null; if (latest) diagnosis = await api(`/diagnoses/${latest.id}`);
  renderDetail(incident, diagnosis);
  if (latest?.status === 'QUEUED' || latest?.status === 'RUNNING') watchRun(latest.id);
}
function renderDetail(incident, diagnosis) {
  const hypotheses = diagnosis?.hypotheses || []; const evidence = diagnosis?.evidence || [];
  const events = incident.events || []; const report = diagnosis?.conclusion?.report_markdown || '';
  detail.innerHTML = `<header class="detail-header"><div class="meta"><span>${escapeHtml(incident.service_name)}</span><span>·</span><span>${escapeHtml(incident.environment)}</span><span>·</span><span>${fmt(incident.created_at)}</span></div><h2>${escapeHtml(incident.alert_name)}</h2><p>${escapeHtml(incident.description || '告警没有附带描述')}</p><div class="detail-actions"><button class="primary-button" id="rediagnose">重新调查</button><span class="status-tag">${incident.status}</span></div></header>
  <section class="detail-section"><h3>根因假设</h3>${hypotheses.length ? hypotheses.map((item) => `<div class="hypothesis"><span class="rank">0${item.rank}</span><span><b>${escapeHtml(item.title)}</b><small>${item.category} · ${item.verdict}</small></span><span class="confidence">${Math.round(item.confidence * 100)}%</span></div>`).join('') : '<p class="muted">诊断 Worker 尚未生成假设。</p>'}</section>
  <section class="detail-section"><h3>证据信号</h3><div class="evidence-rail">${evidence.length ? evidence.map((item) => `<article class="evidence-item" data-source="${item.source}"><b>${escapeHtml(item.source)} / ${escapeHtml(item.kind)}</b><p>${escapeHtml(item.summary)}</p></article>`).join('') : '<p>指标、日志和 Trace 证据尚未到达。</p>'}</div></section>
  ${report ? `<section class="detail-section"><h3>调查报告</h3><div class="report">${escapeHtml(report)}</div></section>` : ''}
  <section class="detail-section"><h3>事件时间线</h3>${events.slice().reverse().map((event) => `<div class="timeline-item"><time>${fmt(event.created_at)}</time><span><b>${escapeHtml(event.event_type)}</b><br>${escapeHtml(event.message)}</span></div>`).join('')}</section>`;
  document.querySelector('#rediagnose').addEventListener('click', async () => { const run = await api(`/incidents/${incident.id}/diagnoses`, { method: 'POST', body: '{}' }); watchRun(run.id); await loadIncidents(); });
}
function watchRun(runId) {
  if (activeStream) activeStream.abort(); activeStream = new AbortController();
  streamSSE(`/diagnoses/${runId}/events`, async (event) => { if (event === 'complete' || event.startsWith('diagnosis_')) await loadIncidents(); }, activeStream.signal).catch((error) => { if (error.name !== 'AbortError') console.warn(error); });
}
function fillChatOptions() {
  const select = document.querySelector('#chatIncident'); const selected = select.value;
  select.innerHTML = '<option value="">不关联事件</option>' + incidents.map((item) => `<option value="${item.id}">${escapeHtml(item.service_name)} · ${escapeHtml(item.alert_name)}</option>`).join(''); select.value = selected;
}
export function initIncidentControls(showToast) {
  document.querySelector('#refreshIncidents').addEventListener('click', () => loadIncidents().catch((error) => showToast(error.message)));
  document.querySelectorAll('#incidentFilters button').forEach((button) => button.addEventListener('click', () => { document.querySelectorAll('#incidentFilters button').forEach((item) => item.classList.remove('is-active')); button.classList.add('is-active'); loadIncidents(button.dataset.status).catch((error) => showToast(error.message)); }));
}
