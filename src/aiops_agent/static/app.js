(() => {
  const csrf = document.querySelector('meta[name="csrf-token"]')?.content || '';
  const flash = document.getElementById('flash');

  function notify(message, error = false) {
    if (!flash) return;
    flash.textContent = message;
    flash.classList.toggle('error', error);
    flash.classList.add('visible');
    window.setTimeout(() => flash.classList.remove('visible'), 4500);
  }

  async function postJSON(url, body) {
    const response = await fetch(url, {
      method: 'POST',
      credentials: 'same-origin',
      headers: {'Content-Type': 'application/json', 'X-CSRF-Token': csrf},
      body: JSON.stringify(body),
    });
    const payload = await response.json().catch(() => ({detail: response.statusText}));
    if (!response.ok) throw new Error(payload.detail || `HTTP ${response.status}`);
    return payload;
  }

  const diagnoseForm = document.getElementById('diagnose-form');
  diagnoseForm?.addEventListener('submit', async (event) => {
    event.preventDefault();
    const button = diagnoseForm.querySelector('button');
    button.disabled = true;
    button.textContent = '正在创建…';
    try {
      const result = await postJSON('/ui-api/diagnose', {
        service: diagnoseForm.dataset.service,
        symptom: diagnoseForm.querySelector('[name="symptom"]').value,
      });
      window.location.assign(result.location);
    } catch (error) {
      notify(error.message, true);
      button.disabled = false;
      button.textContent = '运行诊断';
    }
  });

  const dialog = document.getElementById('decision-dialog');
  const confirmButton = document.getElementById('confirm-decision');
  let pendingDecision = null;
  document.querySelectorAll('.decision-button').forEach((button) => {
    button.addEventListener('click', () => {
      pendingDecision = {
        action: button.dataset.action,
        digest: button.dataset.digest,
        decision: button.dataset.decision,
      };
      document.getElementById('dialog-title').textContent = button.dataset.decision === 'approve' ? '批准高风险变更' : '拒绝变更提案';
      document.getElementById('dialog-copy').textContent = `${button.dataset.kind} 将作用于 ${button.dataset.target}。审批仅对当前摘要有效。`;
      document.getElementById('dialog-digest').textContent = button.dataset.digest;
      confirmButton.textContent = button.dataset.decision === 'approve' ? '批准并执行' : '确认拒绝';
      confirmButton.classList.toggle('danger-button', button.dataset.decision === 'approve');
      dialog.showModal();
    });
  });

  confirmButton?.addEventListener('click', async () => {
    if (!pendingDecision) return;
    confirmButton.disabled = true;
    try {
      const result = await postJSON(`/ui-api/actions/${pendingDecision.action}/decision`, {
        decision: pendingDecision.decision,
        action_digest: pendingDecision.digest,
      });
      dialog.close();
      window.location.assign(result.location);
    } catch (error) {
      notify(error.message, true);
      confirmButton.disabled = false;
    }
  });

  const taskRoot = document.querySelector('[data-task-id]');
  if (taskRoot) {
    const eventList = document.getElementById('event-list');
    const source = new EventSource(`/ui/tasks/${taskRoot.dataset.taskId}/stream?after=${taskRoot.dataset.after}`);
    source.addEventListener('audit', (event) => {
      const item = JSON.parse(event.data);
      const row = document.createElement('div');
      row.className = 'event-row new';
      row.dataset.seq = item.seq;
      row.innerHTML = `<code>${item.seq}</code><span><strong></strong><small></small></span><pre></pre>`;
      row.querySelector('strong').textContent = item.event_type;
      row.querySelector('small').textContent = item.created_at;
      row.querySelector('pre').textContent = JSON.stringify(item.data);
      eventList?.appendChild(row);
      taskRoot.dataset.after = item.seq;
      if (item.event_type === 'task.status') {
        const status = item.data.to;
        const statusNode = document.getElementById('task-status');
        if (statusNode) {
          statusNode.textContent = status;
          statusNode.className = `status-label ${status}`;
        }
        if (['waiting_approval', 'succeeded', 'failed', 'rolled_back', 'cancelled'].includes(status)) {
          window.setTimeout(() => window.location.reload(), 650);
        }
      }
    });
    source.onerror = () => notify('实时事件连接暂时中断，浏览器会自动重连。', true);
  }
})();
