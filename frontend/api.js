// ========================================
// Jarvis V3 - API 层
// ========================================

class APIError extends Error {
  constructor(code, message) {
    super(message);
    this.code = code;
    this.name = 'APIError';
  }
}

const API = {
  async _request(url, options = {}) {
    try {
      const resp = await fetch(url, options);
      const ct = resp.headers.get('content-type') || '';
      if (!ct.includes('json')) return resp;
      const data = await resp.json();
      if (!data.success && data.detail) {
        const detail = typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail);
        throw new APIError(resp.status, detail);
      }
      return data;
    } catch (e) {
      if (e instanceof APIError) throw e;
      if (e instanceof TypeError && e.message.includes('fetch')) {
        throw new APIError(0, '网络连接失败，请检查后端服务是否启动');
      }
      throw e;
    }
  },

  async get(url) { return this._request(url); },
  async post(url, body = {}) {
    return this._request(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    });
  },
  async del(url) {
    return this._request(url, { method: 'DELETE' });
  },
  async patch(url, body = {}) {
    return this._request(url, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    });
  },

  // ── 聊天 SSE ──
  createChatStream(message, sessionId, model) {
    const params = new URLSearchParams({ message });
    if (sessionId) params.set('session_id', sessionId);
    if (model) params.set('model', model);

    const controller = new AbortController();
    const url = '/api/chat/stream?' + params.toString();

    const stream = fetch(url, {
      signal: controller.signal
    }).then(async resp => {
      if (!resp.ok) {
        const text = await resp.text().catch(() => '');
        throw new APIError(resp.status, text || `HTTP ${resp.status}`);
      }
      return resp.body.getReader();
    });

    return { stream, controller, url };
  },

  // ── 会话 ──
  async listConversations() { return this.get('/api/sessions'); },
  async createConversation() { return this.post('/api/sessions'); },
  async getConversation(sid) { return this.get(`/api/sessions/${sid}`); },
  async deleteConversation(sid) { return this.del(`/api/sessions/${sid}`); },
  async updateConversation(sid, data) { return this.patch(`/api/sessions/${sid}`, data); },

  // ── API Key ──
  async getApiKeys() { return this.get('/api/config/keys'); },
  async saveApiKey(providerId, apiKey) {
    return this.post('/api/config/keys', { provider_id: providerId, api_key: apiKey });
  },

  // ── 打断 ──
  async interrupt() { return this.post('/api/chat/interrupt'); },

  // ── 状态 ──
  async checkStatus() { return this.get('/api/status'); }
};
