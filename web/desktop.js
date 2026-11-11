/* Экран по QR — локальная панель управления.
 *
 * Контракт панели (порт 8766, только 127.0.0.1):
 *   GET  /api/desktop/state   -> {status, addresses, selectedAddress,
 *                                pairingUrl, tokenExpiresAt, error}
 *   POST /api/desktop/start   <- {address}
 *   POST /api/desktop/stop    <- {}
 *   GET  /api/desktop/qr.svg  -> SVG действующей pairingUrl
 *
 * Все серверные строки выводятся через textContent, HTML не собирается.
 * Панель только управляет BackendService: захват экрана и телефонный
 * сервер (порт 8765) остаются на стороне Python.
 */
(function () {
  'use strict';

  var POLL_MS = 2000;
  var REQUEST_TIMEOUT_MS = 15000;

  var STATUS_LABELS = {
    stopped: 'Сервер остановлен',
    starting: 'Запуск сервера…',
    running: 'Сервер запущен',
    stopping: 'Остановка сервера…',
    error: 'Ошибка сервера'
  };

  var STATUS_HINTS = {
    stopped: 'Выберите адрес и нажмите «Запустить».',
    starting: 'Готовим адрес и токен подключения.',
    running: 'Откройте ссылку на телефоне или отсканируйте QR-код.',
    stopping: 'Освобождаем порт и завершаем сессию.',
    error: 'Проверьте сообщение об ошибке и попробуйте снова.'
  };

  var ERROR_LABELS = {
    capture_unavailable: 'Захват экрана недоступен',
    busy: 'Сервер занят',
    server_start_failed: 'Не удалось запустить сервер',
    invalid_address: 'Недопустимый адрес',
    conflict: 'Действие уже выполняется',
    forbidden: 'Запрос отклонён',
    network: 'Нет связи с локальной панелью',
    invalid_response: 'Некорректный ответ панели'
  };

  var els = {
    panel: document.getElementById('desktopPanel'),
    address: document.getElementById('desktop-address'),
    monitor: document.getElementById('desktop-monitor'),
    start: document.getElementById('desktop-start'),
    stop: document.getElementById('desktop-stop'),
    status: document.getElementById('desktop-status'),
    error: document.getElementById('desktop-error'),
    pairing: document.getElementById('desktop-pairing'),
    qr: document.getElementById('desktop-qr'),
    qrImage: document.getElementById('desktop-qr-image'),
    link: document.getElementById('desktop-link'),
    expiry: document.getElementById('desktop-expiry'),
    fileNote: document.getElementById('desktop-file-note')
  };

  var state = {
    fileMode: false,
    // Последнее известное состояние сервера; null — ещё не получено.
    snapshot: null,
    // Идёт POST или GET: повторные действия и параллельные опросы блокируются.
    busy: false,
    // Локальная ошибка, которую нельзя стирать успешным опросом раньше времени.
    localError: null,
    pollTimer: null,
    pollInFlight: false,
    // Адрес, который выбрал пользователь; не затирается фоновым опросом.
    selectedAddress: null,
    // Выбранный монитор.
    selectedMonitor: null,
    monitorsLoaded: false,
    // QR перерисовывается только при смене pairingUrl.
    qrUrl: null,
    qrObjectUrl: null
  };

  /* ---------------- утилиты ---------------- */

  function show(el) {
    if (el) el.hidden = false;
  }

  function hide(el) {
    if (el) el.hidden = true;
  }

  function setText(el, text) {
    if (el) el.textContent = text == null ? '' : String(text);
  }

  function isHttpPage() {
    return window.location.protocol === 'http:' || window.location.protocol === 'https:';
  }

  function describeError(error) {
    if (!error) return '';
    var message = typeof error.message === 'string' ? error.message.trim() : '';
    if (message) return message;
    var code = typeof error.code === 'string' ? error.code : '';
    if (code && ERROR_LABELS[code]) return ERROR_LABELS[code];
    return 'Неизвестная ошибка панели.';
  }

  function formatExpiry(value) {
    if (typeof value !== 'string' || !value) return '';
    var date = new Date(value);
    if (isNaN(date.getTime())) return '';
    try {
      return 'Ссылка действует до ' + date.toLocaleString('ru-RU', {
        day: '2-digit',
        month: '2-digit',
        hour: '2-digit',
        minute: '2-digit'
      });
    } catch (err) {
      return 'Ссылка действует до ' + date.toISOString();
    }
  }

  /* ---------------- HTTP ---------------- */

  function requestJson(url, options) {
    var opts = options || {};
    var controller = null;
    var timer = null;
    var init = {
      method: opts.method || 'GET',
      cache: 'no-store',
      headers: { Accept: 'application/json' }
    };
    if (opts.body !== undefined) {
      init.headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(opts.body);
    }
    if (typeof AbortController === 'function') {
      controller = new AbortController();
      init.signal = controller.signal;
      timer = setTimeout(function () {
        try {
          controller.abort();
        } catch (err) {
          /* уже завершён */
        }
      }, REQUEST_TIMEOUT_MS);
    }

    function cleanup() {
      if (timer) clearTimeout(timer);
      timer = null;
    }

    return fetch(url, init)
      .then(function (response) {
        return response
          .json()
          .catch(function () {
            return null;
          })
          .then(function (payload) {
            if (response.ok) return payload;
            var error = payload && payload.error ? payload.error : null;
            if (!error) {
              error = {
                code: 'http_' + response.status,
                message: 'Панель ответила ошибкой ' + response.status + '.'
              };
            }
            throw { error: error, status: response.status };
          });
      })
      .catch(function (err) {
        if (err && err.error) throw err;
        throw {
          error: {
            code: 'network',
            message:
              'Не удалось связаться с локальной панелью. Проверьте, что запущен python main.py --web.'
          }
        };
      })
      .then(
        function (value) {
          cleanup();
          return value;
        },
        function (err) {
          cleanup();
          throw err;
        }
      );
  }

  function normalizeState(payload) {
    if (!payload || typeof payload !== 'object') {
      throw { error: { code: 'invalid_response', message: 'Панель вернула пустой ответ.' } };
    }
    var status = typeof payload.status === 'string' ? payload.status : 'error';
    if (!STATUS_LABELS[status]) status = 'error';
    var addresses = Array.isArray(payload.addresses)
      ? payload.addresses.filter(function (item) {
          return item && typeof item.address === 'string' && item.address;
        })
      : [];
    return {
      status: status,
      addresses: addresses,
      selectedAddress:
        typeof payload.selectedAddress === 'string' ? payload.selectedAddress : null,
      pairingUrl:
        typeof payload.pairingUrl === 'string' && payload.pairingUrl ? payload.pairingUrl : null,
      tokenExpiresAt: typeof payload.tokenExpiresAt === 'string' ? payload.tokenExpiresAt : null,
      error: payload.error && typeof payload.error === 'object' ? payload.error : null
    };
  }

  /* ---------------- отрисовка ---------------- */

  function renderAddresses(snapshot) {
    var select = els.address;
    if (!select) return;
    var addresses = snapshot.addresses;
    var wanted = state.selectedAddress || snapshot.selectedAddress || '';
    var current = addresses.map(function (item) {
      return item.address;
    });
    var signature = current.join(',');
    if (select.dataset.signature !== signature) {
      select.dataset.signature = signature;
      while (select.firstChild) select.removeChild(select.firstChild);
      if (!addresses.length) {
        var empty = document.createElement('option');
        empty.value = '';
        empty.textContent = 'LAN-адреса не найдены';
        select.appendChild(empty);
      } else {
        addresses.forEach(function (item) {
          var option = document.createElement('option');
          option.value = item.address;
          option.textContent = item.label ? item.label + ' — ' + item.address : item.address;
          select.appendChild(option);
        });
      }
    }
    if (wanted && current.indexOf(wanted) !== -1) {
      select.value = wanted;
    } else if (current.length) {
      select.value = current[0];
    }
    state.selectedAddress = select.value || null;
  }

  function renderMonitors(data) {
    var select = els.monitor;
    if (!select || !data || !Array.isArray(data.monitors)) return;
    var monitors = data.monitors;
    var selected = typeof data.selected === 'number' ? data.selected : 0;
    while (select.firstChild) select.removeChild(select.firstChild);
    if (!monitors.length) {
      var empty = document.createElement('option');
      empty.value = '0';
      empty.textContent = 'Экраны не найдены';
      select.appendChild(empty);
    } else {
      monitors.forEach(function (m) {
        var option = document.createElement('option');
        option.value = String(m.id);
        option.textContent = m.name || ('Экран ' + (m.id + 1));
        select.appendChild(option);
      });
    }
    select.value = String(selected);
    state.selectedMonitor = selected;
    state.monitorsLoaded = true;
  }

  function loadMonitors() {
    if (state.fileMode) return;
    requestJson('/api/desktop/monitors')
      .then(function (data) {
        renderMonitors(data);
        if (els.monitor && state.snapshot) {
          var status = state.snapshot.status;
          els.monitor.disabled =
            state.busy || status === 'starting' || status === 'stopping' || status === 'running';
        }
      })
      .catch(function () {
        /* не критично */
      });
  }

  function renderPairing(snapshot) {
    var running = snapshot.status === 'running';
    var url = running && snapshot.pairingUrl ? snapshot.pairingUrl : null;
    if (!url) {
      hide(els.pairing);
      setText(els.link, '');
      setText(els.expiry, '');
      clearQr();
      return;
    }
    show(els.pairing);
    if (els.link) {
      var link = document.createElement('a');
      link.textContent = url;
      link.href = url;
      link.target = '_blank';
      link.rel = 'noopener noreferrer';
      els.link.replaceChildren(link);
    }
    var expiry = formatExpiry(snapshot.tokenExpiresAt);
    setText(els.expiry, expiry || 'Срок действия ссылки не указан.');
    if (url !== state.qrUrl) {
      loadQr(url);
    }
  }

  function clearQr() {
    state.qrUrl = null;
    if (state.qrObjectUrl) {
      try {
        URL.revokeObjectURL(state.qrObjectUrl);
      } catch (err) {
        /* не критично */
      }
      state.qrObjectUrl = null;
    }
    if (els.qrImage) {
      els.qrImage.removeAttribute('src');
      hide(els.qrImage);
    }
  }

  function loadQr(url) {
    state.qrUrl = url;
    if (state.qrObjectUrl) {
      try {
        URL.revokeObjectURL(state.qrObjectUrl);
      } catch (err) {
        /* не критично */
      }
      state.qrObjectUrl = null;
    }
    if (!els.qrImage) return;
    hide(els.qrImage);
    // Маршрут QR отдаёт SVG действующей pairingUrl; параметр url не нужен.
    fetch('/api/desktop/qr.svg', {
      cache: 'no-store'
    })
      .then(function (response) {
        if (!response.ok) throw new Error('qr ' + response.status);
        return response.blob();
      })
      .then(function (blob) {
        // Пока грузился QR, ссылка могла смениться — устаревший ответ не показываем.
        if (state.qrUrl !== url) return;
        var objectUrl = URL.createObjectURL(blob);
        state.qrObjectUrl = objectUrl;
        els.qrImage.src = objectUrl;
        show(els.qrImage);
      })
      .catch(function () {
        if (state.qrUrl !== url) return;
        setLocalError({
          code: 'qr_unavailable',
          message: 'Не удалось загрузить QR-код. Откройте ссылку на телефоне вручную.'
        });
      });
  }

  function setLocalError(error) {
    state.localError = error || null;
    renderError();
  }

  function currentError() {
    if (state.localError) return state.localError;
    if (state.snapshot && state.snapshot.error) return state.snapshot.error;
    return null;
  }

  function renderError() {
    var error = currentError();
    if (!error) {
      setText(els.error, '');
      hide(els.error);
      return;
    }
    setText(els.error, describeError(error));
    show(els.error);
  }

  function renderControls(snapshot) {
    var status = snapshot.status;
    var hasAddress = !!(els.address && els.address.value);
    var canStart = !state.busy && hasAddress && (status === 'stopped' || status === 'error');
    var canStop = !state.busy && (status === 'running' || status === 'starting' || status === 'error');
    if (els.address) {
      els.address.disabled = state.busy || status === 'starting' || status === 'stopping';
    }
    if (els.monitor) {
      els.monitor.disabled =
        state.busy || status === 'starting' || status === 'stopping' || status === 'running';
    }
    if (els.start) els.start.disabled = !canStart;
    if (els.stop) els.stop.disabled = !canStop;
  }

  function render(snapshot) {
    state.snapshot = snapshot;
    if (els.panel) els.panel.setAttribute('data-status', snapshot.status);
    var label = STATUS_LABELS[snapshot.status] || 'Неизвестное состояние';
    var hint = STATUS_HINTS[snapshot.status] || '';
    setText(els.status, hint ? label + '. ' + hint : label);
    renderAddresses(snapshot);
    renderPairing(snapshot);
    renderError();
    renderControls(snapshot);
  }

  function renderFailure(error) {
    // Потеря связи: устаревшие данные подключения больше не показываем.
    state.snapshot = null;
    if (els.panel) els.panel.setAttribute('data-status', 'error');
    setText(els.status, 'Сервер недоступен. Проверьте, что запущен python main.py --web.');
    hide(els.pairing);
    setText(els.link, '');
    setText(els.expiry, '');
    clearQr();
    state.localError = error || null;
    renderError();
    if (els.address) els.address.disabled = true;
    if (els.monitor) els.monitor.disabled = true;
    if (els.start) els.start.disabled = true;
    if (els.stop) els.stop.disabled = true;
  }

  /* ---------------- действия ---------------- */

  function applyState(payload) {
    var snapshot = normalizeState(payload);
    state.localError = null;
    render(snapshot);
  }

  function poll() {
    if (state.fileMode || state.pollInFlight || state.busy) return;
    state.pollInFlight = true;
    requestJson('/api/desktop/state')
      .then(function (payload) {
        if (state.busy) return;
        applyState(payload);
      })
      .catch(function (err) {
        if (state.busy) return;
        renderFailure(err && err.error ? err.error : null);
      })
      .then(function () {
        state.pollInFlight = false;
      });
  }

  function startPolling() {
    if (state.pollTimer) return;
    state.pollTimer = setInterval(poll, POLL_MS);
  }

  function stopPolling() {
    if (state.pollTimer) {
      clearInterval(state.pollTimer);
      state.pollTimer = null;
    }
  }

  function runAction(action) {
    if (state.busy) return;
    state.busy = true;
    state.localError = null;
    renderError();
    if (state.snapshot) renderControls(state.snapshot);
    else {
      if (els.start) els.start.disabled = true;
      if (els.stop) els.stop.disabled = true;
      if (els.address) els.address.disabled = true;
    }
    action()
      .then(function (payload) {
        applyState(payload);
      })
      .catch(function (err) {
        var error = err && err.error ? err.error : null;
        // Ошибка действия не должна оставлять старый QR доступным.
        if (state.snapshot) {
          state.snapshot = Object.assign({}, state.snapshot, {
            status: 'error',
            pairingUrl: null,
            tokenExpiresAt: null
          });
          render(state.snapshot);
        } else if (els.panel) {
          els.panel.setAttribute('data-status', 'error');
        }
        state.localError = error || {
          code: 'unknown',
          message: 'Не удалось выполнить действие. Попробуйте ещё раз.'
        };
        renderError();
      })
      .then(function () {
        state.busy = false;
        if (state.snapshot) renderControls(state.snapshot);
        else if (els.address) els.address.disabled = false;
        poll();
      });
  }

  function onStart() {
    var address = els.address ? els.address.value : '';
    if (!address) {
      setLocalError({ code: 'invalid_address', message: 'Выберите адрес в локальной сети.' });
      return;
    }
    state.selectedAddress = address;
    runAction(function () {
      return requestJson('/api/desktop/start', { method: 'POST', body: { address: address } });
    });
  }

  function onStop() {
    runAction(function () {
      return requestJson('/api/desktop/stop', { method: 'POST', body: {} });
    });
  }

  function onAddressChange() {
    state.selectedAddress = els.address ? els.address.value : null;
    if (state.snapshot) renderControls(state.snapshot);
  }

  /* ---------------- file:// ---------------- */

  function showFileMode() {
    state.fileMode = true;
    stopPolling();
    if (els.panel) els.panel.setAttribute('data-status', 'error');
    setText(els.status, 'Панель открыта как файл, а не через локальный сервер.');
    hide(els.pairing);
    setText(els.link, '');
    setText(els.expiry, '');
    clearQr();
    state.localError = {
      code: 'file_protocol',
      message:
        'Файл нельзя открыть напрямую: серверные маршруты и оформление доступны только на http://127.0.0.1:8766/desktop.'
    };
    renderError();
    if (els.address) els.address.disabled = true;
    if (els.monitor) els.monitor.disabled = true;
    if (els.start) els.start.disabled = true;
    if (els.stop) els.stop.disabled = true;
    show(els.fileNote);
  }

  /* ---------------- старт ---------------- */

  function bindEvents() {
    if (els.start) els.start.addEventListener('click', onStart);
    if (els.stop) els.stop.addEventListener('click', onStop);
    if (els.address) els.address.addEventListener('change', onAddressChange);
    if (els.monitor) {
      els.monitor.addEventListener('change', function () {
        var val = parseInt(els.monitor.value, 10) || 0;
        state.selectedMonitor = val;
        requestJson('/api/desktop/select-monitor', {
          method: 'POST',
          body: { monitor: val }
        }).catch(function () {});
      });
    }

    document.addEventListener('visibilitychange', function () {
      if (state.fileMode) return;
      if (document.hidden) {
        stopPolling();
      } else {
        poll();
        startPolling();
      }
    });

    window.addEventListener('pagehide', function () {
      stopPolling();
      clearQr();
    });
  }

  function init() {
    bindEvents();
    if (!isHttpPage()) {
      showFileMode();
      return;
    }
    hide(els.fileNote);
    loadMonitors();
    poll();
    startPolling();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
