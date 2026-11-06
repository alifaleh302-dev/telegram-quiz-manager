/* Student Mini App — single-page controller.
 *
 * Identity: on load we read Telegram.WebApp.initData (the signed payload) and
 * exchange it for a short-lived student token. Every request then carries the
 * token; mutating requests also carry the raw initData so the server can prove
 * the call comes from a live Telegram session. The client never sends its own
 * user id — the server derives it from the signature.
 */
const SA = (() => {
  const tg = window.Telegram?.WebApp;
  let studentToken = '';
  let initData = '';
  let state = { view: 'home', stack: [], exam: null, session: null, timer: null, sessionCleanup: null };

  // --- low-level -----------------------------------------------------------
  function headers(write = false) {
    const h = { 'X-Student-Token': studentToken };
    if (write && initData) h['X-Telegram-Init-Data'] = initData;
    return h;
  }

  async function api(path, { method = 'GET', body, write = false, keepalive = false } = {}) {
    const options = { method, headers: headers(write) };
    if (keepalive) options.keepalive = true;
    if (body !== undefined) {
      options.headers['Content-Type'] = 'application/json';
      options.body = JSON.stringify(body);
    }
    let response;
    try {
      response = await fetch(path, options);
    } catch {
      throw new Error('تعذّر الاتصال بالخادم.');
    }
    const text = await response.text();
    let data = {};
    if (text) {
      try { data = JSON.parse(text); } catch { data = { error: text }; }
    }
    if (!response.ok) {
      throw new Error(data.detail || data.error || `فشل الطلب (${response.status}).`);
    }
    return data;
  }

  function el(html) {
    const t = document.createElement('template');
    t.innerHTML = html.trim();
    return t.content.firstElementChild;
  }
  function esc(value) {
    return String(value ?? '').replace(/[&<>"']/g, (c) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[c]));
  }
  function alertMsg(message) {
    const box = document.getElementById('saAlert');
    if (!message) { box.hidden = true; return; }
    box.textContent = message;
    box.hidden = false;
    setTimeout(() => { box.hidden = true; }, 4000);
  }
  function setHeader(title, subtitle, showBack) {
    document.getElementById('saTitle').textContent = title;
    document.getElementById('saSubtitle').textContent = subtitle || '';
    document.getElementById('saBack').hidden = !showBack;
  }
  const view = () => document.getElementById('saView');
  function render(node) {
    // Any render replaces the current view, so tear down the previous session's
    // timer/listeners first — otherwise a hidden interval would keep running.
    if (state.sessionCleanup) state.sessionCleanup();
    const v = view();
    v.innerHTML = '';
    v.appendChild(node);
  }
  const fmtPct = (v) => `${Math.round(Number(v) || 0)}%`;
  function fmtDuration(seconds) {
    const s = Math.max(0, Math.floor(Number(seconds) || 0));
    const m = Math.floor(s / 60);
    return `${m}:${String(s % 60).padStart(2, '0')}`;
  }

  // --- bootstrap -----------------------------------------------------------
  async function boot() {
    if (tg) { tg.ready(); tg.expand(); applyTheme(); }
    initData = tg?.initData || '';
    try {
      if (!initData) {
        // Opened outside Telegram (or an old client): show a clear message
        // instead of a blank screen.
        render(emptyState('bi-telegram', 'افتح التطبيق من داخل تيليجرام', 'هذه الصفحة تعمل كتطبيق مصغّر داخل تيليجرام فقط.'));
        document.getElementById('saTabs').hidden = true;
        return;
      }
      const auth = await fetch('/api/student/auth', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ init_data: initData }),
      });
      const data = await auth.json();
      if (!auth.ok) throw new Error(data.detail || 'فشل تسجيل الدخول.');
      studentToken = data.token;
      await refreshBadge();
      await go('home');
    } catch (error) {
      render(emptyState('bi-exclamation-triangle', 'تعذّر الدخول', error.message));
    }
  }

  const THEME_KEY = 'sa-theme';
  function themeMode() {
    return document.documentElement.getAttribute('data-theme') === 'light' ? 'light' : 'dark';
  }
  function setTheme(mode, persist) {
    const next = mode === 'light' ? 'light' : 'dark';
    document.documentElement.setAttribute('data-theme', next);
    if (persist) {
      try { localStorage.setItem(THEME_KEY, next); } catch { /* private mode */ }
    }
  }
  // The inline <head> script sets the initial theme. Here we only follow the
  // Telegram client's colour scheme while the student has made no explicit
  // choice; an explicit choice is remembered across sessions.
  function applyTheme() {
    let saved = null;
    try { saved = localStorage.getItem(THEME_KEY); } catch { /* ignore */ }
    if (saved === 'light' || saved === 'dark') return;
    setTheme(tg?.colorScheme === 'light' ? 'light' : 'dark', false);
  }

  async function refreshBadge() {
    try {
      const data = await api('/api/student/notifications');
      const badge = document.getElementById('saBellBadge');
      badge.textContent = data.unread;
      badge.hidden = !data.unread;
    } catch { /* badge is non-critical */ }
  }

  // --- navigation ----------------------------------------------------------
  async function go(tab) {
    state.view = tab;
    document.querySelectorAll('#saTabs button').forEach((b) =>
      b.classList.toggle('active', b.dataset.tab === tab));
    document.getElementById('saBack').hidden = true;
    const loaders = {
      home: loadHome, exams: loadExams, results: loadResults,
      notifications: loadNotifications, settings: loadSettings,
    };
    render(loading());
    try {
      await loaders[tab]();
    } catch (error) {
      render(emptyState('bi-exclamation-triangle', 'خطأ', error.message));
    }
  }

  const loading = () => el('<div class="sa-loading"><div class="spinner-border text-primary" role="status"></div></div>');
  function emptyState(icon, title, text) {
    return el(`<div class="sa-empty"><i class="bi ${icon}"></i><h3>${esc(title)}</h3><p>${esc(text || '')}</p></div>`);
  }
  function statCard(value, label) {
    return el(`<div class="sa-stat"><div class="value">${esc(value)}</div><div class="label">${esc(label)}</div></div>`);
  }
  function chip(label, cls) {
    return `<span class="sa-chip ${cls}">${esc(label)}</span>`;
  }

  // --- home ----------------------------------------------------------------
  async function loadHome() {
    const data = await api('/api/student/dashboard');
    const s = data.summary;
    const wrap = el('<div></div>');
    const stats = el('<div class="sa-stats"></div>');
    stats.append(
      statCard(s.total_available, 'اختبارات متاحة'),
      statCard(s.completed_exams, 'اختبارات مكتملة'),
      statCard(s.new_exams, 'اختبارات جديدة'),
      statCard(fmtPct(s.average_percentage), 'متوسط النتائج'),
    );
    wrap.appendChild(stats);

    if (data.continue.length) {
      wrap.appendChild(sectionTitle('أكمل ما بدأته'));
      data.continue.forEach((e) => wrap.appendChild(examCard(e)));
    }
    if (data.new_exams.length) {
      wrap.appendChild(sectionTitle('اختبارات جديدة'));
      data.new_exams.forEach((e) => wrap.appendChild(examCard(e)));
    }
    if (data.upcoming.length) {
      wrap.appendChild(sectionTitle('قادمة'));
      data.upcoming.forEach((e) => wrap.appendChild(examCard(e)));
    }
    if (!data.continue.length && !data.new_exams.length && !data.upcoming.length) {
      wrap.appendChild(emptyState('bi-inbox', 'لا توجد اختبارات متاحة حالياً', 'سيظهر هنا أي اختبار جديد.'));
    }
    setHeader('اختباراتي', 'بوابة الطالب', false);
    render(wrap);
  }

  function sectionTitle(text, action) {
    const node = el(`<div class="sa-section-title"><h2>${esc(text)}</h2></div>`);
    if (action) {
      const button = el(`<button>${esc(action.label)}</button>`);
      button.addEventListener('click', action.onClick);
      node.appendChild(button);
    }
    return node;
  }

  function statusChip(exam) {
    if (exam.label === 'in_progress') return chip('قيد التنفيذ', 'in_progress');
    if (exam.label === 'new') return chip('جديد', 'new');
    if (exam.state === 'upcoming') return chip('لم يبدأ', 'upcoming');
    if (exam.state === 'closed' || exam.state === 'archived') return chip('منتهي', 'closed');
    if (exam.attempts_submitted > 0) return chip('مكتمل', 'completed');
    return chip('متاح', 'available');
  }

  function examCard(exam) {
    const node = el(`
      <div class="sa-card sa-exam">
        <div class="sa-exam-head">
          <h3>${esc(exam.title)}</h3>
          ${statusChip(exam)}
        </div>
        <div class="sa-meta">
          <span><i class="bi bi-list-ol"></i> ${exam.question_count} سؤال</span>
          ${exam.duration_minutes ? `<span><i class="bi bi-clock"></i> ${exam.duration_minutes} دقيقة</span>` : ''}
          ${exam.category ? `<span><i class="bi bi-tag"></i> ${esc(exam.category)}</span>` : ''}
          ${exam.best_percentage !== null && exam.best_percentage !== undefined
            ? `<span><i class="bi bi-star"></i> أفضل نتيجة ${fmtPct(exam.best_percentage)}</span>` : ''}
        </div>
        <button class="sa-btn">${exam.label === 'in_progress' ? 'متابعة' : 'عرض التفاصيل'}</button>
      </div>`);
    node.querySelector('button').addEventListener('click', () => openExam(exam.id));
    return node;
  }

  // --- exam detail ---------------------------------------------------------
  async function openExam(examId) {
    render(loading());
    try {
      const data = await api(`/api/student/exams/${examId}`);
      const exam = data.exam;
      const wrap = el('<div></div>');
      wrap.appendChild(el(`
        <div class="sa-card">
          <h3>${esc(exam.title)}</h3>
          <div class="sa-meta" style="margin:8px 0">
            <span><i class="bi bi-list-ol"></i> ${exam.question_count} سؤال</span>
            ${exam.duration_minutes ? `<span><i class="bi bi-clock"></i> ${exam.duration_minutes} دقيقة</span>` : ''}
            <span><i class="bi bi-arrow-repeat"></i> ${exam.max_attempts === 0 ? 'محاولات غير محدودة' : `${exam.max_attempts} محاولة`}</span>
          </div>
          <p class="sa-note">${esc(exam.description || 'لا يوجد وصف.')}</p>
        </div>`));

      if (exam.starts_at || exam.ends_at) {
        wrap.appendChild(el(`<div class="sa-card"><h3>النافذة الزمنية</h3>
          <p class="sa-note">${exam.starts_at ? 'تبدأ: ' + esc(exam.starts_at) : ''}
          ${exam.ends_at ? '<br>تنتهي: ' + esc(exam.ends_at) : ''}</p></div>`));
      }

      if (data.attempts.length) {
        wrap.appendChild(sectionTitle('محاولاتك السابقة'));
        data.attempts.forEach((a) => wrap.appendChild(attemptRow(a, exam)));
      }

      const action = el('<button class="sa-btn"></button>');
      const eligibility = await api(`/api/student/exams/${examId}/eligibility`);
      if (eligibility.active_attempt_id) {
        action.textContent = 'متابعة المحاولة';
        action.addEventListener('click', () => resumeAttempt(eligibility.active_attempt_id));
      } else if (eligibility.can_start) {
        action.textContent = 'بدء الاختبار';
        action.addEventListener('click', () => confirmStart(exam));
      } else {
        action.textContent = eligibility.reason || 'غير متاح';
        action.disabled = true;
      }
      wrap.appendChild(action);

      setHeader(exam.title, 'تفاصيل الاختبار', true);
      render(wrap);
    } catch (error) {
      render(emptyState('bi-exclamation-triangle', 'خطأ', error.message));
    }
  }

  function attemptRow(attempt, exam) {
    const node = el(`
      <div class="sa-card sa-exam">
        <div class="sa-exam-head">
          <h3>محاولة رقم ${attempt.attempt_number}</h3>
          <span class="sa-chip ${attempt.status === 'submitted' ? 'completed' : 'closed'}">${attempt.status === 'submitted' ? 'مكتملة' : 'منتهية'}</span>
        </div>
        <div class="sa-meta">
          <span><i class="bi bi-percent"></i> ${fmtPct(attempt.percentage)}</span>
          <span><i class="bi bi-check2"></i> ${attempt.correct_count} صحيحة</span>
          <span><i class="bi bi-x"></i> ${attempt.wrong_count} خاطئة</span>
        </div>
        <button class="sa-btn secondary">مراجعة</button>
      </div>`);
    node.querySelector('button').addEventListener('click', () => openReview(attempt.id));
    return node;
  }

  function confirmStart(exam) {
    openModal('بدء الاختبار', `<p class="sa-note">سيبدأ المؤقّت بمجرد الضغط على «ابدأ».
      ${exam.duration_minutes ? `مدة الاختبار ${exam.duration_minutes} دقيقة.` : 'لا يوجد حد زمني.'}</p>`,
      [{ label: 'ابدأ', onClick: () => startAttempt(exam.id) }, { label: 'إلغاء', secondary: true }]);
  }

  async function startAttempt(examId) {
    closeModal();
    render(loading());
    try {
      const data = await api(`/api/student/exams/${examId}/start`, { method: 'POST', write: true });
      openSession(data);
    } catch (error) {
      render(emptyState('bi-exclamation-triangle', 'تعذّر البدء', error.message));
    }
  }

  async function resumeAttempt(attemptId) {
    render(loading());
    try {
      const data = await api(`/api/student/attempts/${attemptId}`);
      openSession(data);
    } catch (error) {
      render(emptyState('bi-exclamation-triangle', 'خطأ', error.message));
    }
  }

  // --- session -------------------------------------------------------------
  function openSession(data) {
    const questions = data.questions;
    const answers = {};
    // Resume at the last question the student answered, so reopening a session
    // lands where they stopped instead of at the first question.
    let index = 0;
    questions.forEach((q, i) => {
      if (q.selected_option !== null && q.selected_option !== undefined) {
        answers[q.question_id] = q.selected_option;
        index = i;
      }
    });

    const wrap = el('<div class="sa-session"></div>');
    const top = el(`<div class="sa-card">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
        <span class="sa-note" id="saCounter"></span>
        <span class="sa-timer" id="saTimer"></span>
      </div>
      <div class="sa-progress"><div id="saBar" style="width:0%"></div></div>
      <p class="sa-note" id="saResumeNote" hidden></p>
    </div>`);
    const body = el('<div class="sa-card"></div>');
    const hint = el(`<p class="sa-nav-hint"><i class="bi bi-hand-index-thumb"></i>
      للتنقل بين الأسئلة اسحب يميناً أو يساراً، أو اضغط وسط حافة الشاشة ثم السهم.</p>`);
    const submitBar = el(`<div class="sa-submit-bar">
      <button class="sa-btn sa-submit" id="saSubmit"><i class="bi bi-send-check"></i> تسليم الاختبار</button>
    </div>`);
    const sideNav = el(`<div class="sa-side-nav" id="saSideNav">
      <div class="sa-side-zone sa-side-zone-prev" id="saZonePrev"></div>
      <div class="sa-side-zone sa-side-zone-next" id="saZoneNext"></div>
      <button class="sa-side-arrow sa-side-arrow-prev" id="saArrowPrev" aria-label="السؤال السابق"><i class="bi bi-chevron-right"></i></button>
      <button class="sa-side-arrow sa-side-arrow-next" id="saArrowNext" aria-label="السؤال التالي"><i class="bi bi-chevron-left"></i></button>
    </div>`);
    wrap.append(top, body, hint, submitBar, sideNav);
    setHeader(data.exam.title, 'قيد التنفيذ', false);
    render(wrap);

    const timerEl = document.getElementById('saTimer');
    const counterEl = document.getElementById('saCounter');
    const barEl = document.getElementById('saBar');
    const sideNavEl = document.getElementById('saSideNav');
    const resumeNote = document.getElementById('saResumeNote');
    const answeredCount = Object.keys(answers).length;
    if (answeredCount > 0 && index > 0) {
      resumeNote.textContent = `تم استئناف جلستك من السؤال ${index + 1} (آخر سؤال أجبت عنه).`;
      resumeNote.hidden = false;
    }

    function paint() {
      const q = questions[index];
      counterEl.textContent = `السؤال ${index + 1} من ${questions.length}`;
      barEl.style.width = `${Math.round(((index + 1) / questions.length) * 100)}%`;
      body.innerHTML = `<div class="sa-question-text">${esc(q.question)}</div>`;
      q.options.forEach((option, optIndex) => {
        const optionNode = el(`<button class="sa-option ${answers[q.question_id] === optIndex ? 'selected' : ''}">
          <span class="key">${String.fromCharCode(65 + optIndex)}</span>
          <span>${esc(option)}</span></button>`);
        optionNode.addEventListener('click', () => {
          answers[q.question_id] = optIndex;
          api(`/api/student/attempts/${data.attempt.id}/answers`, {
            method: 'POST', write: true,
            body: { question_id: q.question_id, selected_option: optIndex },
          }).catch((error) => alertMsg(error.message));
          paint();
        });
        body.appendChild(optionNode);
      });
      const first = index === 0;
      const last = index === questions.length - 1;
      sideNavEl.classList.toggle('hide-prev', first);
      sideNavEl.classList.toggle('hide-next', last);
    }

    function move(delta) {
      const next = index + delta;
      if (next < 0 || next >= questions.length) return;
      index = next;
      animate(delta);
      paint();
    }

    // Swipe gestures are an *addition* to the edge arrows below, which keep
    // working exactly as before. In this RTL layout the next question sits to
    // the left, so a rightward drag (dx > 0) reveals it and a leftward drag
    // goes back — the mirror of a LTR carousel.
    function bindSwipe() {
      const THRESHOLD = 45;
      let startX = 0;
      let startY = 0;
      let tracking = false;
      body.addEventListener('touchstart', (event) => {
        if (event.touches.length !== 1) { tracking = false; return; }
        tracking = true;
        startX = event.touches[0].clientX;
        startY = event.touches[0].clientY;
      }, { passive: true });
      body.addEventListener('touchend', (event) => {
        if (!tracking) return;
        tracking = false;
        const touch = event.changedTouches[0];
        if (!touch) return;
        const dx = touch.clientX - startX;
        const dy = touch.clientY - startY;
        // Ignore short taps and vertical drags so scrolling is never hijacked.
        if (Math.abs(dx) < THRESHOLD || Math.abs(dx) <= Math.abs(dy)) return;
        move(dx > 0 ? 1 : -1);
      }, { passive: true });
    }

    // A short slide so a swipe reads as a page change rather than a redraw.
    function animate(delta) {
      body.style.animation = 'none';
      void body.offsetWidth;
      body.style.animation = `${delta > 0 ? 'saSlideNext' : 'saSlidePrev'} .22s ease`;
    }

    // Edge arrows stay hidden until the student taps the side of the screen:
    // the first tap reveals the arrow, a tap while revealed navigates.
    function bindSideNav() {
      let timer = null;
      [['saZonePrev', 'revealed-prev', -1], ['saZoneNext', 'revealed-next', 1]].forEach(
        ([zoneId, cls, delta]) => {
          document.getElementById(zoneId).addEventListener('click', () => {
            const revealed = sideNavEl.classList.contains(cls);
            clearTimeout(timer);
            sideNavEl.classList.remove('revealed-prev', 'revealed-next');
            sideNavEl.classList.add(cls);
            timer = setTimeout(() => sideNavEl.classList.remove(cls), 3000);
            if (revealed) move(delta);
          });
        },
      );
    }

    // --- countdown: paused while the student is away ---------------------
    const attemptId = data.attempt.id;
    const hasTimer = data.attempt.remaining_seconds !== null
      && data.attempt.remaining_seconds !== undefined;
    let remaining = hasTimer ? Math.max(0, Math.floor(Number(data.attempt.remaining_seconds))) : null;
    let paused = false;
    let finished = false;

    function paintTimer() {
      if (!hasTimer) return;
      timerEl.textContent = fmtDuration(remaining);
      timerEl.classList.toggle('warning', remaining < 60);
    }

    function startTicker() {
      if (!hasTimer || paused) return;
      if (state.timer) clearInterval(state.timer);
      paintTimer();
      state.timer = setInterval(() => {
        if (paused || finished) return;
        remaining = Math.max(0, remaining - 1);
        paintTimer();
        if (remaining <= 0) {
          clearInterval(state.timer);
          state.timer = null;
          submitAttempt(attemptId);
        }
      }, 1000);
    }

    function stopTicker() {
      if (state.timer) { clearInterval(state.timer); state.timer = null; }
    }

    // Freeze the clock on the server and locally the moment the student leaves
    // the screen; the balance is re-based on resume, so no time is lost.
    function pauseTimer() {
      // Submitting tears the view down on purpose: do not pause a session that
      // is on its way out, or the pause would race the submit.
      if (!hasTimer || paused || finished || state.submitting) return;
      paused = true;
      stopTicker();
      api(`/api/student/attempts/${attemptId}/pause`, { method: 'POST', write: true, keepalive: true })
        .then((res) => {
          const value = res?.attempt?.remaining_seconds;
          if (value !== null && value !== undefined) remaining = Math.max(0, Math.floor(Number(value)));
        })
        .catch(() => { /* the server clock stays authoritative on resume */ });
    }

    function resumeTimer() {
      if (!hasTimer || !paused || finished) return;
      api(`/api/student/attempts/${attemptId}/resume`, { method: 'POST', write: true })
        .then((res) => {
          if (finished) return;
          if (res?.attempt && res.attempt.status !== 'in_progress') {
            finishAttempt();
            return;
          }
          const value = res?.attempt?.remaining_seconds;
          if (value !== null && value !== undefined) remaining = Math.max(0, Math.floor(Number(value)));
          paused = false;
          startTicker();
        })
        .catch(() => { paused = false; startTicker(); });
    }

    async function finishAttempt() {
      finished = true;
      stopTicker();
      render(loading());
      try {
        const res = await api(`/api/student/attempts/${attemptId}/result`);
        await refreshBadge();
        showResult(res.result);
      } catch (error) {
        render(emptyState('bi-exclamation-triangle', 'تعذّر عرض النتيجة', error.message));
      }
    }

    // --- live activity heartbeat -----------------------------------------
    // While the student is present in the session (visible tab) keep them
    // counted as active in the admin live feed, so the operator sees who is
    // answering right now. It stops on hide/cleanup: an abandoned session must
    // not keep reporting the student as online.
    let activityTimer = null;
    const pingActivity = () => {
      if (document.hidden || finished) return;
      const current = questions[index];
      api('/api/student/activity/heartbeat', {
        method: 'POST', write: true,
        body: { exam_id: data.exam.id, question_id: current ? current.question_id : '' },
      }).catch(() => { /* activity is best-effort */ });
    };
    const startActivity = () => {
      pingActivity();
      if (activityTimer) clearInterval(activityTimer);
      activityTimer = setInterval(pingActivity, 45000);
    };
    const stopActivity = () => {
      if (activityTimer) { clearInterval(activityTimer); activityTimer = null; }
    };

    const onHide = () => { pauseTimer(); stopActivity(); };
    const onShow = () => { if (!document.hidden) { resumeTimer(); startActivity(); } };
    const onVisibility = () => { document.hidden ? onHide() : onShow(); };

    // Leaving the session screen (another tab, back, submit) must stop this
    // interval, or it would keep ticking against a view that no longer exists.
    state.sessionCleanup = () => {
      document.removeEventListener('visibilitychange', onVisibility);
      window.removeEventListener('pagehide', onHide);
      window.removeEventListener('blur', onHide);
      window.removeEventListener('pageshow', onShow);
      window.removeEventListener('focus', onShow);
      stopActivity();
      if (!finished) pauseTimer();
      stopTicker();
      state.sessionCleanup = null;
    };

    document.addEventListener('visibilitychange', onVisibility);
    window.addEventListener('pagehide', onHide);
    window.addEventListener('blur', onHide);
    window.addEventListener('pageshow', onShow);
    window.addEventListener('focus', onShow);

    bindSideNav();
    bindSwipe();
    document.getElementById('saSubmit').addEventListener('click', () => confirmSubmit(attemptId, answers, questions));
    paint();

    if (!hasTimer) {
      timerEl.textContent = '∞';
    } else if (data.attempt.paused) {
      // Reopened a session that was paused when the student last left: the
      // server holds the balance, so resume it now that they are back.
      paused = true;
      paintTimer();
      resumeTimer();
    } else {
      startTicker();
    }
    startActivity();
  }

  function confirmSubmit(attemptId, answers, questions) {
    const unanswered = questions.filter((q) => answers[q.question_id] === undefined).length;
    openModal('تسليم الاختبار',
      `<p class="sa-note">${unanswered ? `لديك ${unanswered} سؤالاً بلا إجابة. ` : ''}هل تريد التسليم الآن؟ لا يمكن التراجع.</p>`,
      [{ label: 'تسليم', onClick: () => { closeModal(); submitAttempt(attemptId); } },
       { label: 'إلغاء', secondary: true }]);
  }

  async function submitAttempt(attemptId) {
    state.submitting = true;
    // Detach the session listeners without pausing: the pause is skipped while
    // submitting, and a stale listener must not fire against a torn-down view.
    if (state.sessionCleanup) state.sessionCleanup();
    state.sessionCleanup = null;
    if (state.timer) { clearInterval(state.timer); state.timer = null; }
    render(loading());
    try {
      const data = await api(`/api/student/attempts/${attemptId}/submit`, { method: 'POST', write: true });
      await refreshBadge();
      showResult(data.result);
    } catch (error) {
      render(emptyState('bi-exclamation-triangle', 'تعذّر التسليم', error.message));
    } finally {
      state.submitting = false;
    }
  }

  // --- result & review -----------------------------------------------------
  function showResult(result) {
    const wrap = el('<div></div>');
    wrap.appendChild(el(`<div class="sa-card sa-result-ring">
      <div class="pct">${fmtPct(result.percentage)}</div>
      <div class="sa-note">${esc(result.exam_title)}</div>
    </div>`));
    const grid = el('<div class="sa-result-grid"></div>');
    grid.append(
      statCard(result.correct_count, 'صحيحة'),
      statCard(result.wrong_count, 'خاطئة'),
      statCard(result.unanswered_count, 'بلا إجابة'),
    );
    wrap.appendChild(grid);
    wrap.appendChild(el(`<div class="sa-card"><div class="sa-meta">
      <span><i class="bi bi-stopwatch"></i> المدة ${fmtDuration(result.duration_seconds)}</span>
      <span><i class="bi bi-award"></i> ${result.score}/${result.total_score}</span>
    </div></div>`));

    if (result.show_solutions) {
      const review = el('<button class="sa-btn">مراجعة الإجابات</button>');
      review.addEventListener('click', () => openReview(result.id));
      wrap.appendChild(review);
    }
    const home = el('<button class="sa-btn secondary">العودة للرئيسية</button>');
    home.addEventListener('click', () => go('home'));
    wrap.appendChild(home);

    setHeader('النتيجة', result.exam_title, false);
    render(wrap);
  }

  async function openReview(attemptId) {
    render(loading());
    try {
      const data = await api(`/api/student/attempts/${attemptId}/review`);
      const wrap = el('<div></div>');
      wrap.appendChild(el(`<div class="sa-card sa-result-ring">
        <div class="pct">${fmtPct(data.attempt.percentage)}</div>
        <div class="sa-note">${esc(data.exam.title)}</div></div>`));
      data.questions.forEach((q, i) => {
        const card = el(`<div class="sa-card"><div class="sa-question-text">${i + 1}. ${esc(q.question)}</div></div>`);
        q.options.forEach((option, optIndex) => {
          let cls = '';
          if (q.correct_option_id !== undefined && optIndex === q.correct_option_id) cls = 'correct';
          else if (q.selected_option === optIndex) cls = 'wrong';
          card.appendChild(el(`<div class="sa-option ${cls}"><span class="key">${String.fromCharCode(65 + optIndex)}</span><span>${esc(option)}</span></div>`));
        });
        if (q.explanation) card.appendChild(el(`<p class="sa-note" style="margin-top:8px"><i class="bi bi-info-circle"></i> ${esc(q.explanation)}</p>`));
        wrap.appendChild(card);
      });
      const back = el('<button class="sa-btn secondary">رجوع</button>');
      back.addEventListener('click', () => go('results'));
      wrap.appendChild(back);
      setHeader('مراجعة الإجابات', data.exam.title, true);
      render(wrap);
    } catch (error) {
      render(emptyState('bi-exclamation-triangle', 'خطأ', error.message));
    }
  }

  // --- exams / results / notifications / profile ---------------------------
  async function loadExams() {
    const data = await api('/api/student/exams');
    const wrap = el('<div></div>');
    if (!data.exams.length) {
      wrap.appendChild(emptyState('bi-journal-x', 'لا توجد اختبارات', 'لم يُنشر أي اختبار بعد.'));
    } else {
      data.exams.forEach((e) => wrap.appendChild(examCard(e)));
    }
    setHeader('الاختبارات', `${data.exams.length} اختبار`, false);
    render(wrap);
  }

  async function loadResults() {
    const data = await api('/api/student/attempts');
    const wrap = el('<div></div>');
    if (!data.attempts.length) {
      wrap.appendChild(emptyState('bi-trophy', 'لا توجد نتائج', 'أكمل اختباراً لعرض نتيجتك هنا.'));
    } else {
      data.attempts.forEach((a) => {
        const node = el(`<div class="sa-card sa-exam">
          <div class="sa-exam-head"><h3>${esc(a.exam_title)}</h3>
            <span class="sa-chip completed">${fmtPct(a.percentage)}</span></div>
          <div class="sa-meta">
            <span><i class="bi bi-hash"></i> محاولة ${a.attempt_number}</span>
            <span><i class="bi bi-check2"></i> ${a.correct_count}</span>
            <span><i class="bi bi-x"></i> ${a.wrong_count}</span>
          </div>
          <button class="sa-btn secondary">مراجعة</button></div>`);
        node.querySelector('button').addEventListener('click', () => openReview(a.id));
        wrap.appendChild(node);
      });
    }
    setHeader('نتائجي', '', false);
    render(wrap);
  }

  async function loadNotifications() {
    const data = await api('/api/student/notifications');
    // Opening the list counts as reading it: clear the badge and mark the rows
    // read server-side, so the unread count does not linger until each row is
    // tapped. Best-effort — a failure must not stop the list from rendering.
    if (data.unread) {
      try {
        await api('/api/student/notifications/read-all', { method: 'POST', write: true });
        data.notifications.forEach((note) => { if (!note.read_at) note.read_at = new Date().toISOString(); });
        data.unread = 0;
      } catch { /* badge stays until the next successful refresh */ }
    }
    await refreshBadge();
    const wrap = el('<div></div>');
    if (!data.notifications.length) {
      wrap.appendChild(emptyState('bi-bell-slash', 'لا توجد تنبيهات', 'ستظهر التنبيهات هنا.'));
    } else {
      data.notifications.forEach((note) => {
        const node = el(`<div class="sa-card">
          <h3>${esc(note.title)}</h3>
          <p class="sa-note">${esc(note.body)}</p>
          <div class="sa-meta" style="margin-top:8px"><span><i class="bi bi-clock-history"></i> ${esc(note.created_at)}</span></div>
        </div>`);
        wrap.appendChild(node);
      });
    }
    setHeader('التنبيهات', '', false);
    render(wrap);
  }

  // --- settings ------------------------------------------------------------
  // Rows reuse the .sa-row list pattern; the account card shows the identity
  // Telegram gave us, and everything else (theme, notifications, policy, about)
  // is reachable from here without leaving the Mini App.
  function settingsRow({ icon, title, subtitle, end, onClick, control }) {
    // A <div> (not <button>) so a switch control can live inside without
    // nesting interactive elements.
    const node = el(`<div class="sa-row" role="${onClick ? 'button' : 'group'}"${onClick ? ' tabindex="0"' : ''}>
      <span class="sa-row-icon"><i class="bi ${icon}"></i></span>
      <span class="sa-row-body">${esc(title)}${subtitle ? `<small>${esc(subtitle)}</small>` : ''}</span>
      <span class="sa-row-end"></span>
    </div>`);
    const endEl = node.querySelector('.sa-row-end');
    if (control) endEl.appendChild(control);
    else endEl.innerHTML = end || '<i class="bi bi-chevron-left"></i>';
    if (onClick) {
      node.addEventListener('click', (event) => {
        // Ignore clicks that originate inside an embedded control.
        if (event.target.closest('.sa-switch, .sa-seg, input, button')) return;
        onClick();
      });
      node.addEventListener('keydown', (event) => {
        if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); onClick(); }
      });
    } else {
      node.style.cursor = 'default';
    }
    return node;
  }

  function toggleSwitch(checked, onChange) {
    const wrap = el(`<span class="sa-switch">
      <input type="checkbox" ${checked ? 'checked' : ''} aria-label="تبديل">
      <span class="track"></span><span class="thumb"></span></span>`);
    wrap.querySelector('input').addEventListener('change', (event) => onChange(event.target.checked));
    return wrap;
  }

  function themeSegment() {
    const seg = el(`<div class="sa-seg">
      <button data-mode="light"><i class="bi bi-sun"></i> نهاري</button>
      <button data-mode="dark"><i class="bi bi-moon-stars"></i> ليلي</button>
      <button data-mode="system"><i class="bi bi-circle-half"></i> تلقائي</button>
    </div>`);
    let saved = null;
    try { saved = localStorage.getItem(THEME_KEY); } catch { /* ignore */ }
    let current = saved === 'light' || saved === 'dark' ? saved : 'system';
    const paint = () => seg.querySelectorAll('button').forEach((b) =>
      b.classList.toggle('active', b.dataset.mode === current));
    seg.querySelectorAll('button').forEach((button) =>
      button.addEventListener('click', () => {
        const mode = button.dataset.mode;
        if (mode === 'system') {
          try { localStorage.removeItem(THEME_KEY); } catch { /* ignore */ }
          setTheme(tg?.colorScheme === 'light' ? 'light' : 'dark', false);
        } else {
          setTheme(mode, true);
        }
        current = mode;
        paint();
      }));
    paint();
    return seg;
  }

  async function loadSettings() {
    const data = await api('/api/student/profile');
    const p = data.profile || {};
    const wrap = el('<div></div>');

    const initial = (p.name || '؟').trim().charAt(0);
    wrap.appendChild(el(`<div class="sa-card sa-hero">
      ${p.photo_url
        ? `<img class="sa-avatar" src="${esc(p.photo_url)}" alt="">`
        : `<span class="sa-avatar">${esc(initial)}</span>`}
      <div class="sa-hero-text">
        <h3>${esc(p.name || 'طالب')}</h3>
        <p>${p.username ? '@' + esc(p.username) : 'حساب تيليجرام'}</p>
      </div>
      <i class="bi bi-patch-check-fill" style="color:var(--sa-brand);font-size:1.3rem"></i>
    </div>`));

    wrap.appendChild(sectionTitle('التفضيلات'));
    const prefs = el('<div class="sa-list"></div>');
    prefs.appendChild(settingsRow({
      icon: 'bi-bell', title: 'تفعيل الإشعارات',
      subtitle: 'استلام تنبيهات الاختبارات والنتائج',
      control: toggleSwitch(!!p.notifications_enabled, async (enabled) => {
        try {
          await api('/api/student/notifications/prefs', {
            method: 'POST', write: true, body: { enabled },
          });
          alertMsg(enabled ? 'تم تفعيل الإشعارات.' : 'تم إيقاف الإشعارات.');
        } catch (error) { alertMsg(error.message); }
      }),
    }));
    wrap.appendChild(prefs);

    wrap.appendChild(sectionTitle('المظهر'));
    const appearance = el('<div class="sa-list"></div>');
    appearance.appendChild(el(`<div class="sa-row" style="cursor:default">
      <span class="sa-row-icon"><i class="bi bi-palette"></i></span>
      <span class="sa-row-body">وضع العرض<small>الوضع الليلي أو النهاري</small></span>
    </div>`));
    const segWrap = el('<div style="padding:4px 15px 14px"></div>');
    segWrap.appendChild(themeSegment());
    appearance.appendChild(segWrap);
    wrap.appendChild(appearance);

    wrap.appendChild(sectionTitle('الحساب والمعلومات'));
    const info = el('<div class="sa-list"></div>');
    info.appendChild(settingsRow({
      icon: 'bi-person-badge', title: 'حسابي',
      subtitle: p.username ? '@' + p.username : 'حساب تيليجرام',
      onClick: () => openAccount(p),
    }));
    info.appendChild(settingsRow({
      icon: 'bi-shield-lock', title: 'سياسة الخصوصية',
      onClick: openPrivacy,
    }));
    info.appendChild(settingsRow({
      icon: 'bi-info-circle', title: 'حول التطبيق',
      subtitle: 'إصدار 1.0.0',
      onClick: openAbout,
    }));
    wrap.appendChild(info);

    setHeader('الإعدادات', 'التفضيلات والحساب', false);
    render(wrap);
  }

  function openAccount(p) {
    const rows = [
      ['الاسم', p.name || '—'],
      ['اسم المستخدم', p.username ? '@' + p.username : '—'],
      ['معرّف تيليجرام', p.telegram_user_id ?? '—'],
      ['تاريخ الانضمام', p.created_at || '—'],
      ['آخر ظهور', p.last_seen_at || '—'],
      ['الإشعارات', p.notifications_enabled ? 'مفعّلة' : 'موقوفة'],
    ].map(([k, v]) => `<div style="display:flex;justify-content:space-between;gap:12px;padding:9px 0;border-bottom:1px solid var(--sa-border)">
        <span class="sa-note" style="margin:0">${esc(k)}</span>
        <span style="font-weight:700;font-size:0.85rem">${esc(v)}</span></div>`).join('');
    openModal('حسابي', rows, [{ label: 'إغلاق', secondary: true }]);
  }

  function openPrivacy() {
    openModal('سياسة الخصوصية', `<p class="sa-note">
      نحترم خصوصيتك في هذا التطبيق المصغّر:
    </p>
    <ul class="sa-note" style="padding-inline-start:18px;margin:10px 0 0">
      <li>نجمع فقط معرّف تيليجرام وبيانات ملفك العام (الاسم وصورة العرض) للتعريف بك.</li>
      <li>تُستخدم إجاباتك ونتائجك لعرض تقدّمك داخل الاختبارات فقط.</li>
      <li>لا نشارك بياناتك مع أي طرف ثالث، ولا نرسل رسائل نيابة عنك.</li>
      <li>يمكنك إيقاف الإشعارات في أي وقت من صفحة الإعدادات.</li>
      <li>لطلب حذف بياناتك تواصل مع مشرف القناة.</li>
    </ul>`, [{ label: 'فهمت', secondary: true }]);
  }

  function openAbout() {
    openModal('حول التطبيق', `<p class="sa-note">
      تطبيق الاختبارات التفاعلي داخل تيليجرام. خُض الاختبارات، تابع نتائجك،
      واستعرض الإجابات بعد التسليم.
    </p>
    <div class="sa-note" style="margin-top:10px">
      الإصدار 1.0.0 · بوابة الطالب
    </div>`, [{ label: 'إغلاق', secondary: true }]);
  }

  // --- modal ---------------------------------------------------------------
  function openModal(title, bodyHtml, actions) {
    const modal = document.getElementById('saModal');
    document.getElementById('saModalTitle').textContent = title;
    document.getElementById('saModalBody').innerHTML = bodyHtml;
    const area = document.getElementById('saModalActions');
    area.innerHTML = '';
    actions.forEach((action) => {
      const button = el(`<button class="sa-btn ${action.secondary ? 'secondary' : ''}">${esc(action.label)}</button>`);
      // A secondary action (cancel/close) may omit onClick; default to closing
      // so the button is never wired to a missing handler.
      button.addEventListener('click', action.onClick || closeModal);
      area.appendChild(button);
    });
    modal.hidden = false;
  }
  function closeModal() { document.getElementById('saModal').hidden = true; }

  // --- wiring --------------------------------------------------------------
  document.addEventListener('DOMContentLoaded', () => {
    document.querySelectorAll('#saTabs button').forEach((button) =>
      button.addEventListener('click', () => go(button.dataset.tab)));
    document.getElementById('saBack').addEventListener('click', () => go(state.view === 'home' ? 'home' : state.view));
    document.getElementById('saBell').addEventListener('click', () => go('notifications'));
    document.getElementById('saModal').addEventListener('click', (event) => {
      if (event.target.id === 'saModal') closeModal();
    });
    if (tg) tg.onEvent?.('themeChanged', applyTheme);
    boot();
  });

  return { go, openExam, refreshBadge };
})();
