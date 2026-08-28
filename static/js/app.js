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

initIncidentControls(showToast); initChat(showToast);
initAuth(() => loadIncidents().catch((error) => showToast(error.message)));
