const API_BASE = '/api/v1';

export function getToken() { return localStorage.getItem('aiops_token') || ''; }
export function setToken(token) { token ? localStorage.setItem('aiops_token', token) : localStorage.removeItem('aiops_token'); }

export async function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  if (options.body && !(options.body instanceof FormData)) headers.set('Content-Type', 'application/json');
  if (getToken()) headers.set('Authorization', `Bearer ${getToken()}`);
  const response = await fetch(`${API_BASE}${path}`, { ...options, headers });
  if (response.status === 401) window.dispatchEvent(new CustomEvent('auth:expired'));
  const payload = response.status === 204 ? null : await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload?.detail || `请求失败 (${response.status})`);
  return payload;
}

export async function streamSSE(path, onEvent, signal) {
  let lastEventId = 0;
  const headers = { Accept: 'text/event-stream', Authorization: `Bearer ${getToken()}` };
  if (lastEventId) headers['Last-Event-ID'] = String(lastEventId);
  const response = await fetch(`${API_BASE}${path}`, { headers, signal });
  if (!response.ok || !response.body) throw new Error(`事件流连接失败 (${response.status})`);
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const blocks = buffer.split('\n\n');
    buffer = blocks.pop() || '';
    for (const block of blocks) {
      const event = { event: 'message', data: '', id: '' };
      block.split('\n').forEach((line) => {
        const split = line.indexOf(':');
        if (split < 0) return;
        const key = line.slice(0, split); const data = line.slice(split + 1).trimStart();
        if (key === 'data') event.data += data;
        else if (key === 'event') event.event = data;
        else if (key === 'id') event.id = data;
      });
      if (event.id) lastEventId = Number(event.id);
      onEvent(event.event, event.data ? JSON.parse(event.data) : {});
    }
  }
}
