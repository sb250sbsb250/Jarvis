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
    const isProModel = ref(true);
    
    function toggleModel() {
      modelName.value = isProModel.value ? 'deepseek-v4-pro' : 'deepseek-v4-flash';
    }

    const showSettings = ref(false);
    const apiProviders = ref([]);

    const inputEl = ref(null);
    const msgContainer = ref(null);
    const interrupting = ref(false);

    let _currentMsgIndex = -1;
    let _msgUid = 0;
    let _abortController = null;

    // ── HTML 转义 ──
    function escapeHtml(text) {
      if (!text) return '';
      return text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;').replace(/'/g, '&#039;');
    }

    // ── Markdown 渲染 ──
    function renderMarkdown(text) {
      if (!text) return '';
      let html = escapeHtml(text);

      // 代码块
      html = html.replace(/```(\w*)\n([\s\S]*?)```/g, (_, lang, code) => {
        const langClass = lang ? ` class="language-${escapeHtml(lang)}"` : '';
        const escapedCode = escapeHtml(code);
        const safeCode = btoa(unescape(encodeURIComponent(code)));
        return `<pre><button class="copy-btn" data-code="${safeCode}">📋 复制</button><code${langClass}>${escapedCode}</code></pre>`;
      });

      // 行内代码
      html = html.replace(/`([^`]+)`/g, '<code>$1</code>');

      // 表格
      html = html.replace(/\n\|(.+)\|\n\|([-| :]+)\|\n((?:\|.+\|\n?)*)/g, (_, header, alignLine, body) => {
        const headers = header.split('|').map(h => h.trim()).filter(Boolean);
        const aligns = alignLine.split('|').map(a => a.trim()).filter(Boolean).map(a => {
          if (a.startsWith(':') && a.endsWith(':')) return 'center';
          if (a.endsWith(':')) return 'right';
          return 'left';
        });
        const rows = body.trim().split('\n').map(row => {
          const cols = row.split('|').map(c => c.trim()).filter(Boolean);
          return `<tr>${cols.map((c, i) => `<td style="text-align:${aligns[i] || 'left'}">${c}</td>`).join('')}</tr>`;
        }).join('');
        return `\n<table><thead><tr>${headers.map((h, i) => `<th style="text-align:${aligns[i] || 'left'}">${h}</th>`).join('')}</tr></thead><tbody>${rows}</tbody></table>\n`;
      });

      // 引用
      html = html.replace(/^&gt;\s?(.+)$/gm, '<blockquote>$1</blockquote>');

      // 粗体/斜体
      html = html.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
      html = html.replace(/\*(.+?)\*/g, '<em>$1</em>');

      // 无序列表
      html = html.replace(/^[*-]\s+(.+)$/gm, '<li>$1</li>');
      html = html.replace(/(<li>.*<\/li>\n?)+/g, '<ul>$&</ul>');

      // 有序列表
      html = html.replace(/^\d+\.\s+(.+)$/gm, '<li>$1</li>');

      // 标题
      html = html.replace(/^###\s+(.+)$/gm, '<h4>$1</h4>');
      html = html.replace(/^##\s+(.+)$/gm, '<h3>$1</h3>');

      // 链接
      html = html.replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2" target="_blank">$1</a>');

      // 段落
      html = html.replace(/\n\n/g, '</p><p>');
      html = '<p>' + html + '</p>';

      // 清理嵌套
      const cleanPairs = [
        [/<p><ul>/g, '<ul>'], [/<\/ul><\/p>/g, '</ul>'],
        [/<p><ol>/g, '<ol>'], [/<\/ol><\/p>/g, '</ol>'],
        [/<p><blockquote>/g, '<blockquote>'], [/<\/blockquote><\/p>/g, '</blockquote>'],
        [/<p><table>/g, '<table>'], [/<\/table><\/p>/g, '</table>'],
        [/<p><h([34])>/g, '<h$1>'], [/<\/h([34])><\/p>/g, '</h$1>'],
        [/<p><pre>/g, '<pre>'], [/<\/pre><\/p>/g, '</pre>'],
      ];
      for (const [re, replace] of cleanPairs) {
        html = html.replace(re, replace);
      }

      return html;
    }

    // ── 渲染消息 ──
    function renderMessage(msg) {
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

      msg.html = html;
      
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
      const { stream, controller } = API.createChatStream(text, currentSessionId.value, modelName.value);
      _abortController = controller;

      try {
        const reader = await stream;
        const decoder = new TextDecoder();
        let buffer = '';

        while (true) {
          let { done, value } = await reader.read();
          if (done) break;

          buffer += decoder.decode(value, { stream: true });
          const parts = buffer.split('\n\n');
          buffer = parts.pop() || '';

          for (const part of parts) {
            for (const line of part.split('\n')) {
              if (line.startsWith('data: ')) {
                const payload = line.slice(6);
                if (payload === '[DONE]') {
                  console.log('SSE stream done');
                  done = true;
                  break;
                }
                try {
                  handleSSEEvent(JSON.parse(payload));
                } catch (e) {
                  console.error('SSE parse error:', e);
                }
              }
            }
          }
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
        messages.value = [];
        if (data.messages) {
          for (const m of data.messages) {
            const msg = {
              _uid: ++_msgUid,
              role: m.role || 'assistant',
              content: m.content || '',
              steps: m.steps || []
            };
            // 如果是 V3 格式，消息结构略有不同
            if (m.role === 'user') {
              msg.html = `<p>${escapeHtml(m.content || '')}</p>`;
            } else {
              msg.html = '';
              if (msg.steps.length > 0) {
                for (const step of msg.steps) {
                  step.state = step.state || 'done';
                  step.icon = step.state === 'done' ? '✅' : step.state === 'error' ? '❌' : '⏳';
                }
              }
              renderMessage(msg);
            }
            messages.value.push(msg);
          }
        }
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
        provider.masked = provider.newKey.slice(-4);
        provider.masked = '****' + provider.newKey.slice(-4);
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
      conversations, currentSessionId, modelName, isProModel, toggleModel,
      showSettings, apiProviders,
      sendMessage, interruptSession, newConversation,
      switchConversation, deleteConversation,
      copyMessage, resendMessage,
      saveApiKey,
      formatTime, escapeHtml,
      onInputKeydown, msgContainer, inputEl, interrupting
    };
  }
});

app.mount('#app');
console.log('Jarvis V3 前端已加载 ✅');
