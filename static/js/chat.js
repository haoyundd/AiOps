import { api } from './api.js';

export function initChat(showToast) {
  const form = document.querySelector('#chatForm'); const log = document.querySelector('#chatLog');
  form.addEventListener('submit', async (event) => {
    event.preventDefault(); const field = document.querySelector('#chatMessage'); const message = field.value.trim(); if (!message) return;
    log.insertAdjacentHTML('beforeend', `<p class="user-message"></p>`); log.lastElementChild.textContent = message; field.value = '';
    try { const response = await api('/chat', { method: 'POST', body: JSON.stringify({ message, incident_id: document.querySelector('#chatIncident').value || null }) }); log.insertAdjacentHTML('beforeend', '<p class="assistant-message"></p>'); log.lastElementChild.textContent = response.message; }
    catch (error) { showToast(error.message); }
    log.scrollTop = log.scrollHeight;
  });
}
