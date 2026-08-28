import { api, setToken, getToken } from './api.js';

export function initAuth(onReady) {
  const gate = document.querySelector('#loginGate'); const shell = document.querySelector('#appShell');
  const form = document.querySelector('#loginForm'); const error = document.querySelector('#loginError');
  async function check() {
    if (!getToken()) return showLogin();
    try { const user = await api('/auth/me'); showApp(user); onReady(user); } catch { showLogin(); }
  }
  function showLogin() { setToken(''); gate.hidden = false; shell.hidden = true; }
  function showApp(user) {
    gate.hidden = true; shell.hidden = false;
    document.querySelector('#currentUser').textContent = user.username;
    document.querySelector('#currentRole').textContent = user.role;
  }
  form.addEventListener('submit', async (event) => {
    event.preventDefault(); error.textContent = '';
    try {
      const result = await api('/auth/login', { method: 'POST', body: JSON.stringify({ username: document.querySelector('#loginUsername').value, password: document.querySelector('#loginPassword').value }) });
      setToken(result.access_token); showApp(result); onReady(result);
    } catch (caught) { error.textContent = caught.message; }
  });
  document.querySelector('#logoutButton').addEventListener('click', showLogin);
  window.addEventListener('auth:expired', showLogin);
  check();
}
