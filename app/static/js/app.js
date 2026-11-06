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

  // --- sidebar drawer / collapse ------------------------------------------
  // Below lg the nav is an off-canvas drawer (open with the topbar toggle).
  // At lg+ the same toggle collapses the sidebar to an icon rail. The desktop
  // state is remembered in localStorage.
  function initSidebar() {
    const toggle = document.getElementById('sidebarToggle');
    const sidebar = document.getElementById('appSidebar');
    const backdrop = document.getElementById('sidebarBackdrop');
    const closeBtn = document.getElementById('sidebarClose');
    if (!toggle || !sidebar) return;

    const LG = 992;
    const isDesktop = () => window.innerWidth >= LG;
    const isDrawerOpen = () => document.body.classList.contains('sidebar-open');
    const isCollapsed = () => document.body.classList.contains('sidebar-collapsed');

    // Icon-only items need a tooltip to stay usable when collapsed.
    sidebar.querySelectorAll('a.nav-item').forEach((link) => {
      const label = link.querySelector('span');
      if (label && !link.title) link.title = label.textContent.trim();
    });

    const syncToggle = () => {
      const expanded = isDesktop() ? !isCollapsed() : isDrawerOpen();
      toggle.setAttribute('aria-expanded', String(expanded));
    };
    const setDrawer = (open) => {
      document.body.classList.toggle('sidebar-open', open);
      if (backdrop) backdrop.hidden = !open;
      syncToggle();
    };
    const setCollapsed = (collapsed) => {
      document.body.classList.toggle('sidebar-collapsed', collapsed);
      try { localStorage.setItem('tqm.sidebarCollapsed', collapsed ? '1' : '0'); } catch (e) {}
      syncToggle();
    };

    // Restore the remembered desktop preference before the first paint settles.
    try {
      if (localStorage.getItem('tqm.sidebarCollapsed') === '1') setCollapsed(true);
    } catch (e) {}
    syncToggle();

    toggle.addEventListener('click', () => {
      if (isDesktop()) setCollapsed(!isCollapsed());
      else setDrawer(!isDrawerOpen());
    });
    closeBtn?.addEventListener('click', () => setDrawer(false));
    backdrop?.addEventListener('click', () => setDrawer(false));
    document.addEventListener('keydown', (event) => {
      if (event.key === 'Escape' && isDrawerOpen()) setDrawer(false);
    });
    // Navigating away should not leave the drawer covering the new page.
    sidebar.querySelectorAll('a.nav-item').forEach((link) => {
      link.addEventListener('click', () => setDrawer(false));
    });
    // Crossing the breakpoint drops the drawer state so the layout is sane.
    window.addEventListener('resize', () => {
      if (isDesktop() && isDrawerOpen()) setDrawer(false);
      syncToggle();
    });
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
  function syncChannelTransport() {
    const select = document.getElementById('channelTransport');
    const wrap = document.getElementById('channelUserbotWrap');
    if (!select || !wrap) return;
    wrap.hidden = select.value !== 'userbot';
  }

  function initChannelForm() {
    const form = document.getElementById('channelForm');
    if (!form) return;
    const transport = document.getElementById('channelTransport');
    transport?.addEventListener('change', syncChannelTransport);
    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      const data = Object.fromEntries(new FormData(form).entries());
      const payload = {
        name: data.name,
        chat_id: data.chat_id,
        description: data.description || '',
        transport: data.transport || 'bot',
        userbot_account_id: data.transport === 'userbot' ? (data.userbot_account_id || '') : '',
        enabled: true,
      };
      const editing = form.dataset.id || '';
      const result = editing
        ? await api(`/api/channels/${editing}`, { method: 'PUT', body: payload })
        : await api('/api/channels', { method: 'POST', body: payload });
      if (result.ok) {
        toast(editing ? 'تم تحديث القناة' : `تمت إضافة القناة ${result.data.name}`);
        setTimeout(() => window.location.reload(), 700);
      } else {
        toast(result.error, 'error');
      }
    });
  }

  const showChannelForm = (id) => {
    const panel = document.getElementById('channelFormPanel');
    const form = document.getElementById('channelForm');
    if (!panel || !form) return;
    form.reset();
    form.dataset.id = id || '';
    const title = document.getElementById('channelFormTitle');
    if (title) {
      title.innerHTML = id
        ? '<i class="bi bi-pencil"></i> تعديل قناة'
        : '<i class="bi bi-plus-circle"></i> إضافة قناة';
    }
    if (id) {
      const source = document.querySelector(`[data-channel='${id}']`);
      if (source) {
        form.name.value = source.dataset.name || '';
        form.chat_id.value = source.dataset.chatId || '';
        form.description.value = source.dataset.description || '';
        form.transport.value = source.dataset.transport || 'bot';
        form.userbot_account_id.value = source.dataset.userbotAccount || '';
      }
    }
    syncChannelTransport();
    panel.hidden = false;
    panel.scrollIntoView({ behavior: 'smooth' });
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
    if (action === 'clear-queue' && !confirm('إفراغ قائمة الأسئلة المرتبطة بهذا الجدول؟')) return;
    const labels = {
      toggle: 'تغيير حالة الجدول',
      'skip-next': 'تجاوز الموعد القادم',
      'run-now': 'تنفيذ الجدول الآن',
      'clear-queue': 'إفراغ قائمة الأسئلة',
    };
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

  // ---------- Bulk publish (selected questions -> channel, now or dedicated) ----------
  function initBulkPublish() {
    const modalEl = document.getElementById('bulkPublishModal');
    if (!modalEl) return;
    const confirmBtn = document.getElementById('bpConfirm');
    const channelSelect = document.getElementById('bpChannel');
    const progressWrap = document.getElementById('bpProgressWrap');
    const progressBar = document.getElementById('bpProgress');
    const estimateEl = document.getElementById('bpEstimate');
    const immediateNote = document.getElementById('bpImmediateNote');
    const dedicatedBox = document.getElementById('bpDedicated');
    const modeNow = document.getElementById('bpModeNow');
    const modeSchedule = document.getElementById('bpModeSchedule');
    const dupBox = document.getElementById('bpDuplicates');
    const dupList = document.getElementById('bpDupList');
    const cancelBtn = document.getElementById('bpCancelRun');
    const progressText = document.getElementById('bpProgressText');
    const canSchedule = modalEl.dataset.canSchedule === '1';
    // Server-rendered pacing interval, so the estimate matches what will happen.
    const interval = Number(modalEl.dataset.pacingInterval || 0);
    let pending = [];      // every selected id, in selection order
    let duplicateIds = []; // subset already published to the chosen channel
    let pollTimer = null;

    function humanDuration(seconds) {
      if (seconds < 60) return `${seconds} ثانية`;
      const minutes = Math.floor(seconds / 60);
      const rest = seconds % 60;
      return rest ? `${minutes} دقيقة و${rest} ثانية` : `${minutes} دقيقة`;
    }

    // The ids that will actually be published: every selected question except the
    // duplicates the user left unchecked.
    function approvedIds() {
      const keepDuplicates = new Set(
        Array.from(dupList?.querySelectorAll('input[type="checkbox"]:checked') || [])
          .map((box) => box.value)
      );
      return pending.filter((id) => !duplicateIds.includes(id) || keepDuplicates.has(id));
    }

    function refreshEstimate() {
      if (!estimateEl) return;
      const seconds = Math.max(0, (approvedIds().length - 1)) * interval;
      estimateEl.textContent = humanDuration(seconds);
    }

    function questionText(id) {
      const row = document.querySelector(`tr[data-question-id="${id}"] .q-text`);
      return row ? row.textContent.trim().slice(0, 70) : '';
    }

    function renderDuplicates() {
      if (!dupBox || !dupList) return;
      if (!duplicateIds.length) {
        dupBox.classList.add('d-none');
        dupList.innerHTML = '';
        return;
      }
      dupBox.classList.remove('d-none');
      dupList.innerHTML = duplicateIds
        .map(
          (id) => `<li class="bp-dup-item">
            <input type="checkbox" class="form-check-input dup-check" value="${id}" id="dup-${id}">
            <label class="form-check-label" for="dup-${id}">
              <code>${id}</code> ${questionText(id)}
            </label>
          </li>`
        )
        .join('');
      dupList.querySelectorAll('.dup-check').forEach((box) =>
        box.addEventListener('change', refreshEstimate)
      );
      // Default: nothing selected, so repeats are excluded unless opted in.
      refreshEstimate();
    }

    async function loadDuplicates() {
      duplicateIds = [];
      renderDuplicates();
      if (!pending.length || !channelSelect?.value) return;
      const result = await api('/api/publishing/duplicates', {
        method: 'POST',
        body: { question_ids: pending, channel_id: channelSelect.value },
      });
      if (result.ok && result.data) {
        duplicateIds = result.data.duplicates.map((row) => row.id);
        renderDuplicates();
      }
    }

    function syncMode() {
      const scheduling = canSchedule && modeSchedule?.checked;
      dedicatedBox?.classList.toggle('d-none', !scheduling);
      immediateNote?.classList.toggle('d-none', Boolean(scheduling));
      if (confirmBtn) {
        confirmBtn.innerHTML = scheduling
          ? '<i class="bi bi-calendar2-plus"></i> إنشاء الجدول المخصص'
          : '<i class="bi bi-send-fill"></i> نشر الآن';
      }
    }

    channelSelect?.addEventListener('change', () => { loadDuplicates(); syncMode(); });
    modeNow?.addEventListener('change', syncMode);
    modeSchedule?.addEventListener('change', syncMode);
    document.getElementById('bpDupAll')?.addEventListener('click', () => {
      dupList?.querySelectorAll('.dup-check').forEach((box) => { box.checked = true; });
      refreshEstimate();
    });
    document.getElementById('bpDupNone')?.addEventListener('click', () => {
      dupList?.querySelectorAll('.dup-check').forEach((box) => { box.checked = false; });
      refreshEstimate();
    });

    cancelBtn?.addEventListener('click', async () => {
      // Ask the server to stop; it halts after the message already in flight.
      cancelBtn.disabled = true;
      cancelBtn.innerHTML = '<span class="spinner-border spinner-border-sm"></span> جارٍ الإيقاف...';
      const result = await api('/api/publishing/cancel', { method: 'POST' });
      cancelBtn.disabled = false;
      cancelBtn.innerHTML = '<i class="bi bi-stop-circle"></i> إيقاف النشر';
      if (result.ok) toast('تم إرسال طلب الإيقاف؛ سيتوقف النشر بعد الرسالة الحالية.', 'warning');
      else toast(result.error || 'تعذّر إيقاف النشر', 'error');
    });

    // Stop polling whenever the modal closes, so a stale timer cannot linger.
    modalEl.addEventListener('hidden.bs.modal', () => {
      stopPolling();
      cancelBtn?.classList.add('d-none');
    });

    function stopPolling() {
      if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
    }

    function setRunning(running) {
      // While a run is active the confirm button is disabled and a cancel button
      // is offered, so the user can stop a long burst mid-flight.
      if (confirmBtn) confirmBtn.disabled = running;
      cancelBtn?.classList.toggle('d-none', !running);
      if (running) {
        confirmBtn.innerHTML = '<span class="spinner-border spinner-border-sm"></span> جارٍ النشر...';
        progressWrap?.classList.remove('d-none');
        progressText?.classList.remove('d-none');
      } else {
        progressText?.classList.add('d-none');
        syncMode();
      }
    }

    function startPolling(total) {
      stopPolling();
      pollTimer = setInterval(async () => {
        const result = await api('/api/publishing/progress');
        if (!result.ok || !result.data) return;
        const run = result.data.run;
        const done = run ? run.done : total;
        if (progressBar && total) progressBar.style.width = `${(done / total) * 100}%`;
        if (progressText) {
          const suffix = result.data.cancel_requested ? ' — جارٍ الإيقاف...' : '';
          progressText.textContent = `تم نشر ${done} من ${total}${suffix}`;
        }
        if (!result.data.running) stopPolling();
      }, Math.max(1000, interval * 1000));
    }

    async function publishNow(ids) {
      setRunning(true);
      startPolling(ids.length);

      const result = await api('/api/publishing/bulk', {
        method: 'POST',
        body: { question_ids: ids, channel_id: channelSelect?.value },
      });
      stopPolling();
      setRunning(false);
      if (progressBar) progressBar.style.width = '100%';

      if (result.ok && result.data) {
        const { succeeded, failed, cancelled, total } = result.data;
        if (cancelled) {
          toast(`أُوقف النشر: نُشر ${succeeded} من ${total}، وأُلغي ${cancelled}`, 'warning');
        } else if (failed) {
          toast(`تم نشر ${succeeded} من ${total} — فشل ${failed}`, 'warning');
        } else {
          toast(`تم نشر ${succeeded} سؤالاً بنجاح`);
        }
        bootstrap.Modal.getInstance(modalEl)?.hide();
        setTimeout(() => window.location.reload(), 1400);
      } else {
        toast(result.data?.error || result.error || 'فشل النشر', 'error');
      }
    }

    async function createDedicated(ids) {
      confirmBtn.disabled = true;
      confirmBtn.innerHTML = '<span class="spinner-border spinner-border-sm"></span> جارٍ الإنشاء...';
      const result = await api('/api/publishing/dedicated-schedule', {
        method: 'POST',
        body: {
          channel_id: channelSelect?.value,
          question_ids: ids,
          interval_minutes: Number(document.getElementById('bpInterval')?.value || 30),
          start_time: document.getElementById('bpStartTime')?.value || '',
          end_date: document.getElementById('bpEndDate')?.value || '',
          questions_per_run: Number(document.getElementById('bpPerRun')?.value || 1),
        },
      });
      confirmBtn.disabled = false;
      syncMode();
      if (result.ok && result.data) {
        toast(`تم إنشاء الجدول ${result.data.schedule_id} لـ ${result.data.queued} سؤالاً`);
        bootstrap.Modal.getInstance(modalEl)?.hide();
        setTimeout(() => window.location.reload(), 1200);
      } else {
        toast(result.data?.error || result.error || 'فشل إنشاء الجدول', 'error');
      }
    }

    confirmBtn?.addEventListener('click', async () => {
      if (!pending.length) return;
      if (!channelSelect?.value) { toast('اختر قناة أولاً.', 'warning'); return; }
      const ids = approvedIds();
      if (!ids.length) {
        toast('لا توجد أسئلة معتمدة للنشر (كل الأسئلة المكررة غير محددة).', 'warning');
        return;
      }
      if (canSchedule && modeSchedule?.checked) await createDedicated(ids);
      else await publishNow(ids);
    });

    // Exposed so the bulk bar can open the modal with the current selection.
    initBulkPublish.open = () => {
      pending = selectedIds();
      if (!pending.length) { toast('لم يتم تحديد أي سؤال.', 'warning'); return; }
      duplicateIds = [];
      const count = document.getElementById('bpCount');
      const list = document.getElementById('bpList');
      if (count) count.textContent = pending.length;
      if (progressWrap) progressWrap.classList.add('d-none');
      if (progressBar) progressBar.style.width = '0%';
      if (modeNow) modeNow.checked = true;
      if (list) {
        list.innerHTML = pending
          .map((id) => `<li><code>${id}</code> ${questionText(id)}</li>`)
          .join('');
      }
      renderDuplicates();
      syncMode();
      refreshEstimate();
      new bootstrap.Modal(modalEl).show();
      loadDuplicates();
    };
  }

  function bulkPublish() {
    if (initBulkPublish.open) initBulkPublish.open();
  }

  // ---------- Users admin page ----------
  function initUsersPage() {
    const modalEl = document.getElementById('userModal');
    if (!modalEl) return;
    const titleEl = document.getElementById('userModalTitle');
    const usernameWrap = document.getElementById('userUsernameWrap');
    const passwordWrap = document.getElementById('userPasswordWrap');
    const usernameInput = document.getElementById('uUsername');
    const passwordInput = document.getElementById('uPassword');
    const emailInput = document.getElementById('uEmail');
    const roleSelect = document.getElementById('uRole');
    const permsWrap = document.getElementById('userPermsWrap');
    const saveBtn = document.getElementById('userSave');
    let editing = ''; // '' => create, otherwise the username being edited

    const permChecks = () => Array.from(document.querySelectorAll('.perm-check'));
    const chosenPerms = () => permChecks().filter((c) => c.checked).map((c) => c.value);

    function syncRole() {
      // Admins implicitly hold every grant, so the checkboxes are moot for them.
      const isAdminRole = roleSelect.value === 'admin';
      permsWrap.style.opacity = isAdminRole ? '0.45' : '1';
      permChecks().forEach((c) => { c.disabled = isAdminRole; });
    }
    roleSelect?.addEventListener('change', syncRole);

    function openForm(user) {
      editing = user ? user.username : '';
      titleEl.innerHTML = user
        ? '<i class="bi bi-sliders"></i> تعديل الصلاحيات'
        : '<i class="bi bi-person-plus"></i> مستخدم جديد';
      usernameWrap.hidden = Boolean(user);
      passwordWrap.hidden = Boolean(user);
      usernameInput.value = user ? user.username : '';
      passwordInput.value = '';
      emailInput.value = user ? (user.email || '') : '';
      roleSelect.value = user ? user.role : 'user';
      const perms = user ? (user.permissions || []) : [];
      permChecks().forEach((c) => { c.checked = perms.includes(c.value); });
      syncRole();
      new bootstrap.Modal(modalEl).show();
    }

    initUsersPage.openCreate = () => openForm(null);

    saveBtn?.addEventListener('click', async () => {
      const payload = {
        email: emailInput.value.trim(),
        role: roleSelect.value,
        permissions: chosenPerms(),
      };
      let result;
      if (editing) {
        result = await api(`/api/users/${encodeURIComponent(editing)}`, {
          method: 'PUT',
          body: payload,
        });
      } else {
        result = await api('/api/users', {
          method: 'POST',
          body: {
            ...payload,
            username: usernameInput.value.trim(),
            password: passwordInput.value,
          },
        });
      }
      if (result.ok) {
        toast('تم الحفظ');
        bootstrap.Modal.getInstance(modalEl)?.hide();
        setTimeout(() => window.location.reload(), 700);
      } else {
        toast(result.error, 'error');
      }
    });

    // Exposed handlers used by inline onclick attributes in users.html.
    initUsersPage.edit = (user) => openForm(user);
    initUsersPage.remove = async (username) => {
      if (!confirm(`حذف المستخدم ${username}؟ لا يمكن التراجع.`)) return;
      const result = await api(`/api/users/${encodeURIComponent(username)}`, { method: 'DELETE' });
      if (result.ok) { toast('تم حذف المستخدم', 'warning'); setTimeout(() => window.location.reload(), 700); }
      else toast(result.error, 'error');
    };
    initUsersPage.resetPassword = async (username) => {
      const pwd = prompt(`كلمة مرور جديدة للمستخدم ${username} (8 أحرف على الأقل):`);
      if (pwd === null) return;
      const result = await api(`/api/users/${encodeURIComponent(username)}/password`, {
        method: 'POST',
        body: { new_password: pwd },
      });
      toast(result.ok ? 'تم تغيير كلمة المرور' : result.error, result.ok ? 'success' : 'error');
    };
  }

  function showUserForm() { initUsersPage.openCreate?.(); }
  function editUser(user) { initUsersPage.edit?.(user); }
  function deleteUser(username) { initUsersPage.remove?.(username); }
  function resetUserPassword(username) { initUsersPage.resetPassword?.(username); }

  // ---------- Userbot accounts ----------
  function userbotForm() {
    const form = document.getElementById('userbotForm');
    if (!form) return;
    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      const data = Object.fromEntries(new FormData(form).entries());
      const editing = form.dataset.id || '';
      const payload = {
        name: data.name,
        api_id: (data.api_id || '').trim(),
        api_hash: (data.api_hash || '').trim(),
        phone: (data.phone || '').trim(),
        role: data.role || 'primary',
      };
      const result = editing
        ? await api(`/api/userbots/${editing}`, { method: 'PUT', body: payload })
        : await api('/api/userbots', { method: 'POST', body: payload });
      if (result.ok) {
        toast(editing ? 'تم تحديث الحساب' : 'تمت إضافة الحساب');
        setTimeout(() => window.location.reload(), 700);
      } else {
        toast(result.error, 'error');
      }
    });
  }

  const showUserbotForm = (id) => {
    const panel = document.getElementById('userbotFormPanel');
    const form = document.getElementById('userbotForm');
    if (!panel || !form) return;
    form.reset();
    form.dataset.id = id || '';
    const title = document.getElementById('userbotFormTitle');
    if (title) title.textContent = id ? 'تعديل حساب userbot' : 'إضافة حساب userbot';
    if (id) {
      const source = document.querySelector(`[data-userbot='${id}']`);
      if (source) {
        form.name.value = source.dataset.name || '';
        form.phone.value = source.dataset.phone || '';
        form.role.value = source.dataset.role || 'primary';
        form.api_id.value = source.dataset.apiId || '';
      }
    }
    panel.hidden = false;
    panel.scrollIntoView({ behavior: 'smooth' });
  };
  const hideUserbotForm = () => {
    const panel = document.getElementById('userbotFormPanel');
    if (panel) panel.hidden = true;
  };

  const toggleUserbot = (id) => quickAction(`/api/userbots/${id}/toggle`, 'تغيير حالة الحساب');
  const setDefaultUserbot = (id) => quickAction(`/api/userbots/${id}/default`, 'تعيين الحساب الافتراضي');

  async function deleteUserbot(id) {
    if (!confirm('حذف هذا الحساب ومفاتيحه المشفّرة؟')) return;
    const result = await api(`/api/userbots/${id}`, { method: 'DELETE' });
    if (result.ok) { toast('تم حذف الحساب', 'warning'); setTimeout(() => window.location.reload(), 600); }
    else toast(result.error, 'error');
  }

  function userbotLoginBox(accountId) {
    let box = document.getElementById('userbotLoginBox');
    if (!box) {
      box = document.createElement('div');
      box.id = 'userbotLoginBox';
      box.className = 'panel';
      document.getElementById('userbotFormPanel')?.after(box);
    }
    box.innerHTML = `
      <div class="panel-header"><h2><i class="bi bi-box-arrow-in-right"></i> تسجيل دخول ${accountId}</h2></div>
      <div class="panel-body">
        <div class="mb-3">
          <label class="form-label">رقم الهاتف (بالصيغة الدولية)</label>
          <input class="form-control" id="ubLoginPhone" dir="ltr" placeholder="+9665xxxxxxx">
        </div>
        <button class="btn btn-primary btn-sm" id="ubSendCode">
          <i class="bi bi-send"></i> إرسال رمز الدخول
        </button>
        <div id="ubCodeBlock" hidden class="mt-3">
          <label class="form-label">رمز الدخول</label>
          <input class="form-control" id="ubLoginCode" dir="ltr" placeholder="12345">
          <label class="form-label mt-2">كلمة مرور الخطوة الثانية (2FA) <span class="hint">إن وُجدت</span></label>
          <input class="form-control" type="password" id="ubLoginPassword" dir="ltr">
          <button class="btn btn-success btn-sm mt-2" id="ubComplete">
            <i class="bi bi-check2-circle"></i> تأكيد الدخول
          </button>
        </div>
        <div id="ubLoginResult" class="mt-3"></div>
      </div>`;
    box.scrollIntoView({ behavior: 'smooth' });

    const report = (ok, html) => {
      const result = document.getElementById('ubLoginResult');
      result.innerHTML = `<div class="alert alert-${ok ? 'success' : 'danger'} small py-2 mb-0">${html}</div>`;
    };

    document.getElementById('ubSendCode').addEventListener('click', async () => {
      const phone = document.getElementById('ubLoginPhone').value.trim();
      const response = await api(`/api/userbots/${accountId}/login/start`, {
        method: 'POST', body: { phone },
      });
      if (response.ok) {
        report(true, 'تم إرسال الرمز إلى حسابك في تيليجرام. أدخل الرمز بالأسفل.');
        document.getElementById('ubCodeBlock').hidden = false;
      } else {
        report(false, response.error);
      }
    });

    document.getElementById('ubComplete').addEventListener('click', async () => {
      const code = document.getElementById('ubLoginCode').value.trim();
      const password = document.getElementById('ubLoginPassword').value;
      const response = await api(`/api/userbots/${accountId}/login/complete`, {
        method: 'POST', body: { code, password },
      });
      if (!response.ok) { report(false, response.error); return; }
      if (response.data.step === 'password') {
        report(true, response.data.message);
        document.getElementById('ubLoginPassword').focus();
        return;
      }
      report(true, 'تم تسجيل الدخول وحفظ الجلسة بنجاح.');
      toast('تم تسجيل دخول الحساب');
      setTimeout(() => window.location.reload(), 1200);
    });
  }

  async function testUserbot(id) {
    const result = await api(`/api/userbots/${id}/test`, { method: 'POST' });
    if (result.ok && result.data.ok) {
      toast(`الحساب متصل — @${result.data.account.username || result.data.account.id}`);
    } else {
      toast(result.data?.error || result.error || 'فشل اختبار الحساب', 'error');
    }
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

  function initMessagesForm() {
    const form = document.getElementById('messagesForm');
    if (!form) return;
    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      const messages = {};
      form.querySelectorAll('textarea[name]').forEach((el) => { messages[el.name] = el.value; });
      const result = await api('/api/settings', { method: 'PUT', body: { messages } });
      toast(result.ok ? 'تم حفظ الرسائل' : result.error, result.ok ? 'success' : 'error');
    });
    document.getElementById('messagesReset')?.addEventListener('click', () => {
      if (!confirm('استعادة النصوص الافتراضية؟ سيستبدل المحتوى الحالي بعد الحفظ.')) return;
      // Each textarea carries its shipped default in the placeholder attribute.
      form.querySelectorAll('textarea[name]').forEach((el) => { el.value = el.placeholder || ''; });
      toast('تمت استعادة النص الافتراضي — اضغط «حفظ الرسائل» للتأكيد', 'warning');
    });
  }

  function initNotifyForm() {
    const form = document.getElementById('notifyForm');
    if (!form) return;
    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      const data = new FormData(form);
      const types = {};
      form.querySelectorAll('input[type=checkbox][name]').forEach((el) => {
        if (el.name !== 'notify_on_publish') types[el.name] = el.checked;
      });
      const result = await api('/api/settings', {
        method: 'PUT',
        body: {
          notifications: { on_publish: data.get('notify_on_publish') === 'on', types },
        },
      });
      toast(result.ok ? 'تم حفظ إعدادات الإشعارات' : result.error, result.ok ? 'success' : 'error');
    });
  }

  function initSettingsForms() {
    initTokenForm();
    userbotForm();
    initMessagesForm();
    initNotifyForm();
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
        pacing: {
          interval_seconds: Number(form.get('pacing_interval') || 0),
          max_wait_seconds: Number(form.get('pacing_max_wait') || 0),
        },
        analytics: {
          auto_collect: form.get('analytics_auto_collect') === 'on',
          auto_collect_hours: Number(form.get('analytics_auto_collect_hours') || 6),
        },
        transport: {
          default_mode: form.get('default_transport') || 'bot',
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

  // --- exam platform (admin) ----------------------------------------------
  function initExamAnalytics() {
    const main = document.getElementById('eaMain');
    const offcanvasEl = document.getElementById('examListOffcanvas');
    if (!main) return;

    const palette = {
      grid: { color: 'rgba(148,163,184,0.12)' },
      tick: { color: '#94a3b8' },
      bar: 'rgba(99,102,241,0.65)',
    };
    const axis = () => ({
      y: { beginAtZero: true, ticks: { precision: 0, color: '#94a3b8' }, grid: palette.grid },
      x: { ticks: palette.tick, grid: palette.grid },
    });

    // Charts are described by a <script type="application/json"> island rather
    // than inline JS: the partial is parsed inside the page body, before
    // Chart.js loads, so running it there would throw on a full page load.
    const renderCharts = (root) => {
      if (!window.Chart) return;
      (TQM._examCharts || []).forEach((c) => { try { c.destroy(); } catch (e) {} });
      TQM._examCharts = [];
      const mk = (id, cfg) => {
        const el = root.querySelector('#' + id);
        if (!el) return;
        TQM._examCharts.push(new Chart(el, cfg));
      };
      const overview = root.querySelector('script[data-ea-chart="overview"]');
      if (overview) {
        const buckets = JSON.parse(overview.textContent || '{}');
        mk('overviewScoreChart', {
          type: 'bar',
          data: {
            labels: Object.keys(buckets),
            datasets: [{ label: 'عدد الطلاب', data: Object.values(buckets), backgroundColor: palette.bar, borderRadius: 8 }],
          },
          options: { responsive: true, plugins: { legend: { display: false } }, scales: axis() },
        });
        return;
      }
      const exam = root.querySelector('script[data-ea-chart="exam"]');
      if (!exam) return;
      const stats = JSON.parse(exam.textContent || '{}');
      mk('eaScoreChart', {
        type: 'bar',
        data: {
          labels: Object.keys(stats.buckets || {}),
          datasets: [{ label: 'عدد الطلاب', data: Object.values(stats.buckets || {}), backgroundColor: palette.bar, borderRadius: 8 }],
        },
        options: { responsive: true, plugins: { legend: { display: false } }, scales: axis() },
      });
      mk('eaPassChart', {
        type: 'doughnut',
        data: {
          labels: ['ناجح', 'راسب'],
          datasets: [{ data: [stats.pass_rate, Math.max(0, 100 - stats.pass_rate)], backgroundColor: ['#22c55e', '#ef4444'], borderWidth: 0 }],
        },
        options: { plugins: { legend: { position: 'bottom', labels: { color: '#cbd5e1' } } } },
      });
      mk('eaCorrectChart', {
        type: 'doughnut',
        data: {
          labels: ['صحيحة', 'خاطئة / بلا إجابة'],
          datasets: [{ data: [stats.correct_rate, Math.max(0, 100 - stats.correct_rate)], backgroundColor: ['#06b6d4', '#f59e0b'], borderWidth: 0 }],
        },
        options: { plugins: { legend: { position: 'bottom', labels: { color: '#cbd5e1' } } } },
      });
    };

    // Highlight the picked exam in *both* the desktop sidebar and the
    // offcanvas list, since the two are separate DOM trees.
    const markActive = (examId) => {
      document.querySelectorAll('.ea-exam-item').forEach((item) => {
        const own = item.dataset.examId || '';
        item.classList.toggle('active', own === (examId || ''));
      });
    };

    renderCharts(main);

    document.body.addEventListener('htmx:afterSwap', (event) => {
      if (event.target !== main) return;
      const detail = main.querySelector('.ea-detail');
      markActive(detail ? detail.dataset.examId : '');
      renderCharts(main);
      // On mobile the picker is an offcanvas; close it so the result is visible.
      if (window.bootstrap && offcanvasEl) {
        const inst = bootstrap.Offcanvas.getInstance(offcanvasEl);
        if (inst) inst.hide();
      }
    });

    document.body.addEventListener('htmx:historyRestore', () => {
      const detail = main.querySelector('.ea-detail');
      markActive(detail ? detail.dataset.examId : '');
      renderCharts(main);
    });

    // A 4xx (e.g. the exam was deleted meanwhile) would otherwise swap nothing
    // and leave the click looking broken.
    document.body.addEventListener('htmx:responseError', (event) => {
      if (!event.detail || !event.detail.target || event.detail.target.id !== 'eaMain') return;
      toast('تعذّر تحميل تحليل الاختبار. حدّث الصفحة وحاول مرة أخرى.', 'error');
    });
  }

  async function examAction(examId, action) {
    if (action === 'delete') return examDelete(examId);
    const labels = {
      publish: 'نشر الاختبار', notify: 'إشعار الطلاب', close: 'إغلاق الاختبار',
      reopen: 'إعادة فتح الاختبار', archive: 'أرشفة الاختبار',
    };
    const result = await api(`/api/admin/exams/${examId}/${action}`, { method: 'POST' });
    if (result.ok) {
      const data = result.data || {};
      let suffix = '';
      if (action === 'publish' || action === 'notify') {
        const enqueued = data.enqueued ?? data.notified ?? 0;
        const sent = data.sent || 0;
        const failed = data.failed || 0;
        suffix = enqueued
          ? ` — تم إرسال ${sent} من ${enqueued}`
          : ' — لا يوجد طلاب جدد لإشعارهم';
        if (failed) suffix += ` (تعذّر ${failed})`;
      }
      toast(`${labels[action] || action}: تم بنجاح${suffix}`);
      setTimeout(() => window.location.reload(), 900);
    } else {
      toast(result.error, 'error');
    }
  }

  async function examDelete(examId, force = false) {
    const warning = force
      ? `حذف الاختبار ${examId} مع كل محاولات ونتائج الطلاب؟ لا يمكن التراجع.`
      : `حذف الاختبار ${examId}؟ لا يمكن التراجع.`;
    if (!confirm(warning)) return;
    const url = `/api/admin/exams/${examId}${force ? '?force=true' : ''}`;
    const result = await api(url, { method: 'DELETE' });
    if (result.ok) {
      toast('حذف الاختبار: تم بنجاح');
      setTimeout(() => { window.location.href = '/exams'; }, 700);
      return;
    }
    // An exam with attempts is refused without force; offer the cascade explicitly.
    if (!force && /محاولات/.test(result.error || '')) {
      if (confirm(`${result.error}\n\nحذف الاختبار وكل محاولات الطلاب نهائياً؟`)) {
        await examDelete(examId, true);
      }
      return;
    }
    toast(result.error, 'error');
  }

  function initExamForm() {
    const form = document.getElementById('examForm');
    if (!form) return;
    const accessMode = document.getElementById('accessMode');
    const window_box = document.getElementById('scheduleWindow');
    const syncWindow = () => { window_box.hidden = accessMode.value !== 'scheduled'; };
    accessMode?.addEventListener('change', syncWindow);
    syncWindow();

    // Question picker: live search/category filter and selection counter.
    const search = document.getElementById('qPickerSearch');
    const category = document.getElementById('qPickerCategory');
    const items = () => Array.from(document.querySelectorAll('.q-pick-item'));
    const applyFilter = () => {
      const text = (search?.value || '').trim().toLowerCase();
      const cat = category?.value || '';
      items().forEach((item) => {
        const matchesText = !text || (item.dataset.text || '').includes(text);
        const matchesCat = !cat || item.dataset.category === cat;
        item.hidden = !(matchesText && matchesCat);
      });
    };
    search?.addEventListener('input', applyFilter);
    category?.addEventListener('change', applyFilter);

    const checks = () => Array.from(document.querySelectorAll('.exam-q-check'));
    const updateCount = () => {
      const count = checks().filter((c) => c.checked).length;
      const label = document.getElementById('selectedCount');
      if (label) label.textContent = count;
    };
    document.addEventListener('change', (event) => {
      if (event.target.classList?.contains('exam-q-check')) updateCount();
    });
    updateCount();

    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      const ids = checks().filter((c) => c.checked).map((c) => c.value);
      if (!ids.length) { toast('اختر سؤالاً واحداً على الأقل.', 'error'); return; }
      const data = new FormData(form);
      const payload = {
        title: data.get('title') || '',
        description: data.get('description') || '',
        category: data.get('category') || 'General',
        cover_image: data.get('cover_image') || '',
        access_mode: data.get('access_mode') || 'always',
        start_date: data.get('start_date') || '',
        start_time: data.get('start_time') || '',
        end_date: data.get('end_date') || '',
        end_time: data.get('end_time') || '',
        timezone: data.get('timezone') || 'Asia/Riyadh',
        duration_minutes: Number(data.get('duration_minutes') || 0),
        max_attempts: Number(data.get('max_attempts') || 0),
        status: data.get('status') || 'draft',
        allow_retake: data.get('allow_retake') === 'on',
        count_abandoned_attempts: data.get('count_abandoned_attempts') === 'on',
        shuffle_questions: data.get('shuffle_questions') === 'on',
        shuffle_options: data.get('shuffle_options') === 'on',
        show_results: data.get('show_results') === 'on',
        show_solutions_after_submit: data.get('show_solutions_after_submit') === 'on',
        question_ids: ids,
      };
      const examId = form.dataset.examId;
      const url = examId ? `/api/admin/exams/${examId}` : '/api/admin/exams';
      const result = await api(url, { method: examId ? 'PUT' : 'POST', body: payload });
      if (result.ok) {
        toast('تم الحفظ بنجاح');
        setTimeout(() => { window.location.href = `/exams/${result.data.id}`; }, 600);
      } else {
        toast(result.error, 'error');
      }
    });
  }

  function selectAllVisible() {
    document.querySelectorAll('.q-pick-item').forEach((item) => {
      if (!item.hidden) {
        const check = item.querySelector('.exam-q-check');
        if (check) check.checked = true;
      }
    });
    const label = document.getElementById('selectedCount');
    if (label) label.textContent = document.querySelectorAll('.exam-q-check:checked').length;
  }

  function clearExamQuestions() {
    document.querySelectorAll('.exam-q-check').forEach((c) => { c.checked = false; });
    const label = document.getElementById('selectedCount');
    if (label) label.textContent = '0';
  }

  // ---------- Live student activity ----------
  // Polls the activity feed while the exam detail / attempts page is open and
  // re-renders the list. Action-based: the server only counts a student whose
  // last action is within the TTL, so nobody idle is shown as active.
  function initLiveActivity() {
    const panel = document.getElementById('livePanel');
    const list = document.getElementById('liveList');
    if (!panel || !list) return;
    const examId = panel.dataset.examId || '';
    const esc = (v) => String(v ?? '').replace(/[&<>"']/g, (c) => (
      { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
    ));
    let timer = null;

    function ago(seconds) {
      const s = Math.max(0, Number(seconds) || 0);
      if (s < 60) return `قبل ${s} ثانية`;
      const m = Math.floor(s / 60);
      return `قبل ${m} دقيقة`;
    }

    function paint(data) {
      const countEl = document.getElementById('liveActiveCount');
      if (countEl) countEl.textContent = data.count;
      const solvedEl = document.getElementById('liveSolvedCount');
      if (solvedEl) solvedEl.textContent = data.solved_now ?? 0;
      list.innerHTML = '';
      if (!data.active || !data.active.length) {
        list.innerHTML = '<p class="empty-state py-3" id="liveEmpty"><i class="bi bi-people"></i><br>لا يوجد طلاب نشطون الآن.</p>';
        return;
      }
      data.active.forEach((row) => {
        const node = document.createElement('div');
        node.className = 'live-row';
        const exam = row.exam_title ? `<span class="live-meta">${esc(row.exam_title)}</span>` : '';
        node.innerHTML = `<span class="online-dot"></span>
          <span class="live-name">${esc(row.student_name)}</span>
          <span class="badge text-bg-warning">${esc(row.event_label)}</span>
          ${exam}
          <span class="live-meta">${esc(ago(row.seconds_ago))}</span>`;
        list.appendChild(node);
      });
    }

    async function refresh() {
      const query = examId ? `?exam_id=${encodeURIComponent(examId)}` : '';
      const result = await api(`/api/admin/activity${query}`);
      if (result.ok) paint(result.data);
    }

    async function tick() {
      // A hidden tab does not need to keep polling; resume on visibilitychange.
      if (document.hidden) return;
      await refresh();
    }

    refresh();
    timer = setInterval(tick, 10000);
    document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(); });
    window.addEventListener('beforeunload', () => { if (timer) clearInterval(timer); });
  }

  return {
    api, toast, quickAction, publishNow, previewQuestion, duplicateQuestion, deleteQuestion,
    bulk, initQuestionTable, initImportForm, initEditor, initSidebar,
    initChannelForm, showChannelForm, hideChannelForm, testChannel, toggleChannel,
    setDefaultChannel, deleteChannel,
    initScheduleForm, showScheduleForm, hideScheduleForm, scheduleAction,
    retryPublish, initSettingsForms, restoreBackup, revealToken,
    collectAnalytics, showPollDetail, initManualPublish,
    initBulkPublish, bulkPublish,
    initUsersPage, showUserForm, editUser, deleteUser, resetUserPassword,
    userbotForm, showUserbotForm, hideUserbotForm, toggleUserbot, setDefaultUserbot,
    deleteUserbot, userbotLoginBox, testUserbot,
    examAction, examDelete, initExamForm, initExamAnalytics, selectAllVisible, clearExamQuestions,
    initLiveActivity,
  };
})();
