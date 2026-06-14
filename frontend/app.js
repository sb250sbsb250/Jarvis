// ========================================
// Jarvis V3 - Vue 3 主应用
// ========================================

const { createApp, ref, computed, nextTick } = Vue;

const app = createApp({
  directives: { clickOutside: window.__vClickOutside || {} },

  setup() {
    // ── 状态 ──
    const messages = ref([]);
    const inputText = ref('');
    const sending = ref(false);
    const statusText = ref('准备就绪');
    const statusClass = ref('');

    const conversations = ref([]);
    const currentSessionId = ref(null);
    const modelName = ref('deepseek-v4-pro');
    const searchEnabled = ref(false);
    
    function selectModel(model) {
      if (sending.value) return;
      modelName.value = model;
    }

    const showSettings = ref(false);
    const apiProviders = ref([]);

    const inputEl = ref(null);
    const msgContainer = ref(null);
    const interrupting = ref(false);

    // ── Claude Code: Todo + Approval 状态 ──
    const todoItems = ref([]);
    const todoStats = ref({});
    const pendingApprovals = ref([]);  // [{call_id, tool, message, args_preview, risk_level}]

    let _currentMsgIndex = -1;
    let _msgUid = 0;
    let _abortController = null;

    // ── HTML 转义 ──
    function escapeHtml(text) {
      if (!text) return '';
      return text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;').replace(/'/g, '&#039;');
    }

    // ── Markdown 渲染（使用 marked.js）──
    function renderMarkdown(text) {
      if (!text) return '';
      if (typeof marked === 'undefined') {
        // fallback: 纯文本
        return `<p>${escapeHtml(text)}</p>`;
      }

      const renderer = new marked.Renderer();
      renderer.code = ({ text: codeText, lang }) => {
        const langClass = lang ? ` class="language-${escapeHtml(lang)}"` : '';
        const safeCode = btoa(unescape(encodeURIComponent(codeText)));
        return `<pre><button class="copy-btn" data-code="${safeCode}">📋 复制</button><code${langClass}>${escapeHtml(codeText)}</code></pre>`;
      };

      marked.setOptions({
        renderer,
        breaks: true,
        gfm: true,
      });

      return marked.parse(text);
    }

    // ── 构建消息 HTML（不触发响应式）──
    function buildMessageHtml(msg) {
      if (!msg) return '';
      let html = '';

      // 中间步骤（thinking/steps）
      if (msg.steps && msg.steps.length > 0) {
        const allDone = msg.steps.every(s => s.state === 'done' || s.state === 'error');
        if (!allDone) {
          html += '<div class="thinking-text">🧠 Jarvis 正在思考...</div>';
        }
        for (const step of msg.steps) {
          html += `<div class="sse-step">
            <div class="step-header">
              <span class="step-icon">${step.icon}</span>
              <span class="step-tool">${escapeHtml(step.tool)}</span>
              <span style="color:var(--text-muted);font-size:11px;">
                ${step.state === 'running' ? '⏳ 执行中...' : step.state === 'done' ? '✅ 完成' : step.state === 'error' ? '❌ 失败' : ''}
              </span>
            </div>
            ${step.args && Object.keys(step.args).length
              ? `<div class="step-args">${escapeHtml(JSON.stringify(step.args, null, 2))}</div>` : ''}
            ${step.result
              ? `<div class="step-result">${escapeHtml(String(step.result).slice(0, 200))}</div>`
              : ''}
          </div>`;
        }
      }

      // 最终回复
      if (msg.content) {
        html += `<div class="final-reply">${renderMarkdown(msg.content)}</div>`;
      }

      return html;
    }

    // ── 渲染消息 ──
    function renderMessage(msg) {
      if (!msg) return '';
      msg.html = buildMessageHtml(msg);
      
      // ✅ 强制触发 Vue 响应式更新：替换整个 messages 数组引用
      // Vue 3 无法追踪普通对象数组的 push/splice 变更，
      // 但重新赋值 ref.value 能触发重新渲染
      messages.value = [...messages.value];
    }

    // ── 滚动到底部 ──
    let _scrollPending = false;
    function scrollToBottom() {
      if (_scrollPending) return;
      _scrollPending = true;
      requestAnimationFrame(() => {
        const el = msgContainer.value;
        if (el) el.scrollTop = el.scrollHeight;
        _scrollPending = false;
      });
    }

    // ── SSE 事件处理 ──
    function handleSSEEvent(data) {
      // 服务端事件: tool_call, tool_result, tool_error, planning, round_start, done, session, info, chunk
      function getAssistantMsg() {
        for (let i = messages.value.length - 1; i >= 0; i--) {
          if (messages.value[i].role === 'assistant') return messages.value[i];
        }
        return null;
      }

      switch (true) {
        // ── 工具调用/结果/错误 ──
        case data.type === 'tool_call' || data.type === 'tool_result' || data.type === 'tool_error': {
          const msg = getAssistantMsg();
          if (!msg) { console.log('no msg for', data.type); break; }
          const stepName = data.name || data.tool || '';
          const state = data.status || (data.type === 'tool_result' ? 'done' : data.type === 'tool_error' ? 'error' : 'running');
          const icon = state === 'done' ? '✅' : state === 'error' ? '❌' : '⚡';
          msg.steps.push({ tool: stepName, icon, state, args: data.args || {}, result: data.result || '' });
          statusText.value = state === 'done' ? `✅ 完成: ${stepName}` : state === 'error' ? `❌ 失败: ${stepName}` : `🔧 执行: ${stepName}`;
          statusClass.value = state === 'done' ? '' : state === 'error' ? 'error' : 'executing';
          renderMessage(msg);
          scrollToBottom();
          break;
        }

        // ── 规划/轮次 ──
        case data.type === 'planning' || data.type === 'round_start': {
          const text = data.content || (data.type === 'round_start' ? `第 ${data.round || '?'} 轮` : '分析任务中...');
          statusText.value = '🧠 ' + text;
          statusClass.value = 'executing';
          break;
        }

        // ── 完成 ──
        case data.type === 'done': {
          const msg = getAssistantMsg();
          if (msg && data.content) msg.content = data.content;
          statusText.value = '✅ 回答完成';
          statusClass.value = 'done';
          if (msg) renderMessage(msg);
          scrollToBottom();
          loadConversations();
          break;
        }

        // ── 错误 ──
        case data.type === 'error': {
          const msg = getAssistantMsg();
          if (!msg) { statusText.value = '❌ ' + (data.content || '错误'); break; }
          statusText.value = '❌ 错误: ' + (data.content || data.error || '未知错误');
          statusClass.value = 'error';
          msg.steps.push({ tool: '错误', icon: '❌', state: 'error', args: {}, result: data.content || data.error || '未知错误' });
          renderMessage(msg);
          scrollToBottom();
          break;
        }

        // ── info / session / chunk ──
        default: {
          if (data.type === 'info') {
            statusText.value = data.content || '⏳ 处理中...';
            statusClass.value = 'executing';

          } else if (data.type === 'session') {
            currentSessionId.value = data.session_id;
            loadConversations();

          } else if (data.type === 'chunk') {
            const msg = getAssistantMsg();
            if (!msg) break;
            msg.content = (msg.content || '') + (data.content || '');
            renderMessage(msg);
            scrollToBottom();

          } else if (data.type === 'approval') {
            // Claude Code: 审批事件
            if (data.auto_approved) {
              // 自动模式：在 steps 中显示通知条
              const msg = getAssistantMsg();
              if (msg) {
                msg.steps.push({
                  tool: data.tool || '审批',
                  icon: '🔓',
                  state: 'done',
                  args: {},
                  result: data.message || '已自动审批'
                });
                renderMessage(msg);
                scrollToBottom();
              }
            } else {
              // 手动模式：弹出审批按钮
              pendingApprovals.value.push({
                call_id: data.call_id,
                tool: data.tool,
                message: data.message,
                args_preview: data.args_preview,
                risk_level: data.risk_level || 'medium',
              });
              statusText.value = '⏸️ 等待审批: ' + (data.tool || '');
              statusClass.value = 'executing';
            }

          } else if (data.type === 'todo_update') {
            // Claude Code: Todo 更新
            todoItems.value = data.todos || [];
            todoStats.value = data.stats || {};
            // 同时在 steps 中展示
            const msg = getAssistantMsg();
            if (msg) {
              const summary = (data.todos || [])
                .map(t => `${t.status === 'completed' ? '✅' : t.status === 'in_progress' ? '🔄' : t.status === 'cancelled' ? '⛔' : '⬜'} ${t.content}`)
                .join('\n');
              msg.steps.push({
                tool: 'todo_write',
                icon: '📋',
                state: 'done',
                args: {},
                result: summary || '任务列表已更新'
              });
              renderMessage(msg);
              scrollToBottom();
            }

          } else {
            console.log('Unknown SSE event:', data);
          }
        }
      }
    }

    // ── 发送消息 ──
    async function sendMessage() {
      const text = inputText.value.trim();
      if (!text || sending.value) return;

      // 用户消息
      messages.value.push({
        _uid: ++_msgUid,
        role: 'user',
        content: text,
        html: `<p>${escapeHtml(text)}</p>`
      });
      scrollToBottom();

      inputText.value = '';
      sending.value = true;
      interrupting.value = false;
      statusText.value = '🧠 Jarvis 正在思考...';
      statusClass.value = 'executing';

      // 空白助手消息（先 push 再设索引）
      messages.value.push({
        _uid: ++_msgUid,
        role: 'assistant',
        content: '',
        html: '<div class="thinking-text">🧠 Jarvis 正在思考...</div>',
        steps: []
      });
      _currentMsgIndex = messages.value.length - 1;

      await nextTick();

      // SSE 连接
      const { stream, controller } = API.createChatStream(text, currentSessionId.value, modelName.value, searchEnabled.value);
      _abortController = controller;

      try {
        const reader = await stream;
        const decoder = new TextDecoder();
        let buffer = '';

        while (true) {
          let { done: streamDone, value } = await reader.read();
          if (streamDone) break;

          buffer += decoder.decode(value, { stream: true });
          const parts = buffer.split('\n\n');
          buffer = parts.pop() || '';

          let shouldStop = false;
          for (const part of parts) {
            for (const line of part.split('\n')) {
              if (line.startsWith('data: ')) {
                const payload = line.slice(6);
                if (payload === '[DONE]') {
                  console.log('SSE stream done');
                  shouldStop = true;
                  break;
                }
                try {
                  handleSSEEvent(JSON.parse(payload));
                } catch (e) {
                  console.error('SSE parse error:', e);
                }
              }
            }
            if (shouldStop) break;
          }
          if (shouldStop) break;
        }
      } catch (err) {
        if (err.name === 'AbortError') {
          console.log('SSE aborted by user');
          statusText.value = '⏹ 已中断';
          statusClass.value = '';
        } else {
          console.error('SSE error:', err);
          statusText.value = '❌ 连接错误: ' + (err.message || '未知错误');
          statusClass.value = 'error';
        }
      } finally {
        sending.value = false;
        interrupting.value = false;
        _abortController = null;

        const msg = messages.value[_currentMsgIndex];
        if (msg && msg.steps) {
          for (const step of msg.steps) {
            if (step.state === 'running') {
              step.state = 'error';
              step.icon = '❌';
            }
          }
          renderMessage(msg);
        }

        if (statusText.value === '🧠 Jarvis 正在思考...') {
          statusText.value = '准备就绪';
          statusClass.value = '';
        }
      }
    }

    // ── 中断 ──
    async function interruptSession() {
      if (_abortController) {
        _abortController.abort();
        interrupting.value = true;
        try {
          await API.interrupt();
        } catch (e) { /* ignore */ }
      }
    }

    // ── Claude Code: 审批响应 ──
    async function respondApproval(callId, approved) {
      try {
        const resp = await fetch('/api/approval/respond', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            session_id: currentSessionId.value,
            call_id: callId,
            approved: approved,
          }),
        });
        if (!resp.ok) {
          console.error('审批响应失败:', await resp.text());
        }
        // 从待审批列表中移除
        pendingApprovals.value = pendingApprovals.value.filter(a => a.call_id !== callId);
      } catch (e) {
        console.error('审批响应错误:', e);
      }
    }

    // ── 新对话 ──
    async function newConversation() {
      try {
        const data = await API.createConversation();
        currentSessionId.value = data.session_id;
        messages.value = [];
        await loadConversations();
      } catch (e) {
        console.error('New conversation error:', e);
      }
    }

    // ── 切换对话 ──
    async function switchConversation(sid) {
      if (!sid) return;
      if (sending.value) return;
      currentSessionId.value = sid;
      try {
        const data = await API.getConversation(sid);
        const mergedMessages = [];
        let pendingAssistant = null;  // 正在收集 tool 步骤的 assistant 消息

        if (data.messages) {
          for (const m of data.messages) {
            const role = m.role || 'assistant';

            if (role === 'user') {
              // 先收尾前一个 pending assistant（如果有）
              if (pendingAssistant) {
                mergedMessages.push(pendingAssistant);
                pendingAssistant = null;
              }
              mergedMessages.push({
                _uid: ++_msgUid,
                role: 'user',
                content: m.content || '',
                steps: [],
                html: `<p>${escapeHtml(m.content || '')}</p>`
              });

            } else if (role === 'tool') {
              // tool 消息 → 合并到当前 assistant 的 steps 中
              if (!pendingAssistant) {
                pendingAssistant = {
                  _uid: ++_msgUid,
                  role: 'assistant',
                  content: '',
                  steps: [],
                  html: ''
                };
              }
              pendingAssistant.steps.push({
                tool: m.name || m.tool || '工具调用',
                icon: '✅',
                state: 'done',
                args: m.args || {},
                result: (m.content || '').slice(0, 200)
              });

            } else {
              // role === 'assistant'
              if (m.tool_calls && m.tool_calls.length > 0) {
                // 带工具调用的 assistant → 开始新的 pending 消息
                if (pendingAssistant) {
                  mergedMessages.push(pendingAssistant);
                }
                pendingAssistant = {
                  _uid: ++_msgUid,
                  role: 'assistant',
                  content: '',
                  steps: [],
                  html: ''
                };
                for (const tc of m.tool_calls) {
                  const fn = tc.function || {};
                  pendingAssistant.steps.push({
                    tool: fn.name || tc.name || '工具调用',
                    icon: '⚡',
                    state: 'done',
                    args: (() => { try { return JSON.parse(fn.arguments || '{}'); } catch { return {}; } })(),
                    result: ''
                  });
                }
              } else {
                // 最终回答 → 合并到 pending 或作为独立消息
                if (pendingAssistant) {
                  pendingAssistant.content = m.content || '';
                  pendingAssistant.html = buildMessageHtml(pendingAssistant);
                  mergedMessages.push(pendingAssistant);
                  pendingAssistant = null;
                } else {
                  mergedMessages.push({
                    _uid: ++_msgUid,
                    role: 'assistant',
                    content: m.content || '',
                    steps: [],
                    html: m.content ? `<div class="final-reply">${renderMarkdown(m.content)}</div>` : ''
                  });
                }
              }
            }
          }

          // 收尾最后的 pending assistant
          if (pendingAssistant) {
            pendingAssistant.html = buildMessageHtml(pendingAssistant);
            mergedMessages.push(pendingAssistant);
            pendingAssistant = null;
          }
        }

        messages.value = mergedMessages;  // 一次原子更新
        await nextTick();
        scrollToBottom();
      } catch (e) {
        console.error('Switch conversation error:', e);
      }
    }

    // ── 删除对话 ──
    async function deleteConversation(sid) {
      try {
        await API.deleteConversation(sid);
        if (currentSessionId.value === sid) {
          currentSessionId.value = null;
          messages.value = [];
        }
        await loadConversations();
      } catch (e) {
        console.error('Delete conversation error:', e);
      }
    }

    // ── 加载对话列表 ──
    async function loadConversations() {
      try {
        const data = await API.listConversations();
        conversations.value = Array.isArray(data) ? data : (data.sessions || []);
      } catch (e) {
        console.error('Load conversations error:', e);
      }
    }

    // ── API Key ──
    async function loadApiKeys() {
      try {
        const data = await API.getApiKeys();
        apiProviders.value = Array.isArray(data) ? data : (data.providers || []);
      } catch (e) {
        console.error('Load API keys error:', e);
        apiProviders.value = [
          { id: 'deepseek', name: 'DeepSeek', configured: true, masked: 'sk-****64bb', newKey: '' }
        ];
      }
    }

    async function saveApiKey(provider) {
      if (!provider.newKey) return;
      try {
        await API.saveApiKey(provider.id, provider.newKey);
        provider.configured = true;
        const key = provider.newKey;
        if (key.length >= 8) {
          provider.masked = key.slice(0, 6) + '****' + key.slice(-4);
        } else {
          provider.masked = '****' + key.slice(-4);
        }
        provider.newKey = '';
        alert('✅ API Key 已保存');
      } catch (e) {
        alert('保存失败: ' + e.message);
      }
    }

    // ── 键盘 ──
    function onInputKeydown(e) {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        sendMessage();
      }
    }

    function onGlobalKeydown(e) {
      if (e.key === 'Escape') {
        showSettings.value = false;
      }
    }

    // ── 时间格式化 ──
    function formatTime(t) {
      if (!t) return '';
      const d = new Date(t);
      const now = new Date();
      const diff = now - d;
      if (diff < 60000) return '刚刚';
      if (diff < 3600000) return Math.floor(diff / 60000) + '分钟前';
      if (diff < 86400000) return Math.floor(diff / 3600000) + '小时前';
      return `${d.getMonth() + 1}/${d.getDate()} ${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
    }

    // ── 消息操作 ──
    function copyMessage(msg) {
      if (!msg || !msg.content) return;
      navigator.clipboard.writeText(msg.content).catch(() => {});
    }

    function resendMessage(msg) {
      if (!msg || !msg.content) return;
      inputText.value = msg.content;
      sendMessage();
    }

    // ── 初始化 ──
    async function init() {
      document.addEventListener('keydown', onGlobalKeydown);

      await Promise.all([
        loadConversations(),
        loadApiKeys()
      ]);

      if (conversations.value.length > 0) {
        const firstId = conversations.value[0].id || conversations.value[0].session_id;
        if (firstId) {
          await switchConversation(firstId);
        }
      }

      // 复制按钮事件委托
      if (msgContainer.value) {
        msgContainer.value.addEventListener('click', (e) => {
          const btn = e.target.closest('.copy-btn');
          if (!btn) return;
          const safeCode = btn.dataset.code;
          if (!safeCode) return;
          try {
            const code = decodeURIComponent(escape(atob(safeCode)));
            navigator.clipboard.writeText(code).then(() => {
              btn.classList.add('copied');
              btn.textContent = '✅ 已复制';
              setTimeout(() => {
                btn.classList.remove('copied');
                btn.textContent = '📋 复制';
              }, 2000);
            });
          } catch (e) {
            console.error('Copy failed:', e);
          }
        });
      }
    }

    init();

    return {
      messages, inputText, sending, statusText, statusClass,
      conversations, currentSessionId, modelName, searchEnabled, selectModel,
      showSettings, apiProviders,
      sendMessage, interruptSession, newConversation,
      switchConversation, deleteConversation,
      copyMessage, resendMessage,
      saveApiKey,
      formatTime, escapeHtml,
      onInputKeydown, msgContainer, inputEl, interrupting,
      // Claude Code 增强
      todoItems, todoStats, pendingApprovals, respondApproval,
    };
  }
});

app.mount('#app');
console.log('Jarvis V3 前端已加载 ✅');
