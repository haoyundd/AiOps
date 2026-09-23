import { api } from './api.js';
import { initAuth } from './auth.js';
import { initChat } from './chat.js';
import { initIncidentControls, loadIncidents } from './incidents.js';

const toast = document.querySelector('#toast'); let toastTimer;
function showToast(message) { toast.textContent = message; toast.classList.add('is-visible'); clearTimeout(toastTimer); toastTimer = setTimeout(() => toast.classList.remove('is-visible'), 3200); }

const viewCopy = { incidents: ['INCIDENT QUEUE', '真实信号，按证据说话'], chat: ['INCIDENT CHAT', '只围绕已经存在的证据追问'], runbooks: ['OPERATOR MEMORY', '把排障经验写进系统'] };
document.querySelectorAll('.nav-item').forEach((button) => button.addEventListener('click', () => {
  document.querySelectorAll('.nav-item').forEach((item) => item.classList.remove('is-active')); button.classList.add('is-active');
  document.querySelectorAll('.view').forEach((view) => view.classList.remove('is-active')); document.querySelector(`#${button.dataset.view}View`).classList.add('is-active');
  document.querySelector('#viewEyebrow').textContent = viewCopy[button.dataset.view][0]; document.querySelector('#viewTitle').textContent = viewCopy[button.dataset.view][1];
}));

document.querySelector('#runbookForm').addEventListener('submit', async (event) => {
  event.preventDefault(); const error = document.querySelector('#runbookError'); error.textContent = '';
  try { const result = await api('/runbooks', { method: 'POST', body: JSON.stringify({ title: document.querySelector('#runbookTitle').value, service_name: document.querySelector('#runbookService').value, tags: document.querySelector('#runbookTags').value.split(',').map((item) => item.trim()).filter(Boolean), content: document.querySelector('#runbookContent').value }) }); showToast(result.created ? 'Runbook 已写入并完成分块' : '相同内容已经存在'); }
  catch (caught) { error.textContent = caught.message; }
});

// 只把服务端返回的文本放入 HTML 转义函数，避免草稿正文中的用户内容形成 XSS。
// 下一步：审核成功后刷新正式手册检索，证明草稿确实已经进入复用链路。
const escapeHtml = (value = '') => String(value).replace(/[&<>'"]/g, (char) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' }[char]));

let draftStatus = 'PENDING';

async function loadDrafts(status = draftStatus) {
  const list = document.querySelector('#draftList');
  if (!list) return;
  draftStatus = status;
  try {
    const drafts = await api(`/runbook-drafts?status=${encodeURIComponent(status)}`);
    list.innerHTML = drafts.length ? drafts.map((draft) => `<article class="incident-row"><span><b>${escapeHtml(draft.title)}</b><p>${escapeHtml(draft.service_name)} · ${escapeHtml(draft.status)} · ${escapeHtml(draft.reviewed_by || '待审核')}</p><small>${escapeHtml(draft.content.slice(0, 180))}</small>${draft.review_reason ? `<small>审核意见：${escapeHtml(draft.review_reason)}</small>` : ''}</span>${status === 'PENDING' ? `<span><button class="text-button" data-approve="${draft.id}">批准</button><button class="text-button" data-reject="${draft.id}">驳回</button></span>` : ''}</article>`).join('') : `<div class="empty-state">暂无${status === 'PENDING' ? '待审核' : status === 'APPROVED' ? '已批准' : '已驳回'}草稿</div>`;
    list.querySelectorAll('[data-approve]').forEach((button) => button.addEventListener('click', async () => {
      try {
        await api(`/runbook-drafts/${button.dataset.approve}/approve`, { method: 'POST', body: JSON.stringify({ reason: '证据充分且可复现' }) });
        showToast('草稿已批准并写入正式手册');
        await loadDrafts('PENDING');
        await loadFormalRunbooks();
      } catch (error) { showToast(error.message); }
    }));
    list.querySelectorAll('[data-reject]').forEach((button) => button.addEventListener('click', async () => {
      try {
        await api(`/runbook-drafts/${button.dataset.reject}/reject`, { method: 'POST', body: JSON.stringify({ reason: '需要补充证据' }) });
        showToast('草稿已驳回');
        await loadDrafts('PENDING');
      } catch (error) { showToast(error.message); }
    }));
  } catch (error) {
    list.innerHTML = `<div class="empty-state">${escapeHtml(error.message)}</div>`;
  }
}

// 只有正式 Runbook 才通过统一检索接口展示，PENDING/REJECTED 草稿不会混入 Agent 复用结果。
async function loadFormalRunbooks() {
  const results = document.querySelector('#runbookSearchResults');
  if (!results) return;
  const query = document.querySelector('#runbookSearchQuery').value.trim();
  try {
    const rows = await api(`/runbooks?service_name=merchantflow&query=${encodeURIComponent(query)}`);
    results.innerHTML = rows.length ? rows.map((item) => `<article class="incident-row"><span><b>${escapeHtml(item.title)}</b><p>第 ${item.chunk_index + 1} 块 · ${escapeHtml((item.tags || []).join(' / '))}</p><small>${escapeHtml(item.content)}</small></span></article>`).join('') : '<div class="empty-state">没有命中已批准的正式手册。</div>';
  } catch (error) {
    document.querySelector('#runbookSearchError').textContent = error.message;
  }
}

document.querySelector('#refreshDrafts')?.addEventListener('click', () => loadDrafts().catch((error) => showToast(error.message)));
document.querySelectorAll('#draftFilters [data-draft-status]').forEach((button) => button.addEventListener('click', () => {
  document.querySelectorAll('#draftFilters [data-draft-status]').forEach((item) => item.classList.remove('is-active'));
  button.classList.add('is-active');
  loadDrafts(button.dataset.draftStatus).catch((error) => showToast(error.message));
}));
document.querySelector('#runbookSearchForm')?.addEventListener('submit', async (event) => {
  event.preventDefault();
  await loadFormalRunbooks();
});

initIncidentControls(showToast); initChat(showToast);
initAuth(() => { loadIncidents().catch((error) => showToast(error.message)); loadDrafts(); });
