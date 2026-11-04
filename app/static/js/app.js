/* Telegram Quiz Manager — dashboard client logic. */
const TQM = (() => {
  const csrfToken = () =>
    document.querySelector('meta[name="csrf-token"]')?.content || '';

  async function api(url, { method = 'GET', body, form } = {}) {
    const options = {
      method,
      headers: { 'X-CSRF-Token': csrfToken() },
      credentials: 'same-origin',
    };
    if (form) {
      options.body = form; // FormData: let the browser set the boundary
    } else if (body !== undefined) {
      options.headers['Content-Type'] = 'application/json';
      options.body = JSON.stringify(body);
    }

    let response;
    try {
      response = await fetch(url, options);
    } catch (error) {
      return { ok: false, error: 'تعذّر الاتصال بالخادم.' };
    }

    if (response.status === 401) {
      window.location.href = '/login';
      return { ok: false, error: 'انتهت الجلسة.' };
    }

    let data = {};
    const text = await response.text();
    if (text) {
      try { data = JSON.parse(text); } catch { data = { message: text }; }
    }

    if (!response.ok) {
      return {
        ok: false,
        error: data.error || data.detail || `فشل الطلب (${response.status}).`,
      };
    }
    return { ok: true, data };
  }

  function toast(message, level = 'success') {
    const area = document.getElementById('toast-area');
    if (!area) return;
    const icons = { success: 'check-circle-fill', error: 'x-circle-fill', warning: 'exclamation-triangle-fill', info: 'info-circle-fill' };
    const node = document.createElement('div');
    node.className = `tqm-toast ${level}`;
    node.innerHTML = `<i class="bi bi-${icons[level] || icons.info}"></i> ${message}`;
    area.appendChild(node);
    setTimeout(() => node.remove(), 4200);
  }

  async function quickAction(url, label) {
    const result = await api(url, { method: 'POST' });
    if (result.ok) {
      toast(`${label}: تم بنجاح`);
      setTimeout(() => window.location.reload(), 600);
    } else {
      toast(result.error, 'error');
    }
  }

  async function publishNow() {
    const result = await api('/api/publishing/publish-now', { method: 'POST', body: {} });
    if (result.ok && result.data.ok) {
      toast(`تم نشر السؤال ${result.data.entry.question_id}`);
      setTimeout(() => window.location.reload(), 800);
    } else {
      toast(result.data?.error || result.error || 'فشل النشر', 'error');
    }
  }

  function previewQuestion(question) {
    const modalEl = document.getElementById('previewModal');
    if (!modalEl) return;
    document.getElementById('pvQuestion').textContent = question.question;
    const options = document.getElementById('pvOptions');
    options.innerHTML = '';
    question.options.forEach((option, index) => {
      const li = document.createElement('li');
      if (index === question.correct_option_id) li.classList.add('correct');
      li.innerHTML = `<span>${option}</span>
        ${index === question.correct_option_id
          ? '<i class="bi bi-check-circle-fill text-success"></i>'
          : `<span class="opt-pct">${Math.round(Math.random() * 20 + 3)}%</span>`}`;
      options.appendChild(li);
    });
    const explanation = document.getElementById('pvExplanation');
    explanation.textContent = question.explanation || '';
    explanation.classList.toggle('visible', Boolean(question.explanation));
    document.getElementById('pvMeta').textContent =
      `${question.category} · ${question.difficulty} · ${question.id}`;
    new bootstrap.Modal(modalEl).show();
  }

  async function duplicateQuestion(id) {
    const result = await api(`/api/questions/${id}/duplicate`, { method: 'POST' });
    if (result.ok) {
      toast(`تم إنشاء نسخة: ${result.data.id}`);
      setTimeout(() => window.location.reload(), 700);
    } else {
      toast(result.error, 'error');
    }
  }

  async function deleteQuestion(id) {
    if (!confirm(`حذف السؤال ${id}؟ لا يمكن التراجع عن هذه العملية.`)) return;
    const result = await api(`/api/questions/${id}`, { method: 'DELETE' });
    if (result.ok) {
      toast(`تم حذف السؤال ${id}`, 'warning');
      document.querySelector(`tr[data-id="${id}"]`)?.remove();
    } else {
      toast(result.error, 'error');
    }
  }

  function selectedIds() {
    return Array.from(document.querySelectorAll('.row-check:checked')).map((el) => el.value);
  }

  async function bulk(action, status) {
    const ids = selectedIds();
    if (!ids.length) { toast('لم يتم تحديد أي سؤال.', 'warning'); return; }
    if (action === 'delete') {
      const all = document.querySelectorAll('.row-check').length;
      if (ids.length === all && all > 1) {
        // Deleting the whole bank is the one action that cannot be undone from the
        // UI; make it a deliberate, typed confirmation.
        const typed = prompt(
          `أنت على وشك حذف كل الأسئلة (${ids.length}).\nاكتب «حذف» للمتابعة:`
        );
        if (typed === null) return;
        if (typed.trim() !== 'حذف') { toast('أُلغيت العملية.', 'warning'); return; }
      } else if (!confirm(`حذف ${ids.length} سؤالاً؟`)) {
        return;
      }
    }
    const result = await api('/api/questions/bulk', {
      method: 'POST',
      body: { ids, action, status, confirm: action === 'delete' },
    });
    if (result.ok) {
      toast(`تم تنفيذ الإجراء على ${result.data.affected} سؤالاً`);
      setTimeout(() => window.location.reload(), 700);
    } else {
      toast(result.error, 'error');
    }
  }

  function initQuestionTable() {
    const checkAll = document.getElementById('checkAll');
    const bulkBar = document.getElementById('bulkBar');
    const bulkCount = document.getElementById('bulkCount');

    const refresh = () => {
      const count = selectedIds().length;
      if (bulkBar) bulkBar.hidden = count === 0;
      if (bulkCount) bulkCount.textContent = count;
    };

    checkAll?.addEventListener('change', () => {
      document.querySelectorAll('.row-check').forEach((box) => { box.checked = checkAll.checked; });
      refresh();
    });
    document.querySelectorAll('.row-check').forEach((box) => box.addEventListener('change', refresh));
  }

  function initImportForm() {
    const form = document.getElementById('importForm');
    if (!form) return;
    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      const result = await api('/api/questions/import-file', {
        method: 'POST',
        form: new FormData(form),
      });
      const box = document.getElementById('importResult');
      if (result.ok) {
        const report = result.data;
        box.innerHTML = `<div class="alert alert-success small mt-2">
          تم الاستيراد: <strong>${report.imported}</strong> ·
          تحديث: <strong>${report.updated}</strong> ·
          تخطي: <strong>${report.skipped}</strong> ·
          فشل: <strong>${report.failed}</strong></div>`;
        toast(`تم استيراد ${report.imported} سؤالاً`);
        setTimeout(() => window.location.reload(), 1600);
      } else {
        box.innerHTML = `<div class="alert alert-danger small mt-2">${result.error}</div>`;
      }
    });
  }

  // ---------- Question editor ----------
  function initEditor(existing) {
    const list = document.getElementById('optionsList');
    if (!list) return;
    const form = document.getElementById('questionForm');
    let correctIndex = existing ? existing.correct_option_id : 0;

    function renderOptions() {
      const current = Array.from(list.querySelectorAll('.option-input')).map((i) => i.value);
      const options = current.length ? current : (existing ? [...existing.options] : ['', '', '', '']);
      list.innerHTML = '';
      options.forEach((value, index) => {
        const row = document.createElement('div');
        row.className = 'option-row';
        row.innerHTML = `
          <button type="button" class="correct-pick ${index === correctIndex ? 'selected' : ''}"
                  title="تحديد كإجابة صحيحة">${String.fromCharCode(65 + index)}</button>
          <input class="form-control option-input" value="${value.replace(/"/g, '&quot;')}"
                 placeholder="نص الخيار ${index + 1}" maxlength="100">
          <button type="button" class="btn btn-outline-danger btn-sm remove-opt"><i class="bi bi-x"></i></button>`;
        row.querySelector('.correct-pick').addEventListener('click', () => {
          correctIndex = index;
          renderOptions();
          syncPreview();
        });
        row.querySelector('.remove-opt').addEventListener('click', () => {
          if (list.querySelectorAll('.option-row').length <= 2) {
            toast('يجب الاحتفاظ بخيارين على الأقل.', 'warning');
            return;
          }
          row.remove();
          if (correctIndex >= list.querySelectorAll('.option-row').length) {
            correctIndex = 0;
          }
          renderOptions();
          syncPreview();
        });
        row.querySelector('.option-input').addEventListener('input', syncPreview);
        list.appendChild(row);
      });
      syncPreview();
    }

    function syncPreview() {
      const text = document.getElementById('questionText').value || 'نص السؤال...';
      document.getElementById('liveQuestion').textContent = text;
      const options = document.getElementById('liveOptions');
      options.innerHTML = '';
      Array.from(list.querySelectorAll('.option-input')).forEach((input, index) => {
        const li = document.createElement('li');
        if (index === correctIndex) li.classList.add('correct');
        li.innerHTML = `<span>${input.value || '...'}</span>`;
        options.appendChild(li);
      });
      const explanation = document.getElementById('liveExplanation');
      explanation.textContent = document.getElementById('explanation').value;
      explanation.classList.toggle('visible', Boolean(explanation.textContent));
    }

    document.getElementById('addOption')?.addEventListener('click', () => {
      const count = list.querySelectorAll('.option-row').length;
      if (count >= 10) { toast('الحد الأقصى 10 خيارات.', 'warning'); return; }
      const row = document.createElement('div');
      row.className = 'option-row';
      row.innerHTML = `
        <button type="button" class="correct-pick">${String.fromCharCode(65 + count)}</button>
        <input class="form-control option-input" placeholder="نص الخيار ${count + 1}" maxlength="100">
        <button type="button" class="btn btn-outline-danger btn-sm remove-opt"><i class="bi bi-x"></i></button>`;
      list.appendChild(row);
      renderOptions();
    });

    document.getElementById('questionText')?.addEventListener('input', syncPreview);
    document.getElementById('explanation')?.addEventListener('input', syncPreview);

    form?.addEventListener('submit', async (event) => {
      event.preventDefault();
      const options = Array.from(list.querySelectorAll('.option-input')).map((i) => i.value.trim());
      const payload = {
        question: document.getElementById('questionText').value.trim(),
        options,
        correct_option_id: correctIndex,
        explanation: document.getElementById('explanation').value.trim(),
        category: document.getElementById('category').value.trim() || 'General',
        difficulty: document.getElementById('difficulty').value,
        status: document.getElementById('status').value,
      };
      const mode = form.dataset.mode;
      const id = form.dataset.id;
      const errorBox = document.getElementById('formError');
      const result = mode === 'edit'
        ? await api(`/api/questions/${id}`, { method: 'PUT', body: payload })
        : await api('/api/questions', { method: 'POST', body: payload });

      if (result.ok) {
        toast('تم حفظ السؤال');
        window.location.href = '/questions';
      } else {
        errorBox.textContent = result.error;
        errorBox.hidden = false;
        window.scrollTo({ top: 0, behavior: 'smooth' });
      }
    });

    document.getElementById('deleteBtn')?.addEventListener('click', async () => {
      const id = form.dataset.id;
      if (!confirm(`حذف السؤال ${id}؟`)) return;
      const result = await api(`/api/questions/${id}`, { method: 'DELETE' });
      if (result.ok) window.location.href = '/questions';
      else toast(result.error, 'error');
    });

    renderOptions();
  }

  // ---------- Channels ----------
  function initChannelForm() {
    const form = document.getElementById('channelForm');
    if (!form) return;
    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      const data = Object.fromEntries(new FormData(form).entries());
      const result = await api('/api/channels', { method: 'POST', body: { ...data, enabled: true } });
      if (result.ok) {
        toast(`تمت إضافة القناة ${result.data.name}`);
        setTimeout(() => window.location.reload(), 700);
      } else {
        toast(result.error, 'error');
      }
    });
  }

  const showChannelForm = () => {
    const panel = document.getElementById('channelFormPanel');
    if (panel) { panel.hidden = false; panel.scrollIntoView({ behavior: 'smooth' }); }
  };
  const hideChannelForm = () => {
    const panel = document.getElementById('channelFormPanel');
    if (panel) panel.hidden = true;
  };

  async function testChannel(id) {
    const result = await api(`/api/channels/${id}/test`, { method: 'POST' });
    if (result.ok && result.data.ok) {
      toast(result.data.dry_run ? 'اختبار ناجح (وضع تجريبي)' : 'تم إرسال رسالة الاختبار بنجاح');
    } else {
      toast(result.data?.error || result.error || 'فشل اختبار القناة', 'error');
    }
  }

  const toggleChannel = (id) => quickAction(`/api/channels/${id}/toggle`, 'تغيير حالة القناة');
  const setDefaultChannel = (id) => quickAction(`/api/channels/${id}/default`, 'تعيين القناة الافتراضية');

  async function deleteChannel(id) {
    if (!confirm('حذف هذه القناة؟')) return;
    const result = await api(`/api/channels/${id}`, { method: 'DELETE' });
    if (result.ok) { toast('تم حذف القناة', 'warning'); setTimeout(() => window.location.reload(), 600); }
    else toast(result.error, 'error');
  }

  // ---------- Schedules ----------
  function initScheduleForm() {
    const form = document.getElementById('scheduleForm');
    if (!form) return;
    const typeSelect = document.getElementById('scheduleType');
    const selectionMode = document.getElementById('selectionMode');
    const categoryWrap = document.getElementById('categoryFilterWrap');

    function syncType() {
      const type = typeSelect.value;
      form.querySelectorAll('[data-type]').forEach((element) => {
        element.hidden = !element.dataset.type.split(' ').includes(type);
      });
    }
    typeSelect.addEventListener('change', syncType);
    syncType();

    selectionMode?.addEventListener('change', () => {
      categoryWrap.hidden = selectionMode.value !== 'category';
    });

    document.getElementById('addTime')?.addEventListener('click', () => {
      const list = document.getElementById('timesList');
      const row = document.createElement('div');
      row.className = 'time-row';
      row.innerHTML = `<input class="form-control" type="time" name="times[]" value="12:00">
        <button class="btn btn-outline-danger btn-sm" type="button" onclick="this.parentElement.remove()">
          <i class="bi bi-x"></i></button>`;
      list.appendChild(row);
    });

    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      const formData = new FormData(form);
      const payload = {
        name: formData.get('name') || '',
        channel_id: formData.get('channel_id'),
        type: formData.get('type'),
        enabled: true,
        times: formData.getAll('times[]').filter(Boolean),
        weekdays: formData.getAll('weekdays[]').map(Number),
        interval_minutes: Number(formData.get('interval_minutes') || 0),
        timezone: formData.get('timezone') || '',
        question_selection: formData.get('question_selection') || 'sequential',
        category_filter: formData.get('category_filter') || '',
        questions_per_day: Number(formData.get('questions_per_day') || 0),
        questions_per_run: Number(formData.get('questions_per_run') || 1),
        batch_interval_seconds: Number(formData.get('batch_interval_seconds') || 0),
        start_date: formData.get('start_date') || '',
        end_date: formData.get('end_date') || '',
      };
      const result = await api('/api/schedules', { method: 'POST', body: payload });
      if (result.ok) {
        toast(`تم إنشاء الجدول ${result.data.id}`);
        setTimeout(() => window.location.reload(), 700);
      } else {
        toast(result.error, 'error');
      }
    });
  }

  const showScheduleForm = () => {
    const panel = document.getElementById('scheduleFormPanel');
    if (panel) { panel.hidden = false; panel.scrollIntoView({ behavior: 'smooth' }); }
  };
  const hideScheduleForm = () => {
    const panel = document.getElementById('scheduleFormPanel');
    if (panel) panel.hidden = true;
  };

  async function scheduleAction(id, action) {
    if (action === 'delete') {
      if (!confirm(`حذف الجدول ${id}؟`)) return;
      const result = await api(`/api/schedules/${id}`, { method: 'DELETE' });
      if (result.ok) { toast('تم حذف الجدول', 'warning'); setTimeout(() => window.location.reload(), 600); }
      else toast(result.error, 'error');
      return;
    }
    const labels = { toggle: 'تغيير حالة الجدول', 'skip-next': 'تجاوز الموعد القادم', 'run-now': 'تنفيذ الجدول الآن' };
    const result = await api(`/api/schedules/${id}/${action}`, { method: 'POST' });
    if (result.ok) {
      if (action === 'run-now' && !result.data.ok) {
        toast(result.data.result?.reason || 'لم يتم النشر', 'warning');
      } else if (action === 'run-now') {
        const published = result.data.result?.published ?? 1;
        toast(`تم نشر ${published} سؤال`);
      } else {
        toast(`${labels[action]}: تم`);
      }
      setTimeout(() => window.location.reload(), 800);
    } else {
      toast(result.error, 'error');
    }
  }

  // ---------- Publishing history ----------
  async function retryPublish(entryId) {
    const result = await api(`/api/publishing/retry/${entryId}`, { method: 'POST' });
    if (result.ok && result.data.ok) {
      toast('تمت إعادة النشر بنجاح');
      setTimeout(() => window.location.reload(), 700);
    } else {
      toast(result.data?.error || result.error || 'فشل إعادة النشر', 'error');
    }
  }

  // ---------- Manual publish (right-click) ----------
  function initManualPublish() {
    const menu = document.getElementById('ctxMenu');
    const modalEl = document.getElementById('manualPublishModal');
    if (!menu || !modalEl) return;

    const titleEl = document.getElementById('ctxMenuTitle');
    const channelSelect = document.getElementById('mpChannel');
    const confirmBtn = document.getElementById('mpConfirm');
    let target = null; // { id, text }

    const closeMenu = () => { menu.hidden = true; };

    function openMenu(event, row) {
      target = {
        id: row.dataset.questionId,
        text: row.dataset.questionText || '',
      };
      titleEl.textContent = `${target.id} — ${target.text}`.slice(0, 70);
      menu.hidden = false;

      // Keep the menu inside the viewport, accounting for the RTL layout.
      const rect = menu.getBoundingClientRect();
      const x = Math.min(event.clientX, window.innerWidth - rect.width - 8);
      const y = Math.min(event.clientY, window.innerHeight - rect.height - 8);
      menu.style.insetInlineStart = `${Math.max(8, x)}px`;
      menu.style.insetBlockStart = `${Math.max(8, y)}px`;
    }

    document.addEventListener('contextmenu', (event) => {
      const row = event.target.closest('tr[data-question-id]');
      if (!row) { closeMenu(); return; }
      event.preventDefault();
      openMenu(event, row);
    });

    document.addEventListener('click', (event) => {
      if (!menu.hidden && !menu.contains(event.target)) closeMenu();
    });
    document.addEventListener('keydown', (event) => {
      if (event.key === 'Escape') closeMenu();
    });
    window.addEventListener('scroll', closeMenu, true);

    menu.querySelectorAll('.ctx-item').forEach((item) => {
      item.addEventListener('click', async () => {
        const action = item.dataset.action;
        closeMenu();
        if (!target) return;

        if (action === 'edit') {
          window.location.href = `/questions/${target.id}/edit`;
          return;
        }
        if (action === 'copy') {
          try {
            await navigator.clipboard.writeText(target.text);
            toast('تم نسخ نص السؤال');
          } catch {
            toast('تعذّر النسخ إلى الحافظة.', 'warning');
          }
          return;
        }
        if (action === 'preview') {
          const result = await api(`/api/questions/${target.id}`);
          if (result.ok) previewQuestion(result.data);
          else toast(result.error, 'error');
          return;
        }
        if (action === 'publish') {
          document.getElementById('mpQuestion').textContent = target.text || '—';
          document.getElementById('mpQuestionId').textContent = target.id;
          new bootstrap.Modal(modalEl).show();
        }
      });
    });

    confirmBtn?.addEventListener('click', async () => {
      if (!target) return;
      const channelId = channelSelect?.value;
      if (!channelId) { toast('اختر قناة أولاً.', 'warning'); return; }

      confirmBtn.disabled = true;
      const result = await api('/api/publishing/manual', {
        method: 'POST',
        body: { question_id: target.id, channel_id: channelId },
      });
      confirmBtn.disabled = false;

      if (result.ok && result.data.ok) {
        const channelName = channelSelect.options[channelSelect.selectedIndex]?.text || '';
        toast(`تم نشر ${target.id} على ${channelName.split(' (')[0]}`);
        bootstrap.Modal.getInstance(modalEl)?.hide();
        setTimeout(() => window.location.reload(), 900);
      } else {
        toast(result.data?.error || result.error || 'فشل النشر', 'error');
      }
    });
  }

  // ---------- Settings ----------
  function revealToken(button) {
    const input = document.querySelector('#tokenForm input[name="token"]');
    if (!input) return;
    const hidden = input.type === 'password';
    input.type = hidden ? 'text' : 'password';
    button.innerHTML = `<i class="bi bi-eye${hidden ? '-slash' : ''}"></i>`;
  }

  function initTokenForm() {
    const form = document.getElementById('tokenForm');
    if (!form) return;
    const input = form.querySelector('input[name="token"]');
    const result = document.getElementById('tokenResult');

    const report = (ok, html) => {
      result.innerHTML = `<div class="alert alert-${ok ? 'success' : 'danger'} small py-2 mb-0">${html}</div>`;
    };

    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      const token = input.value.trim();
      if (!token) { toast('أدخل التوكن أولاً.', 'warning'); return; }
      const response = await api('/api/settings/telegram-token', {
        method: 'POST', body: { token },
      });
      if (response.ok) {
        report(true, `تم حفظ التوكن${response.data.restarted ? ' وتشغيل البوت' : ''}.`);
        toast('تم حفظ التوكن');
        input.value = '';
        setTimeout(() => window.location.reload(), 1200);
      } else {
        report(false, response.error);
        toast(response.error, 'error');
      }
    });

    document.getElementById('tokenTestBtn')?.addEventListener('click', async () => {
      const button = document.getElementById('tokenTestBtn');
      button.disabled = true;
      const response = await api('/api/settings/telegram-token/test', {
        method: 'POST', body: { token: input.value.trim() },
      });
      button.disabled = false;
      if (response.ok && response.data.ok) {
        report(true, `التوكن صالح — البوت @${response.data.username}`);
      } else {
        report(false, response.data?.error || response.error || 'فشل اختبار التوكن');
      }
    });

    document.getElementById('tokenClearBtn')?.addEventListener('click', async () => {
      if (!confirm('حذف التوكن المخزّن؟ سيعود النظام إلى وضع تجريبي إن لم يوجد توكن في .env.')) return;
      const response = await api('/api/settings/telegram-token', { method: 'DELETE' });
      if (response.ok) { toast('تم حذف التوكن المخزّن', 'warning'); setTimeout(() => window.location.reload(), 900); }
      else toast(response.error, 'error');
    });
  }

  function initSettingsForms() {
    initTokenForm();
    const settingsForm = document.getElementById('settingsForm');
    settingsForm?.addEventListener('submit', async (event) => {
      event.preventDefault();
      const form = new FormData(settingsForm);
      const payload = {
        timezone: form.get('timezone'),
        daily_limit: Number(form.get('daily_limit') || 0),
        publishing_enabled: form.get('publishing_enabled') === 'on',
        poll_options: {
          explanation: form.get('poll_explanation') === 'on',
          anonymous: form.get('poll_anonymous') === 'on',
          open_period: Number(form.get('open_period') || 0),
          allows_multiple_answers: false,
        },
        retry: {
          max_attempts: Number(form.get('max_attempts') || 3),
          delay_seconds: Number(form.get('delay_seconds') || 60),
        },
        analytics: {
          auto_collect: form.get('analytics_auto_collect') === 'on',
          auto_collect_hours: Number(form.get('analytics_auto_collect_hours') || 6),
        },
      };
      const result = await api('/api/settings', { method: 'PUT', body: payload });
      toast(result.ok ? 'تم حفظ الإعدادات' : result.error, result.ok ? 'success' : 'error');
    });

    const passwordForm = document.getElementById('passwordForm');
    passwordForm?.addEventListener('submit', async (event) => {
      event.preventDefault();
      const form = new FormData(passwordForm);
      const result = await api('/api/settings/password', {
        method: 'POST',
        body: {
          current_password: form.get('current_password'),
          new_password: form.get('new_password'),
        },
      });
      if (result.ok) { toast('تم تغيير كلمة المرور'); passwordForm.reset(); }
      else toast(result.error, 'error');
    });

    const profileForm = document.getElementById('profileForm');
    profileForm?.addEventListener('submit', async (event) => {
      event.preventDefault();
      const form = new FormData(profileForm);
      const result = await api('/api/settings/profile', {
        method: 'POST',
        body: { email: form.get('email') || '' },
      });
      toast(result.ok ? 'تم حفظ البيانات' : result.error, result.ok ? 'success' : 'error');
    });

    const restoreForm = document.getElementById('restoreForm');
    restoreForm?.addEventListener('submit', async (event) => {
      event.preventDefault();
      if (!confirm('استعادة البيانات من الأرشيف؟ سيتم استبدال البيانات الحالية.')) return;
      const result = await api('/api/import/restore', {
        method: 'POST',
        form: new FormData(restoreForm),
      });
      if (result.ok) { toast('تمت الاستعادة'); setTimeout(() => window.location.reload(), 900); }
      else toast(result.error, 'error');
    });
  }

  async function restoreBackup(name) {
    if (!confirm(`استعادة النسخة ${name}؟`)) return;
    const result = await api(`/api/backups/restore/${encodeURIComponent(name)}`, { method: 'POST' });
    if (result.ok) { toast('تمت الاستعادة'); setTimeout(() => window.location.reload(), 900); }
    else toast(result.error, 'error');
  }

  // ---------- Analytics ----------
  async function collectAnalytics() {
    const button = document.getElementById('collectBtn');
    if (button) button.disabled = true;
    toast('جارٍ جمع النتائج من Telegram…', 'info');
    const result = await api('/api/analytics/collect', { method: 'POST', body: {} });
    if (button) button.disabled = false;
    if (result.ok) {
      const d = result.data;
      if (!d.ok) {
        toast(`تعذّر جمع بعض النتائج (${d.errors} خطأ).`, 'warning');
      } else if (d.collected === 0) {
        toast('لا توجد نتائج جديدة لجمعها.', 'warning');
      } else {
        toast(`تم جمع نتائج ${d.collected} سؤالاً`);
      }
      setTimeout(() => window.location.reload(), 900);
    } else {
      toast(result.error, 'error');
    }
  }

  async function showPollDetail(pollId) {
    const body = document.getElementById('pollDetailBody');
    if (!body) return;
    body.innerHTML = '<p class="empty-state">جارٍ التحميل…</p>';
    new bootstrap.Modal(document.getElementById('pollDetailModal')).show();
    const result = await api(`/api/analytics/poll/${encodeURIComponent(pollId)}`);
    if (!result.ok || !result.data.ok) {
      body.innerHTML = `<div class="alert alert-warning mb-0">${result.data?.error || result.error}</div>`;
      return;
    }
    const p = result.data.poll;
    let html = `<h6 class="mb-1">${p.question_text}</h6>
      <p class="small text-secondary mb-3">
        ${p.channel_name} · <code>${p.question_id}</code> · رسالة ${p.message_id} ·
        ${p.votes} مصوّت · مشاركة ${p.participation_pct ?? '—'}%
      </p>`;
    if (p.explanation) {
      html += `<div class="poll-explanation mb-3">
        <span class="label"><i class="bi bi-info-circle"></i> شرح الإجابة (كما ظهر في Telegram)</span>
        <p class="mb-0">${p.explanation}</p>
      </div>`;
    }
    if (p.options.length) {
      html += '<ul class="poll-breakdown">';
      p.options.forEach((o) => {
        html += `<li class="${o.is_correct ? 'correct' : ''}">
          <span class="opt-label">${o.text}</span>
          <span class="opt-bar"><i style="width:${o.pct}%"></i></span>
          <span class="opt-num">${o.voter_count} (${o.pct}%)</span>
          ${o.is_correct ? '<i class="bi bi-check-circle-fill text-success"></i>' : ''}
        </li>`;
      });
      html += '</ul>';
    } else {
      html += '<p class="empty-state">لم تُجمع أعداد هذا السؤال بعد. اضغط «جمع النتائج الآن».</p>';
    }
    if (p.answers && p.answers.length) {
      html += `<h6 class="mt-3">إجابات الأعضاء (${p.answers.length})</h6>
        <div class="table-responsive"><table class="table table-sm">
        <thead><tr><th>العضو</th><th>الخيار</th><th>صحيحة؟</th><th>الوقت</th></tr></thead><tbody>`;
      p.answers.forEach((a) => {
        const choice = a.option_ids.map((i) => p.options[i]?.text ?? i).join(', ') || '—';
        html += `<tr><td>${a.username || a.user_id}</td><td>${choice}</td>
          <td>${a.is_correct === null ? '—' : (a.is_correct ? '✅' : '❌')}</td>
          <td class="small text-secondary">${a.answered_at}</td></tr>`;
      });
      html += '</tbody></table></div>';
    }
    body.innerHTML = html;
  }

  return {
    api, toast, quickAction, publishNow, previewQuestion, duplicateQuestion, deleteQuestion,
    bulk, initQuestionTable, initImportForm, initEditor,
    initChannelForm, showChannelForm, hideChannelForm, testChannel, toggleChannel,
    setDefaultChannel, deleteChannel,
    initScheduleForm, showScheduleForm, hideScheduleForm, scheduleAction,
    retryPublish, initSettingsForms, restoreBackup, revealToken,
    collectAnalytics, showPollDetail, initManualPublish,
  };
})();
