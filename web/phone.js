/* Экран по QR — мобильная страница.
 *
 * Контракт (см. docs/design/ui.md):
 *   GET  /phone, /assets/phone.css, /assets/phone.js
 *   WS   /ws  (тот же host/порт, что и страница)
 *   auth -> ready -> offer -> answer, ICE только внутри SDP (non-trickle),
 *   iceServers: [], видео recvonly, одноразовый токен из #token.
 */
(function () {
  'use strict';

  var AUTH_TIMEOUT_MS = 5000;
  // Includes the system source picker (up to 120 seconds) and WebRTC setup.
  var CONNECT_TIMEOUT_MS = 150000;
  var ICE_GATHER_TIMEOUT_MS = 5000;

  var ERROR_MESSAGES = {
    invalid_token: 'Ссылка недействительна. Отсканируйте новый QR-код на компьютере.',
    token_expired: 'Срок действия ссылки истёк. Отсканируйте новый QR-код на компьютере.',
    auth_timeout: 'Компьютер не ответил вовремя. Отсканируйте новый QR-код.',
    busy: 'К компьютеру уже подключён другой телефон. Закройте его и отсканируйте новый QR-код.',
    protocol_error: 'Сбой согласования с компьютером. Отсканируйте новый QR-код.',
    capture_unavailable: 'Компьютер не может захватить экран. Проверьте разрешение на запись экрана.'
  };

  // Заголовки для отказов: один и тот же экран, но текст сразу говорит, что произошло.
  var ERROR_TITLES = {
    busy: 'Компьютер занят',
    capture_unavailable: 'Захват экрана недоступен'
  };

  var ENDED_MESSAGES = {
    stopped: 'Трансляция остановлена на компьютере.',
    disconnected: 'Связь с компьютером прервана.',
    capture_failed: 'Захват экрана на компьютере прервался.'
  };

  var ENDED_TITLES = {
    capture_failed: 'Захват экрана прервался'
  };

  // Причина, по которой клиент уже не будет продолжать: токен израсходован.
  var NEW_QR_HINT = 'Отсканируйте новый QR-код на компьютере, чтобы начать снова.';

  var els = {
    app: document.getElementById('phoneApp'),
    video: document.getElementById('phoneVideo'),
    overlay: document.getElementById('phoneOverlay'),
    eyebrow: document.getElementById('eyebrow'),
    title: document.getElementById('title'),
    status: document.getElementById('status'),
    hint: document.getElementById('hint'),
    playButton: document.getElementById('playButton'),
    fullscreenButton: document.getElementById('fullscreenButton'),
    leaveButton: document.getElementById('leaveButton'),
    toolbar: document.getElementById('toolbar'),
    zoomResetButtonMini: document.getElementById('zoomResetButtonMini'),
    fitButtonMini: document.getElementById('fitButtonMini'),
    mouseButtonMini: document.getElementById('mouseButtonMini'),
    fullscreenButtonMini: document.getElementById('fullscreenButtonMini'),
    leaveButtonMini: document.getElementById('leaveButtonMini'),
    mouseBar: document.getElementById('mouseBar'),
    mouseBtnRight: document.getElementById('mouseBtnRight'),
    mouseBtnDbl: document.getElementById('mouseBtnDbl'),
    mouseBtnScrollUp: document.getElementById('mouseBtnScrollUp'),
    mouseBtnScrollDown: document.getElementById('mouseBtnScrollDown'),
    srStatus: document.getElementById('srStatus')
  };

  var state = {
    ws: null,
    pc: null,
    stream: null,
    token: null,
    active: false,
    finished: false,
    timers: {},
    autoplayBlocked: false,
    isCovered: false,
    toolbarDimmed: false,
    toolbarTimer: null,
    wakeLock: null,
    pingTimer: null,
    scale: 1.0,
    panX: 0,
    panY: 0,
    mouseMode: false,
    lastMouseCoords: { x: 0.5, y: 0.5 }
  };

  /* ---------------- утилиты ---------------- */

  function show(el) {
    if (el) el.classList.remove('is-hidden');
  }

  function hide(el) {
    if (el) el.classList.add('is-hidden');
  }

  function announce(text) {
    if (els.srStatus) els.srStatus.textContent = text || '';
  }

  function setOverlay(stateName, title, status, hint) {
    if (!els.overlay) return;
    els.overlay.dataset.state = stateName;
    els.overlay.hidden = false;
    if (els.title) els.title.textContent = title;
    if (els.status) els.status.textContent = status;
    if (els.hint) {
      if (hint) {
        els.hint.textContent = hint;
        els.hint.hidden = false;
      } else {
        els.hint.textContent = '';
        els.hint.hidden = true;
      }
    }
    announce(title + ' ' + status);
  }

  function setStreaming(on) {
    state.active = !!on;
    if (els.app) els.app.classList.toggle('is-streaming', !!on);
    if (els.toolbar) els.toolbar.classList.toggle('is-hidden', !on);
    if (els.overlay) els.overlay.hidden = !!on;
    if (on) {
      updateFitButton();
      updateFullscreenButtons();
      updateZoomResetButton();
      scheduleToolbarHide();
      requestWakeLock();
      startPing();
    } else {
      stopPing();
      releaseWakeLock();
      if (els.mouseBar) els.mouseBar.classList.add('is-hidden');
      resetZoom(false);
    }
  }

  /* Оверлей остаётся видимым поверх видео, но интерактивна только панель:
     .phone-overlay имеет pointer-events: none, .phone-overlay__panel — auto.
     Так кнопка «Показать видео» всегда получает нажатие. */
  function showPlaybackGate() {
    if (!els.overlay || state.finished) return;
    // Playback is not active until the browser actually starts the video.
    if (els.toolbar) els.toolbar.classList.add('is-hidden');
    els.overlay.dataset.state = 'streaming';
    els.overlay.hidden = false;
    if (els.title) els.title.textContent = 'Трансляция готова';
    if (els.status) els.status.textContent = 'Браузер не запустил видео автоматически.';
    if (els.hint) {
      els.hint.textContent = 'Нажмите «Показать видео», чтобы начать просмотр.';
      els.hint.hidden = false;
    }
    show(els.playButton);
    updateFullscreenButtons();
    syncFocusable();
    focusPlayButton();
    announce('Трансляция готова. Нажмите «Показать видео», чтобы начать просмотр.');
  }

  function focusPlayButton() {
    if (!els.playButton) return;
    try {
      els.playButton.focus({ preventScroll: true });
    } catch (err) {
      /* старые WebView не принимают аргументы — фокус не критичен */
    }
  }

  function clearTimer(name) {
    if (state.timers[name]) {
      clearTimeout(state.timers[name]);
      delete state.timers[name];
    }
  }

  function setTimer(name, fn, ms) {
    clearTimer(name);
    state.timers[name] = setTimeout(fn, ms);
  }

  function hasFullscreen() {
    var el = document.documentElement;
    var v = els.video;
    return !!(
      (el && (el.requestFullscreen || el.webkitRequestFullscreen || el.msRequestFullscreen)) ||
      (v && (v.webkitEnterFullscreen || v.webkitSupportsFullscreen)) ||
      els.app
    );
  }

  function isFullscreen() {
    return !!(
      document.fullscreenElement ||
      document.webkitFullscreenElement ||
      document.msFullscreenElement ||
      (els.video && els.video.webkitDisplayingFullscreen) ||
      (els.app && els.app.classList.contains('is-pseudo-fullscreen'))
    );
  }

  function updateFullscreenButtons() {
    var supported = hasFullscreen() && !state.finished;
    var label = isFullscreen() ? 'Выйти из полного экрана' : 'На весь экран';
    [els.fullscreenButton, els.fullscreenButtonMini].forEach(function (btn) {
      if (!btn) return;
      if (!supported) {
        hide(btn);
        return;
      }
      show(btn);
      btn.textContent = label;
      btn.disabled = false;
    });
    syncFocusable();
  }

  function toggleFullscreen() {
    var v = els.video;
    var el = els.app || document.documentElement;
    try {
      if (isFullscreen()) {
        if (v && v.webkitDisplayingFullscreen && v.webkitExitFullscreen) {
          v.webkitExitFullscreen();
        } else if (document.exitFullscreen) {
          var res = document.exitFullscreen();
          if (res && typeof res.catch === 'function') res.catch(function () {});
        } else if (document.webkitExitFullscreen) {
          document.webkitExitFullscreen();
        }
        if (els.app) els.app.classList.remove('is-pseudo-fullscreen');
      } else {
        if (el && el.requestFullscreen) {
          el.requestFullscreen().catch(function () {
            if (v && v.webkitEnterFullscreen) v.webkitEnterFullscreen();
            else if (els.app) els.app.classList.add('is-pseudo-fullscreen');
          });
        } else if (el && el.webkitRequestFullscreen) {
          el.webkitRequestFullscreen();
        } else if (v && v.webkitEnterFullscreen) {
          v.webkitEnterFullscreen();
        } else if (els.app) {
          els.app.classList.add('is-pseudo-fullscreen');
        }
      }
    } catch (err) {
      if (els.app) els.app.classList.toggle('is-pseudo-fullscreen');
    }
    setTimeout(updateFullscreenButtons, 250);
  }

  function updateFitButton() {
    if (!els.fitButtonMini) return;
    els.fitButtonMini.textContent = state.isCovered ? 'По размеру' : 'Заполнить';
  }

  function toggleFit() {
    state.isCovered = !state.isCovered;
    if (els.video) {
      els.video.classList.toggle('is-covered', state.isCovered);
    }
    updateFitButton();
    applyTransform(false);
  }

  /* ---------------- Масштабирование и панорамирование ---------------- */

  function clampPan() {
    if (state.scale <= 1.01) {
      state.panX = 0;
      state.panY = 0;
      return;
    }
    var appW = els.app ? els.app.clientWidth : window.innerWidth;
    var appH = els.app ? els.app.clientHeight : window.innerHeight;
    var videoW = els.video ? els.video.clientWidth : appW;
    var videoH = els.video ? els.video.clientHeight : appH;

    var maxPanX = Math.max(0, (videoW * state.scale - appW) / 2);
    var maxPanY = Math.max(0, (videoH * state.scale - appH) / 2);

    state.panX = Math.max(-maxPanX, Math.min(maxPanX, state.panX));
    state.panY = Math.max(-maxPanY, Math.min(maxPanY, state.panY));
  }

  function updateZoomResetButton() {
    if (!els.zoomResetButtonMini) return;
    if (state.scale > 1.05) {
      els.zoomResetButtonMini.classList.remove('is-hidden');
      els.zoomResetButtonMini.textContent = state.scale.toFixed(1) + 'x ↺';
    } else {
      els.zoomResetButtonMini.classList.add('is-hidden');
    }
  }

  function applyTransform(animated) {
    clampPan();
    if (els.video) {
      els.video.style.transition = animated ? 'transform 0.25s ease-out' : 'none';
      if (state.scale <= 1.01 && state.panX === 0 && state.panY === 0) {
        els.video.style.transform = '';
      } else {
        els.video.style.transform =
          'translate3d(' + state.panX + 'px, ' + state.panY + 'px, 0) scale(' + state.scale + ')';
      }
    }
    updateZoomResetButton();
  }

  function resetZoom(animated) {
    state.scale = 1.0;
    state.panX = 0;
    state.panY = 0;
    applyTransform(animated !== false);
  }

  /* ---------------- Управление мышью (Touchpad / Pointer) ---------------- */

  var pendingMove = null;
  var moveRafId = null;

  function sendInput(payload) {
    if (!state.ws || state.ws.readyState !== WebSocket.OPEN || !state.active) return;
    var msg = Object.assign({ type: 'input' }, payload);
    try {
      state.ws.send(JSON.stringify(msg));
    } catch (err) {}
  }

  function sendMouseMoveThrottled(coords) {
    pendingMove = coords;
    state.lastMouseCoords = coords;
    if (!moveRafId) {
      moveRafId = requestAnimationFrame(function () {
        moveRafId = null;
        if (pendingMove) {
          sendInput({ action: 'move', x: pendingMove.x, y: pendingMove.y });
          pendingMove = null;
        }
      });
    }
  }

  function getMonitorCoords(clientX, clientY) {
    var video = els.video;
    if (!video || !video.videoWidth || !video.videoHeight) {
      var rect = video ? video.getBoundingClientRect() : null;
      if (!rect || rect.width <= 0 || rect.height <= 0) return { x: 0.5, y: 0.5 };
      var nx = Math.max(0, Math.min(1, (clientX - rect.left) / rect.width));
      var ny = Math.max(0, Math.min(1, (clientY - rect.top) / rect.height));
      return { x: nx, y: ny };
    }

    var rect = video.getBoundingClientRect();
    if (rect.width <= 0 || rect.height <= 0) return { x: 0.5, y: 0.5 };

    var isCovered = video.classList.contains('is-covered');
    var videoRatio = video.videoWidth / video.videoHeight;
    var elemRatio = rect.width / rect.height;

    var renderedW, renderedH, offsetX, offsetY;

    if (isCovered) {
      if (elemRatio > videoRatio) {
        renderedW = rect.width;
        renderedH = rect.width / videoRatio;
        offsetX = 0;
        offsetY = (rect.height - renderedH) / 2;
      } else {
        renderedH = rect.height;
        renderedW = rect.height * videoRatio;
        offsetX = (rect.width - renderedW) / 2;
        offsetY = 0;
      }
    } else {
      if (elemRatio > videoRatio) {
        renderedH = rect.height;
        renderedW = rect.height * videoRatio;
        offsetX = (rect.width - renderedW) / 2;
        offsetY = 0;
      } else {
        renderedW = rect.width;
        renderedH = rect.width / videoRatio;
        offsetX = 0;
        offsetY = (rect.height - renderedH) / 2;
      }
    }

    var relX = clientX - rect.left - offsetX;
    var relY = clientY - rect.top - offsetY;

    var normX = relX / renderedW;
    var normY = relY / renderedH;

    normX = Math.max(0, Math.min(1, normX));
    normY = Math.max(0, Math.min(1, normY));

    return { x: normX, y: normY };
  }

  function toggleMouseMode() {
    state.mouseMode = !state.mouseMode;
    if (els.mouseButtonMini) {
      els.mouseButtonMini.textContent = state.mouseMode ? 'Мышь: ВКЛ' : 'Мышь';
      els.mouseButtonMini.classList.toggle('button--active', state.mouseMode);
    }
    if (els.mouseBar) {
      els.mouseBar.classList.toggle('is-hidden', !state.mouseMode);
    }
    if (state.mouseMode) {
      announce('Управление мышью включено. Касание — клик, удержание — правая кнопка, два пальца — скролл и зум.');
    } else {
      announce('Управление мышью выключено.');
    }
  }

  function scheduleToolbarHide() {
    if (state.toolbarTimer) {
      clearTimeout(state.toolbarTimer);
    }
    state.toolbarTimer = setTimeout(function () {
      if (state.active && !state.finished && els.toolbar) {
        els.toolbar.classList.add('is-dimmed');
        state.toolbarDimmed = true;
      }
    }, 4000);
  }

  function showToolbar() {
    if (els.toolbar) {
      els.toolbar.classList.remove('is-dimmed');
      state.toolbarDimmed = false;
      scheduleToolbarHide();
    }
  }

  function toggleToolbar() {
    if (els.toolbar) {
      if (els.toolbar.classList.contains('is-dimmed')) {
        showToolbar();
      } else {
        els.toolbar.classList.add('is-dimmed');
        state.toolbarDimmed = true;
      }
    }
  }

  function requestWakeLock() {
    if ('wakeLock' in navigator && state.active && !state.finished && !state.wakeLock) {
      navigator.wakeLock.request('screen').then(function (lock) {
        state.wakeLock = lock;
        lock.addEventListener('release', function () {
          state.wakeLock = null;
        });
      }).catch(function () {});
    }
  }

  function releaseWakeLock() {
    if (state.wakeLock) {
      try {
        state.wakeLock.release();
      } catch (err) {}
      state.wakeLock = null;
    }
  }

  function startPing() {
    stopPing();
    state.pingTimer = setInterval(function () {
      if (state.ws && state.ws.readyState === WebSocket.OPEN && state.active) {
        send({ type: 'ping' });
      }
    }, 10000);
  }

  function stopPing() {
    if (state.pingTimer) {
      clearInterval(state.pingTimer);
      state.pingTimer = null;
    }
  }

  /* ---------------- токен ---------------- */

  function readToken() {
    var hash = window.location.hash || '';
    if (hash.charAt(0) === '#') hash = hash.slice(1);
    if (!hash) return null;
    var params = new URLSearchParams(hash);
    var token = params.get('token');
    if (!token) return null;
    return token.trim() || null;
  }

  function stripFragment() {
    if (!window.location.hash) return;
    try {
      window.history.replaceState(null, '', window.location.pathname + window.location.search);
    } catch (err) {
      /* replaceState может быть недоступен в старых WebView — не критично. */
    }
  }

  /* ---------------- WebSocket ---------------- */

  function wsUrl() {
    var proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    return proto + '//' + window.location.host + '/ws';
  }

  function send(message) {
    if (!state.ws || state.ws.readyState !== WebSocket.OPEN) return false;
    try {
      state.ws.send(JSON.stringify(message));
      return true;
    } catch (err) {
      return false;
    }
  }

  function closeSocket() {
    if (!state.ws) return;
    var ws = state.ws;
    state.ws = null;
    try {
      ws.onopen = null;
      ws.onmessage = null;
      ws.onerror = null;
      ws.onclose = null;
      ws.close();
    } catch (err) {
      /* уже закрыт */
    }
  }

  function closePeer() {
    if (state.pc) {
      try {
        state.pc.onicecandidate = null;
        state.pc.ontrack = null;
        state.pc.onconnectionstatechange = null;
        state.pc.close();
      } catch (err) {
        /* уже закрыт */
      }
      state.pc = null;
    }
    state.stream = null;
    if (els.video) els.video.srcObject = null;
  }

  function teardown() {
    clearTimer('connect');
    clearTimer('auth');
    stopPing();
    releaseWakeLock();
    closePeer();
    closeSocket();
  }

  function finish(kind, title, status, hint) {
    if (state.finished) return;
    state.finished = true;
    teardown();
    setStreaming(false);
    setOverlay(kind, title, status, hint);
    hide(els.playButton);
    hide(els.fullscreenButton);
    hide(els.leaveButton);
    updateFullscreenButtons();
    syncFocusable();
  }

  function fail(message, code) {
    finish(
      'error',
      (code && ERROR_TITLES[code]) || 'Не удалось подключиться',
      message,
      NEW_QR_HINT
    );
  }

  function describeError(code, message) {
    if (code && ERROR_MESSAGES[code]) return ERROR_MESSAGES[code];
    if (typeof message === 'string' && message.trim()) return message.trim();
    return 'Компьютер отклонил подключение. Отсканируйте новый QR-код.';
  }

  /* ---------------- WebRTC ---------------- */

  function waitForIceGathering(pc) {
    if (pc.iceGatheringState === 'complete') return Promise.resolve();
    return new Promise(function (resolve, reject) {
      var done = false;
      function finishGathering() {
        if (done) return;
        done = true;
        pc.removeEventListener('icegatheringstatechange', onChange);
        resolve();
      }
      function onChange() {
        if (pc.iceGatheringState === 'complete') finishGathering();
      }
      pc.addEventListener('icegatheringstatechange', onChange);
      /* Страховка: не ждём вечно, если браузер не сообщает complete. */
      setTimeout(function () {
        if (done) return;
        done = true;
        pc.removeEventListener('icegatheringstatechange', onChange);
        reject(new Error('ICE gathering timeout'));
      }, ICE_GATHER_TIMEOUT_MS);
    });
  }

  function createPeer() {
    var pc = new RTCPeerConnection({ iceServers: [] });
    state.pc = pc;

    pc.addTransceiver('video', { direction: 'recvonly' });

    pc.ontrack = function (event) {
      var stream = event.streams && event.streams[0];
      if (!stream) {
        stream = new MediaStream([event.track]);
      }
      state.stream = stream;
      if (els.video) {
        els.video.srcObject = stream;
        tryPlay();
      }
    };

    pc.onconnectionstatechange = function () {
      var s = pc.connectionState;
      if (s === 'failed') {
        fail('Не удалось установить видеопоток. Отсканируйте новый QR-код.');
      } else if (s === 'disconnected') {
        setOverlay('connecting', 'Связь прерывается', 'Пробуем восстановить соединение…', '');
      } else if (s === 'connected') {
        clearTimer('connect');
        markStreaming();
      }
    };

    return pc;
  }

  function markStreaming() {
    if (state.finished) return;
    setStreaming(true);
    if (state.autoplayBlocked) {
      showPlaybackGate();
      return;
    }
    hide(els.playButton);
    hide(els.fullscreenButton);
    hide(els.leaveButton);
    updateFullscreenButtons();
    syncFocusable();
  }

  function tryPlay() {
    var video = els.video;
    if (!video) return;
    var promise;
    try {
      promise = video.play();
    } catch (err) {
      state.autoplayBlocked = true;
      showPlaybackGate();
      return;
    }
    if (promise && typeof promise.catch === 'function') {
      promise.catch(function () {
        state.autoplayBlocked = true;
        showPlaybackGate();
      });
    }
  }

  function sendOffer() {
    var pc = createPeer();
    pc.createOffer()
      .then(function (offer) {
        var sdp = offer.sdp;
        if (sdp && sdp.indexOf('b=AS:') === -1) {
          sdp = sdp.replace(/(m=video[^\r\n]+(?:\r\n|\n))/, '$1b=AS:8000\r\nb=TIAS:8000000\r\n');
        }
        return pc.setLocalDescription({ type: offer.type, sdp: sdp });
      })
      .then(function () {
        return waitForIceGathering(pc);
      })
      .then(function () {
        if (state.finished) return;
        var sdp = pc.localDescription && pc.localDescription.sdp;
        if (!sdp) {
          fail('Не удалось подготовить видеопоток. Отсканируйте новый QR-код.');
          return;
        }
        if (!send({ type: 'offer', sdp: sdp })) {
          fail('Связь с компьютером прервалась. Отсканируйте новый QR-код.');
        }
      })
      .catch(function () {
        fail('Не удалось подготовить видеопоток. Отсканируйте новый QR-код.');
      });
  }

  function handleAnswer(sdp) {
    if (!state.pc || typeof sdp !== 'string' || !sdp) {
      fail('Компьютер прислал некорректный ответ. Отсканируйте новый QR-код.');
      return;
    }
    state.pc
      .setRemoteDescription({ type: 'answer', sdp: sdp })
      .catch(function () {
        fail('Не удалось применить ответ компьютера. Отсканируйте новый QR-код.');
      });
  }

  /* ---------------- обработка сообщений ---------------- */

  function handleMessage(raw) {
    var message;
    try {
      message = JSON.parse(raw);
    } catch (err) {
      return;
    }
    if (!message || typeof message.type !== 'string') return;

    switch (message.type) {
      case 'ready':
        clearTimer('auth');
        setOverlay('connecting', 'Подключение', 'Готовим видеопоток…', '');
        sendOffer();
        break;

      case 'answer':
        handleAnswer(message.sdp);
        break;

      case 'error':
        fail(describeError(message.code, message.message), message.code);
        break;

      case 'ended':
        var reason = message.reason;
        finish(
          'ended',
          (reason && ENDED_TITLES[reason]) || 'Трансляция завершена',
          ENDED_MESSAGES[reason] || 'Трансляция завершена.',
          NEW_QR_HINT
        );
        break;

      default:
        break;
    }
  }

  function connect() {
    var token = state.token;
    if (!token) {
      setOverlay(
        'error',
        'Нет ссылки на трансляцию',
        'В ссылке нет токена доступа.',
        'Отсканируйте QR-код на компьютере ещё раз.'
      );
      return;
    }

    if (typeof WebSocket === 'undefined' || typeof RTCPeerConnection === 'undefined') {
      setOverlay(
        'error',
        'Браузер не поддерживается',
        'Этому браузеру нужен WebRTC.',
        'Откройте ссылку в Chrome, Safari или Firefox.'
      );
      return;
    }

    setOverlay('connecting', 'Подключение к компьютеру', 'Проверяем ссылку…', '');

    var ws;
    try {
      ws = new WebSocket(wsUrl());
    } catch (err) {
      fail('Не удалось открыть соединение с компьютером. Отсканируйте новый QR-код.');
      return;
    }
    state.ws = ws;

    /* Токен одноразовый: на повторную попытку с ним не идём. */
    setTimer(
      'auth',
      function () {
        fail('Компьютер не ответил вовремя. Отсканируйте новый QR-код.');
      },
      AUTH_TIMEOUT_MS
    );

    ws.onopen = function () {
      if (state.ws !== ws) return;
      setTimer(
        'connect',
        function () {
          fail('Компьютер не начал трансляцию. Отсканируйте новый QR-код.');
        },
        CONNECT_TIMEOUT_MS
      );
      send({ type: 'auth', token: token });
    };

    ws.onmessage = function (event) {
      if (state.ws !== ws) return;
      handleMessage(event.data);
    };

    ws.onerror = function () {
      if (state.ws !== ws) return;
      if (state.finished) return;
      fail('Не удалось связаться с компьютером. Отсканируйте новый QR-код.');
    };

    ws.onclose = function () {
      if (state.ws !== ws) return;
      state.ws = null;
      if (state.finished) return;
      if (state.active) {
        finish('ended', 'Трансляция завершена', 'Связь с компьютером потеряна.', NEW_QR_HINT);
      } else {
        fail('Компьютер закрыл соединение. Отсканируйте новый QR-код.');
      }
    };
  }

  /* ---------------- действия пользователя ---------------- */

  function stopWatching() {
    if (state.finished) return;
    send({ type: 'stop' });
    finish('ended', 'Просмотр завершён', 'Вы остановили трансляцию.', NEW_QR_HINT);
  }

  function onPlayClick() {
    state.autoplayBlocked = false;
    hide(els.playButton);
    syncFocusable();
    if (state.active) {
      markStreaming();
    } else {
      setOverlay('connecting', 'Подключение', 'Готовим видеопоток…', '');
    }
    tryPlay();
  }

  /* Клавиатура: Escape закрывает полноэкранный режим (браузер сам), а
     Enter/Space на кнопках обрабатываются нативно. Остаётся только
     не дать фокусу «уехать» в скрытые кнопки. */
  function syncFocusable() {
    var buttons = [els.playButton, els.fullscreenButton, els.leaveButton];
    buttons.forEach(function (btn) {
      if (!btn) return;
      var hidden = btn.classList.contains('is-hidden');
      btn.tabIndex = hidden ? -1 : 0;
    });
    /* Если фокус остался на скрытой кнопке, уводим его на первую видимую,
       иначе клавиатурный пользователь остаётся без точки входа. */
    var active = document.activeElement;
    if (active && buttons.indexOf(active) !== -1 && active.classList.contains('is-hidden')) {
      var next = null;
      for (var i = 0; i < buttons.length; i += 1) {
        if (buttons[i] && !buttons[i].classList.contains('is-hidden')) {
          next = buttons[i];
          break;
        }
      }
      if (next) {
        try {
          next.focus({ preventScroll: true });
        } catch (err) {
          /* фокус не критичен */
        }
      } else if (active.blur) {
        active.blur();
      }
    }
  }

  function bindEvents() {
    if (els.playButton) els.playButton.addEventListener('click', onPlayClick);
    if (els.fullscreenButton) els.fullscreenButton.addEventListener('click', toggleFullscreen);
    if (els.fullscreenButtonMini) {
      els.fullscreenButtonMini.addEventListener('click', toggleFullscreen);
    }
    if (els.fitButtonMini) {
      els.fitButtonMini.addEventListener('click', toggleFit);
    }
    if (els.zoomResetButtonMini) {
      els.zoomResetButtonMini.addEventListener('click', function () {
        resetZoom(true);
      });
    }
    if (els.mouseButtonMini) {
      els.mouseButtonMini.addEventListener('click', toggleMouseMode);
    }
    if (els.mouseBtnRight) {
      els.mouseBtnRight.addEventListener('click', function () {
        sendInput({ action: 'right_click', x: state.lastMouseCoords.x, y: state.lastMouseCoords.y });
      });
    }
    if (els.mouseBtnDbl) {
      els.mouseBtnDbl.addEventListener('click', function () {
        sendInput({ action: 'dblclick', x: state.lastMouseCoords.x, y: state.lastMouseCoords.y });
      });
    }
    if (els.mouseBtnScrollUp) {
      els.mouseBtnScrollUp.addEventListener('click', function () {
        sendInput({ action: 'wheel', delta: 120 });
      });
    }
    if (els.mouseBtnScrollDown) {
      els.mouseBtnScrollDown.addEventListener('click', function () {
        sendInput({ action: 'wheel', delta: -120 });
      });
    }
    if (els.leaveButton) els.leaveButton.addEventListener('click', stopWatching);
    if (els.leaveButtonMini) els.leaveButtonMini.addEventListener('click', stopWatching);

    document.addEventListener('fullscreenchange', updateFullscreenButtons);
    document.addEventListener('webkitfullscreenchange', updateFullscreenButtons);
    window.addEventListener('resize', function () {
      applyTransform(false);
    });
    window.addEventListener('orientationchange', function () {
      setTimeout(function () {
        applyTransform(false);
      }, 200);
    });

    // Touch & Gesture handling
    var touchStartPos = { x: 0, y: 0 };
    var touchStartTime = 0;
    var touchFingerCount = 0;
    var longPressTimer = null;
    var longPressFired = false;
    var lastTapTime = 0;

    var pinchInitialDist = 0;
    var pinchInitialScale = 1.0;
    var pinchInitialPan = { x: 0, y: 0 };
    var pinchInitialMid = { x: 0, y: 0 };
    var lastScrollY = 0;

    var panStartPos = { x: 0, y: 0 };
    var panInitial = { x: 0, y: 0 };

    function onTouchStart(e) {
      if (!state.active || state.finished) return;
      touchFingerCount = e.touches.length;
      touchStartTime = Date.now();
      longPressFired = false;
      if (longPressTimer) {
        clearTimeout(longPressTimer);
        longPressTimer = null;
      }

      if (e.touches.length === 1) {
        var t = e.touches[0];
        touchStartPos = { x: t.clientX, y: t.clientY };
        panStartPos = { x: t.clientX, y: t.clientY };
        panInitial = { x: state.panX, y: state.panY };

        var coords = getMonitorCoords(t.clientX, t.clientY);
        state.lastMouseCoords = coords;

        if (state.mouseMode) {
          sendMouseMoveThrottled(coords);
          longPressTimer = setTimeout(function () {
            longPressFired = true;
            sendInput({ action: 'right_click', x: coords.x, y: coords.y });
            if (navigator.vibrate) {
              try {
                navigator.vibrate(50);
              } catch (vErr) {}
            }
          }, 450);
        }
      } else if (e.touches.length === 2) {
        if (longPressTimer) {
          clearTimeout(longPressTimer);
          longPressTimer = null;
        }

        var t1 = e.touches[0];
        var t2 = e.touches[1];
        var dx = t2.clientX - t1.clientX;
        var dy = t2.clientY - t1.clientY;
        pinchInitialDist = Math.sqrt(dx * dx + dy * dy);
        pinchInitialScale = state.scale;
        pinchInitialPan = { x: state.panX, y: state.panY };
        pinchInitialMid = {
          x: (t1.clientX + t2.clientX) / 2,
          y: (t1.clientY + t2.clientY) / 2
        };
        lastScrollY = pinchInitialMid.y;
      }
    }

    function onTouchMove(e) {
      if (!state.active || state.finished) return;

      if (e.touches.length === 2) {
        if (e.cancelable) e.preventDefault();

        var t1 = e.touches[0];
        var t2 = e.touches[1];
        var dx = t2.clientX - t1.clientX;
        var dy = t2.clientY - t1.clientY;
        var currentDist = Math.sqrt(dx * dx + dy * dy);
        var currentMid = {
          x: (t1.clientX + t2.clientX) / 2,
          y: (t1.clientY + t2.clientY) / 2
        };
        var distDelta = Math.abs(currentDist - pinchInitialDist);

        if (distDelta > 15 || !state.mouseMode) {
          if (pinchInitialDist > 0) {
            var factor = currentDist / pinchInitialDist;
            var targetScale = Math.max(1.0, Math.min(4.5, pinchInitialScale * factor));
            state.scale = targetScale;
            state.panX = pinchInitialPan.x + (currentMid.x - pinchInitialMid.x);
            state.panY = pinchInitialPan.y + (currentMid.y - pinchInitialMid.y);
            applyTransform(false);
          }
        } else if (state.mouseMode && distDelta <= 15) {
          var scrollDiff = currentMid.y - lastScrollY;
          if (Math.abs(scrollDiff) >= 12) {
            var deltaWheel = scrollDiff > 0 ? 120 : -120;
            sendInput({ action: 'wheel', delta: deltaWheel });
            lastScrollY = currentMid.y;
          }
        }
        return;
      }

      if (e.touches.length === 1) {
        var t = e.touches[0];
        var moveDx = t.clientX - touchStartPos.x;
        var moveDy = t.clientY - touchStartPos.y;
        var distMoved = Math.sqrt(moveDx * moveDx + moveDy * moveDy);

        if (distMoved > 8 && longPressTimer) {
          clearTimeout(longPressTimer);
          longPressTimer = null;
        }

        if (state.mouseMode) {
          if (e.cancelable) e.preventDefault();
          var coords = getMonitorCoords(t.clientX, t.clientY);
          sendMouseMoveThrottled(coords);
        } else if (state.scale > 1.05) {
          if (e.cancelable) e.preventDefault();
          state.panX = panInitial.x + moveDx;
          state.panY = panInitial.y + moveDy;
          applyTransform(false);
        }
      }
    }

    function onTouchEnd(e) {
      if (!state.active || state.finished) return;

      if (longPressTimer) {
        clearTimeout(longPressTimer);
        longPressTimer = null;
      }

      if (touchFingerCount >= 2 && e.touches.length === 0) {
        if (state.scale < 1.05) {
          resetZoom(true);
        } else {
          applyTransform(true);
        }
        return;
      }

      if (touchFingerCount === 1 && e.changedTouches.length === 1) {
        var t = e.changedTouches[0];
        var moveDx = t.clientX - touchStartPos.x;
        var moveDy = t.clientY - touchStartPos.y;
        var distMoved = Math.sqrt(moveDx * moveDx + moveDy * moveDy);
        var duration = Date.now() - touchStartTime;

        if (state.mouseMode) {
          if (longPressFired) {
            longPressFired = false;
            return;
          }
          if (distMoved < 12 && duration < 400) {
            var coords = getMonitorCoords(t.clientX, t.clientY);
            state.lastMouseCoords = coords;
            var now = Date.now();
            if (now - lastTapTime < 320) {
              sendInput({ action: 'dblclick', x: coords.x, y: coords.y });
              lastTapTime = 0;
            } else {
              sendInput({ action: 'click', x: coords.x, y: coords.y });
              lastTapTime = now;
            }
          }
        } else {
          if (state.scale > 1.05) {
            applyTransform(true);
          }
          if (distMoved < 10 && duration < 350) {
            var now = Date.now();
            if (now - lastTapTime < 320) {
              if (state.scale > 1.05) {
                resetZoom(true);
              } else {
                state.scale = 2.2;
                var appW = els.app ? els.app.clientWidth : window.innerWidth;
                var appH = els.app ? els.app.clientHeight : window.innerHeight;
                state.panX = (appW / 2 - t.clientX) * 1.2;
                state.panY = (appH / 2 - t.clientY) * 1.2;
                applyTransform(true);
              }
              lastTapTime = 0;
            } else {
              lastTapTime = now;
              toggleToolbar();
            }
          }
        }
      }
    }

    if (els.video) {
      els.video.addEventListener('touchstart', onTouchStart, { passive: true });
      els.video.addEventListener('touchmove', onTouchMove, { passive: false });
      els.video.addEventListener('touchend', onTouchEnd, { passive: true });
      els.video.addEventListener('touchcancel', onTouchEnd, { passive: true });

      // Desktop browser mouse support
      els.video.addEventListener('mousedown', function (e) {
        if (!state.mouseMode || !state.active || state.finished) return;
        var coords = getMonitorCoords(e.clientX, e.clientY);
        state.lastMouseCoords = coords;
        var btn = e.button === 2 ? 'right' : e.button === 1 ? 'middle' : 'left';
        sendInput({ action: 'down', button: btn, x: coords.x, y: coords.y });
      });

      els.video.addEventListener('mouseup', function (e) {
        if (!state.mouseMode || !state.active || state.finished) return;
        var coords = getMonitorCoords(e.clientX, e.clientY);
        state.lastMouseCoords = coords;
        var btn = e.button === 2 ? 'right' : e.button === 1 ? 'middle' : 'left';
        sendInput({ action: 'up', button: btn, x: coords.x, y: coords.y });
      });

      els.video.addEventListener('mousemove', function (e) {
        if (!state.mouseMode || !state.active || state.finished) return;
        var coords = getMonitorCoords(e.clientX, e.clientY);
        sendMouseMoveThrottled(coords);
      });

      els.video.addEventListener('contextmenu', function (e) {
        if (state.mouseMode) {
          e.preventDefault();
        }
      });

      els.video.addEventListener('wheel', function (e) {
        if (!state.mouseMode || !state.active || state.finished) return;
        e.preventDefault();
        var delta = e.deltaY < 0 ? 120 : -120;
        sendInput({ action: 'wheel', delta: delta });
      }, { passive: false });

      els.video.addEventListener('webkitbeginfullscreen', updateFullscreenButtons);
      els.video.addEventListener('webkitendfullscreen', updateFullscreenButtons);
      els.video.addEventListener('playing', function () {
        state.autoplayBlocked = false;
        hide(els.playButton);
        if (state.active) markStreaming();
      });
      els.video.addEventListener('pause', function () {
        if (state.active && !state.finished && !document.hidden) {
          state.autoplayBlocked = true;
          showPlaybackGate();
        }
      });
    }

    function handleResume() {
      if (!state.active || state.finished) return;
      if (!document.hidden) {
        if (els.video && els.video.paused) {
          var p = els.video.play();
          if (p && typeof p.then === 'function') {
            p.then(function () {
              state.autoplayBlocked = false;
              hide(els.playButton);
              markStreaming();
            }).catch(function () {
              showPlaybackGate();
            });
          }
        }
        requestWakeLock();
      }
    }

    document.addEventListener('visibilitychange', handleResume);
    window.addEventListener('pageshow', handleResume);
    window.addEventListener('focus', handleResume);

    window.addEventListener('beforeunload', function () {
      if (state.active && !state.finished) {
        send({ type: 'stop' });
      }
    });
  }

  /* ---------------- старт ---------------- */

  function init() {
    state.token = readToken();
    stripFragment();
    bindEvents();
    updateFullscreenButtons();
    syncFocusable();

    if (!state.token) {
      setOverlay(
        'error',
        'Нет ссылки на трансляцию',
        'В ссылке нет токена доступа.',
        'Отсканируйте QR-код на компьютере ещё раз.'
      );
      return;
    }

    connect();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
