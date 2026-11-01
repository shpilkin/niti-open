// macOS Mail-like chrome: collapsible sidebar + draggable list/reading splitter.
// State persists in localStorage so it survives navigation and restarts.
(function () {
  const app = document.getElementById("app");
  if (!app) return;

  // Restored first: the widest the reading pane may get depends on whether the sidebar
  // takes its 268px or not.
  if (localStorage.getItem("sidebarCollapsed") === "1") {
    app.classList.add("sidebar-collapsed");
  }

  // ---- Reading-pane width (drag the splitter) ----
  // The list column keeps at least MIN_LIST and the rest is the pane's to take. A fixed
  // maximum used to stop the splitter in the middle of a wide monitor.
  const MIN_PANE = 320, MIN_LIST = 360;
  const main = document.querySelector(".main");
  const splitter = document.getElementById("splitter");

  const clampPane = (w) => {
    const left = main ? main.getBoundingClientRect().left : 0;
    const gap = splitter ? splitter.offsetWidth : 0;
    const max = Math.max(MIN_PANE, window.innerWidth - left - gap - MIN_LIST);
    return Math.round(Math.min(Math.max(w, MIN_PANE), max));
  };
  const setPane = (w) => app.style.setProperty("--pane-w", clampPane(w) + "px");
  const curPane = () => parseInt(getComputedStyle(app).getPropertyValue("--pane-w"), 10);
  // Clamped, not rejected: the window can be narrower than it was when this was saved.
  const savedW = parseInt(localStorage.getItem("paneWidth") || "", 10);
  if (savedW >= MIN_PANE) setPane(savedW);

  // The maximum follows the window and the sidebar, so re-clamp when either changes.
  const reclampPane = () => {
    const cur = curPane();
    if (cur) setPane(cur);
  };
  window.addEventListener("resize", reclampPane);

  if (splitter) {
    let dragging = false;
    const onMove = (e) => {
      if (dragging) setPane(window.innerWidth - e.clientX);
    };
    const onUp = () => {
      if (!dragging) return;
      dragging = false;
      document.body.classList.remove("resizing");
      const cur = curPane();
      if (cur) localStorage.setItem("paneWidth", cur);
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onUp);
    };
    splitter.addEventListener("mousedown", (e) => {
      e.preventDefault();
      dragging = true;
      document.body.classList.add("resizing");
      window.addEventListener("mousemove", onMove);
      window.addEventListener("mouseup", onUp);
    });
  }

  // ---- Sidebar collapse ----
  const toggle = () => {
    app.classList.toggle("sidebar-collapsed");
    localStorage.setItem(
      "sidebarCollapsed",
      app.classList.contains("sidebar-collapsed") ? "1" : "0"
    );
    reclampPane();
  };
  const btn = document.getElementById("toggle-sidebar");
  if (btn) btn.addEventListener("click", toggle);

  // Cmd+\ toggles the sidebar (matches macOS Mail).
  document.addEventListener("keydown", (e) => {
    if (e.metaKey && e.key === "\\") {
      e.preventDefault();
      toggle();
    }
  });
})();

// ---- ⌘K palette: one way in to everything ----
// The overlay holds its own input; results come from /search/palette (htmx) and are
// walked with the arrow keys. Enter opens the current row the same way a click would —
// into the pane, or by navigating — and ⌘Enter hands the query to the results page.
(function () {
  const back = document.getElementById("palette");
  const input = document.getElementById("palette-q");
  const results = document.getElementById("palette-results");
  if (!back || !input || !results) return;

  const rows = () => Array.from(results.querySelectorAll(".srow"));
  const current = () => results.querySelector(".srow.cur");

  function move(step) {
    const all = rows();
    if (!all.length) return;
    const at = all.indexOf(current());
    const next = all[Math.min(all.length - 1, Math.max(0, (at < 0 ? -1 : at) + step))];
    all.forEach((r) => r.classList.remove("cur"));
    if (next) {
      next.classList.add("cur");
      next.scrollIntoView({ block: "nearest" });
    }
  }

  function open(seed) {
    back.hidden = false;
    input.value = seed || "";
    input.focus();
    input.select();
    if (seed) input.dispatchEvent(new Event("input"));
  }

  function close() {
    back.hidden = true;
    input.blur();
  }

  function activate(row) {
    if (!row || !row.dataset.url) return;
    close();
    if (row.dataset.pane === "1" && window.htmx) {
      htmx.ajax("GET", row.dataset.url, { target: "#pane", swap: "innerHTML" });
    } else {
      window.location = row.dataset.url;
    }
  }

  document.addEventListener("keydown", (e) => {
    if ((e.metaKey || e.ctrlKey) && (e.key === "k" || e.key === "K")) {
      e.preventDefault();
      if (back.hidden) open(""); else close();
      return;
    }
    if (back.hidden) return;
    if (e.key === "Escape") { e.preventDefault(); close(); }
    else if (e.key === "ArrowDown") { e.preventDefault(); move(1); }
    else if (e.key === "ArrowUp") { e.preventDefault(); move(-1); }
    else if (e.key === "Enter") {
      e.preventDefault();
      if (e.metaKey || e.ctrlKey) {
        close();
        const box = document.getElementById("search-box");
        if (box && window.htmx) {
          box.value = input.value;
          htmx.ajax("GET", "/search", {
            target: "#list", swap: "innerHTML", values: { q: input.value },
          });
        }
      } else {
        activate(current() || rows()[0]);
      }
    }
  });

  // Clicking the backdrop closes; clicking a row is handled by the row itself.
  back.addEventListener("mousedown", (e) => { if (e.target === back) close(); });
  results.addEventListener("click", (e) => {
    if (e.target.closest(".srow")) close();
  });
  // A fresh result list has no cursor — put it on the first row so Enter always works.
  results.addEventListener("htmx:afterSwap", () => {
    const first = rows()[0];
    if (first) first.classList.add("cur");
  });
})();

// ---- Toasts (transient confirmations, e.g. "Отправлено") ----
// opts.action + opts.label add a button (undo); opts.ms overrides how long it stays.
function showToast(msg, opts) {
  opts = opts || {};
  let host = document.getElementById("toast-host");
  if (!host) {
    host = document.createElement("div");
    host.id = "toast-host";
    document.body.appendChild(host);
  }
  const t = document.createElement("div");
  t.className = "toast";
  t.textContent = msg;
  if (opts.action) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "toast-undo";
    b.textContent = opts.label || "Отменить";
    b.addEventListener("click", (ev) => { ev.stopPropagation(); t.remove(); opts.action(); });
    t.appendChild(b);
  }
  host.appendChild(t);
  // Fade out then remove.
  const ms = opts.ms || 2600;
  setTimeout(() => t.classList.add("hide"), ms);
  setTimeout(() => t.remove(), ms + 500);
}

// ---- Collapse per-account mail groups (Входящие/Отправленные) ----
function _collapsedAccounts() {
  try { return new Set(JSON.parse(localStorage.getItem("collapsedAccounts") || "[]")); }
  catch (e) { return new Set(); }
}
function applyAccountCollapse() {
  const set = _collapsedAccounts();
  document.querySelectorAll("[data-acct-group]").forEach((g) => {
    g.style.display = set.has(g.dataset.acctGroup) ? "none" : "";
  });
  document.querySelectorAll("[data-acct-toggle]").forEach((c) => {
    const a = c.querySelector(".acct-arrow");
    if (a) a.textContent = set.has(c.dataset.acctToggle) ? "▸" : "▾";
  });
}
document.addEventListener("click", (e) => {
  const cap = e.target.closest("[data-acct-toggle]");
  if (!cap) return;
  const id = cap.dataset.acctToggle;
  const set = _collapsedAccounts();
  set.has(id) ? set.delete(id) : set.add(id);
  localStorage.setItem("collapsedAccounts", JSON.stringify([...set]));
  applyAccountCollapse();
});
// Re-apply after the sidebar re-renders (htmx swaps it on countsChanged).
document.body.addEventListener("htmx:afterSettle", applyAccountCollapse);
// И при загрузке: разметку с сервера правит встроенный скрипт в base.html (до отрисовки),
// но состояние стрелок и повторные проходы держим в одном месте — здесь.
applyAccountCollapse();

// ---- Прокрутка меню переживает переход ----
// Каждый пункт меню — обычная ссылка, то есть полная перезагрузка страницы. Список
// ящиков и проектов длиннее окна, и меню всякий раз отбрасывало в начало.
function _nav() { return document.querySelector(".sidebar .nav"); }
function saveNavScroll() {
  const nav = _nav();
  if (nav) { try { sessionStorage.setItem("navScroll", String(nav.scrollTop)); } catch (e) {} }
}
function restoreNavScroll() {
  const nav = _nav();
  if (!nav) return;
  try {
    const at = parseInt(sessionStorage.getItem("navScroll") || "0", 10);
    if (at > 0) nav.scrollTop = at;
  } catch (e) { /* приватное окно — просто останемся наверху */ }
}
document.addEventListener("scroll", (e) => {
  if (e.target && e.target.classList && e.target.classList.contains("nav")) saveNavScroll();
}, true);
window.addEventListener("beforeunload", saveNavScroll);
// Сайдбар подменяется htmx (countsChanged) — прокрутку тоже надо вернуть.
document.body.addEventListener("htmx:afterSettle", (e) => {
  if (e.target && e.target.id === "sidebar") restoreNavScroll();
});

// ---- Highlight the board row whose item is open in the reading pane ----
// #list and #pane swap independently, so the list has no idea which row is
// "open". The reading pane advertises the id via [data-open-item]; we mirror it
// onto the matching row as .sel (same signal the mail list uses for .mrow.sel).
function markOpenCard() {
  document.querySelectorAll("#list .trow.sel").forEach((c) => c.classList.remove("sel"));
  const el = document.querySelector("#pane [data-open-item]");
  const id = el && el.getAttribute("data-open-item");
  if (!id) return;
  const row = document.querySelector('#list .trow[data-item-id="' + id + '"]');
  if (row) row.classList.add("sel");
}
// Instant feedback on click (the pane hasn't loaded yet); afterSettle reconciles
// from the pane once it — or the list — re-renders (board switch, filter, action,
// or opening an item from the person/prep panes or the sidebar).
document.addEventListener("click", (e) => {
  const row = e.target.closest("#list .trow[data-item-id]");
  if (!row) return;
  document.querySelectorAll("#list .trow.sel").forEach((c) => c.classList.remove("sel"));
  row.classList.add("sel");
});
// Курсор — на строку, с которой работают мышью: после перерисовки списка доска
// докручивает к курсору, и забытый курсор вверху уводил её наверх при каждом «Готово».
// Фаза перехвата — кнопки строки гасят всплытие щелчка.
document.addEventListener("click", (e) => {
  const row = e.target.closest("#list .trow[data-item-id]");
  if (row) setCursor(row);
}, true);
document.body.addEventListener("htmx:afterSettle", markOpenCard);

// A task said at a meeting opens the transcript at the moment it quotes; the highlight is
// somewhere inside 40 000 characters, so put it on screen instead of the top of the file.
document.body.addEventListener("htmx:afterSettle", (e) => {
  const hit = (e.target || document).querySelector?.("#tc-hit");
  if (hit) hit.scrollIntoView({ block: "center" });
});

// Sidebar highlight: supply `active` from the live body[data-view] whenever the sidebar
// (re)loads — on page load and on every countsChanged refresh. Board switches happen via
// pushState (no reload), so only body[data-view] knows the current board; reading it here
// keeps the highlight on the CURRENT board instead of snapping back to the load-time one.
document.body.addEventListener("htmx:configRequest", (e) => {
  const d = e.detail || {};
  if (d.elt && d.elt.id === "sidebar") {
    d.parameters.active = document.body.dataset.view || "";
  }
});

// Resolving a commitment from its open pane (Готово / Не обязательство / Отложить) fires
// openNext with the next board item — open it in the pane so you flow straight to the next
// one; if the board is now empty, reset the pane to its placeholder.
document.body.addEventListener("openNext", (e) => {
  const d = (e && e.detail) || {};
  const view = d.view || document.body.dataset.view || "today";
  if (d.id && window.htmx) {
    const row = document.querySelector('#list .trow[data-item-id="' + d.id + '"]');
    if (row) setCursor(row);
    window.htmx.ajax("GET", "/item/" + d.id + "?view=" + encodeURIComponent(view),
      { target: "#pane", swap: "innerHTML" });
  } else if (window.closePane) {
    window.closePane();
  }
});

// Reset the reading pane to its neutral placeholder (used by "Отмена" on a new
// compose, where there's no previous message to return to).
window.closePane = function () {
  const p = document.getElementById("pane");
  if (p) {
    p.innerHTML =
      '<div class="pane-empty"><div class="pane-empty-ic">✦</div>' +
      "Выберите письмо, чтобы прочитать.</div>";
  }
};

// Совет ждёт модель до минуты и всё это время держит соединение с бэкендом, а браузер
// даёт их шесть на хост: открыв подряд несколько карточек, интерфейс остаётся без
// свободного соединения и перестаёт отвечать на клики. Уходим с карточки — отменяем её
// совет: он всё равно приедет в панель, которой уже нет. htmx сам этого не делает —
// при удалении элемента он снимает слушателей, но запрос продолжает идти.
document.body.addEventListener("htmx:beforeRequest", (e) => {
  const target = e.detail && e.detail.target;
  if (!window.htmx || !target || target.id !== "pane") return;
  document
    .querySelectorAll("#pane .advice.htmx-request, #pane .advice .htmx-request")
    .forEach((el) => window.htmx.trigger(el, "htmx:abort"));
});

// Navigate the reading pane via htmx WITHOUT putting the clicked element on the
// request: a plain `hx-get` attribute on a back/cancel button makes that button
// the request source, but the same #pane swap deletes it mid-flight — and when
// the response carries an HX-Trigger (e.g. /email/{id} -> countsChanged) htmx
// then dereferences the now-removed element and throws "htmx-internal-data",
// aborting the swap so the composer stays open. Issuing the ajax from <body>
// keeps a live source element, so the swap and triggers complete cleanly.
window.paneNav = function (url) {
  if (url && window.htmx) {
    window.htmx.ajax("GET", url, { source: document.body, target: "#pane", swap: "innerHTML" });
  }
};

// --- Черновики ---
// Сохраняем только то, что человек изменил сам: иначе каждое «Ответить → Отмена»
// оставляло бы черновик из одной подписи и цитаты. «Изменил» — это отличие от того,
// что отдал сервер, а его знает сам DOM (defaultValue): снимок по событию htmx
// приходил уже после первых нажатий и объявлял набранное письмо нетронутым.
// Признак правки залипает — стёртый обратно до исходного текст тоже надо сохранить.
const DRAFT_FIELDS = ["account_id", "to", "cc", "subject", "body", "in_reply_to",
                      "references", "back_url", "forward_email_id", "draft_id"];
const DRAFT_WATCHED = ["account_id", "to", "cc", "subject", "body"];
const DRAFT_DELAY = 2000;
let _draftTimer = null;
let _draftChain = Promise.resolve();

function _draftValue(form, name) {
  const el = form.querySelector('[name="' + name + '"]');
  return el ? el.value : "";
}
function _draftFieldChanged(el) {
  if (el.tagName === "SELECT") {
    const def = el.querySelector("option[selected]");
    return def ? el.value !== def.value : false;
  }
  return el.value !== el.defaultValue;
}
function _draftDirty(form) {
  if (form.dataset.draftDirty === "1") return true;
  const changed = DRAFT_WATCHED.some((n) => {
    const el = form.querySelector('[name="' + n + '"]');
    return el ? _draftFieldChanged(el) : false;
  });
  if (!changed) return false;
  form.dataset.draftDirty = "1";
  return true;
}
function _draftPost(form, keepalive) {
  const fd = new FormData();
  DRAFT_FIELDS.forEach((n) => fd.append(n, _draftValue(form, n)));
  return fetch("/drafts/save", { method: "POST", body: fd, keepalive: !!keepalive })
    .then((r) => (r.ok ? r.json() : null))
    .then((d) => {
      const hid = form.querySelector('[name="draft_id"]');
      if (hid && d && d.draft_id) hid.value = d.draft_id;
      // Папка «Черновики» появляется в меню, только когда в ней что-то есть.
      if (d && d.created && window.htmx) window.htmx.trigger(document.body, "countsChanged");
    });
}
// Сохранения идут цепочкой: два одновременных ушли бы оба без `draft_id` и завели бы
// два черновика вместо одного. Поля читаются в момент отправки, уже с полученным id.
function _draftSave(form) {
  if (!form || !_draftDirty(form)) return;
  _draftChain = _draftChain.then(() => _draftPost(form)).catch(() => {});
}
function _draftStop() {
  clearTimeout(_draftTimer);
  _draftTimer = null;
}
// Таймер не перезапускается на каждом нажатии: сброшенный набором, он при непрерывном
// письме не срабатывал бы вовсе, и сорок секунд текста жили бы только в браузере.
document.addEventListener("input", (e) => {
  const form = e.target.closest && e.target.closest(".compose-form");
  if (!form || _draftTimer) return;
  _draftTimer = setTimeout(() => {
    _draftTimer = null;
    _draftSave(form);
  }, DRAFT_DELAY);
});
// Уход из поля — самый частый момент, когда письмо бросают: сохраняем сразу, не дожидаясь
// таймера, чтобы черновик успел появиться до перехода по ссылке.
document.addEventListener("change", (e) => {
  const form = e.target.closest && e.target.closest(".compose-form");
  if (form) _draftSave(form);
});
// Уход со страницы (по ссылке в меню, закрытие окна) — последний шанс сохранить: и
// таймер, и обычный fetch страница уносит с собой. keepalive переживает выгрузку.
window.addEventListener("pagehide", () => {
  const form = document.querySelector(".compose-form");
  if (form && _draftDirty(form)) _draftPost(form, true).catch(() => {});
});
// Отправка сама удаляет черновик на сервере; отложенное сохранение после неё воскресило
// бы его, поэтому таймер снимаем.
document.addEventListener("submit", (e) => {
  if (e.target && e.target.classList && e.target.classList.contains("compose-form")) {
    _draftStop();
  }
});

// Удалённый черновик нельзя оставлять открытым: автосохранение по нему больше не
// проходит (воскрешать удалённое оно не должно), и правки уходили бы в никуда.
document.body.addEventListener("draftDiscarded", (e) => {
  const hid = document.querySelector('.compose-form [name="draft_id"]');
  if (hid && hid.value && hid.value === (e && e.detail && e.detail.value)) {
    window.closePane();
  }
});

// Cancel/▢back from the composer: return to the previous message/thread if there
// is one, otherwise reset the pane to its placeholder. Недописанное письмо уходит в
// черновики — закрытие композера не должно терять набранное.
window.composeBack = function (url) {
  _draftStop();
  const form = document.querySelector(".compose-form");
  if (form) _draftSave(form);
  if (url) window.paneNav(url);
  else window.closePane();
};

// Quick-add (#quick-add in the board head): a successful add empties the box for the next
// line; a failed one keeps the text so nothing typed is lost.
document.body.addEventListener("htmx:afterRequest", (e) => {
  const form = e.target && e.target.closest && e.target.closest("#quick-add");
  if (form && e.detail && e.detail.successful) form.reset();
});

// Undo toast (HX-Trigger: {"showUndo": {id, label, view, prev}}): the resolving response
// hands over the item's previous state; «Отменить» posts it straight back. Stateless —
// nothing is remembered on the server.
// A payload with `url` (+ optional `values`, `target`) undoes something other than an
// item — a deleted meeting posts to its /restore.
document.body.addEventListener("showUndo", (e) => {
  const d = e.detail || {};
  if (!window.htmx || !(d.id || d.url)) return;
  showToast(d.label || "Сделано", { label: "Отменить", ms: 7000, action: () => {
    window.htmx.ajax("POST", d.url || "/item/" + d.id + "/undo", {
      target: d.target || "#list", swap: "innerHTML",
      values: d.values || {
        view: d.view || document.body.dataset.view || "today", prev: JSON.stringify(d.prev || {}),
      },
    });
  } });
});

// Generic toast trigger from the server (HX-Trigger: {"showToast": "..."}).
document.body.addEventListener("showToast", (e) => {
  if (e && e.detail) showToast(typeof e.detail === "string" ? e.detail : (e.detail.value || ""));
});

// ---- Mail context menu + drag&drop (action items / mail rows) ----
// Sidebar drop-target key -> ActionItem type.
const TYPE_LABEL = {
  MY_COMMITMENT: "Моё обещание",
  WAITING_FOR_REPLY: "Жду ответа",
  DELEGATED_TASK: "Делегировано",
  NEED_MY_REPLY: "Нужен ответ",
  TO_DELEGATE: "Передать команде",
  TASK: "Задача",
};
// Куда можно бросить строку в боковом меню: `data-drop` несёт имя типа (тогда перенос
// меняет тип) либо «snoozed»/«done». Имя типа, а не ключ доски, — чтобы работали и свои
// доски, у которых ключ произвольный (docs/ux-tasks.md §17.5).
function isDropType(drop) {
  return Object.prototype.hasOwnProperty.call(TYPE_LABEL, drop);
}

function closeContextMenu() {
  const m = document.getElementById("ctx-menu");
  if (m) m.remove();
}
document.addEventListener("click", closeContextMenu);
document.addEventListener("scroll", closeContextMenu, true);

function openContextMenu(x, y, entries) {
  closeContextMenu();
  const menu = document.createElement("div");
  menu.id = "ctx-menu";
  menu.className = "ctx-menu";
  for (const en of entries) {
    if (en.sep) {
      const s = document.createElement("div");
      s.className = "ctx-sep";
      menu.appendChild(s);
      continue;
    }
    const it = document.createElement("div");
    it.className = "ctx-item";
    it.textContent = en.label;
    it.addEventListener("click", (ev) => {
      ev.stopPropagation();
      closeContextMenu();
      en.action();
    });
    menu.appendChild(it);
  }
  menu.style.left = x + "px";
  menu.style.top = y + "px";
  document.body.appendChild(menu);
  // Keep on screen.
  const r = menu.getBoundingClientRect();
  if (r.right > window.innerWidth) menu.style.left = (x - r.width) + "px";
  if (r.bottom > window.innerHeight) menu.style.top = (y - r.height) + "px";
}

function post(url, values) {
  if (window.htmx) window.htmx.ajax("POST", url, { swap: "none", values: values || {} });
}

// Right-click a thread message (commitment card `.msg`, or the reading-pane thread
// strip `.tmsg`) -> jump to that specific email.
document.addEventListener("contextmenu", (e) => {
  const tmsg = e.target.closest(".msg[data-email-id], .tmsg[data-email-id]");
  if (!tmsg) return;
  e.preventDefault();
  const id = tmsg.dataset.emailId;
  // Carry a "back" target (the commitment card) so the opened email can offer ← Назад.
  const q = tmsg.dataset.back ? "?back=" + encodeURIComponent(tmsg.dataset.back) : "";
  const backAmp = tmsg.dataset.back ? "&back=" + encodeURIComponent(tmsg.dataset.back) : "";
  const nav = (url) => window.htmx && window.htmx.ajax("GET", url, { target: "#pane", swap: "innerHTML" });
  openContextMenu(e.pageX, e.pageY, [
    { label: "Открыть письмо", action: () => nav(`/email/${id}${q}`) },
    { label: "Ответить", action: () => nav(`/reply/${id}${q}`) },
    { label: "Ответить всем", action: () => nav(`/reply/${id}?all=1${backAmp}`) },
    { label: "Переслать", action: () => nav(`/forward/${id}${q}`) },
  ]);
});

// ---- Meetings: move to another space, delete (docs/ux-tasks.md §15) ----
// On «Встречи» every action answers with this space's list and the pane out of band (see
// the routes), so the target is #list and the list's filter rides along. Anywhere else the
// meeting pane was opened from a task (a board, a project page): there is no meetings list
// to swap, so ask for the pane alone — meetingChanged below refetches that page's own rows.
function onMeetingsPage() {
  return document.body.dataset.view === "meetings";
}
function meetingPost(id, path, values) {
  if (!window.htmx) return;
  const tabs = document.getElementById("meeting-filter");
  const req = onMeetingsPage()
    ? { target: "#list", values: { filter: (tabs && tabs.dataset.filter) || "" } }
    : { target: "#pane", values: { pane_only: "1" } };
  window.htmx.ajax("POST", "/meeting/" + id + path, {
    target: req.target, swap: "innerHTML", values: Object.assign(req.values, values || {}),
  });
}
// A meeting acted on from a task's pane: its tasks just closed, reopened or left the space,
// so the rows under the pane are stale — re-read the current page and swap its #list.
document.body.addEventListener("meetingChanged", () => {
  if (onMeetingsPage() || !window.htmx || !document.getElementById("list")) return;
  window.htmx.ajax("GET", location.pathname + location.search, {
    source: document.body, target: "#list", swap: "outerHTML", select: "#list",
  });
});
function moveEntries(spaces, current, id) {
  return spaces.filter((sp) => sp.id !== current).map((sp) => ({
    label: "Перенести в «" + sp.name + "»",
    action: () => meetingPost(id, "/space", { space_id: sp.id }),
  }));
}
// The pane's space button: a menu of the other spaces. Capture phase + stopPropagation,
// like the row's date button: the document-level click would close the menu at once.
document.addEventListener("click", (e) => {
  const btn = e.target.closest("[data-space-menu]");
  if (!btn) return;
  e.preventDefault();
  e.stopPropagation();
  let spaces = [];
  try { spaces = JSON.parse(btn.dataset.spaces || "[]"); } catch (_) { spaces = []; }
  const r = btn.getBoundingClientRect();
  openContextMenu(r.left + window.scrollX, r.bottom + window.scrollY,
    moveEntries(spaces, btn.dataset.space, btn.dataset.meeting));
}, true);
document.addEventListener("click", (e) => {
  const btn = e.target.closest("[data-meeting-delete], [data-meeting-restore]");
  if (!btn) return;
  const del = btn.dataset.meetingDelete;
  meetingPost(del || btn.dataset.meetingRestore, del ? "/delete" : "/restore");
});
// Right-click a meeting row -> open / move / delete.
document.addEventListener("contextmenu", (e) => {
  const row = e.target.closest(".mrow[data-meeting-id]");
  if (!row) return;
  e.preventDefault();
  const list = row.closest("#meeting-list");
  let spaces = [];
  try { spaces = JSON.parse((list && list.dataset.spaces) || "[]"); } catch (_) { spaces = []; }
  const id = row.dataset.meetingId;
  const entries = [{ label: "Открыть", action: () => row.click() }];
  entries.push(...moveEntries(spaces, list ? list.dataset.space : "", id));
  entries.push({ sep: true }, { label: "Удалить встречу", action: () => meetingPost(id, "/delete") });
  openContextMenu(e.pageX, e.pageY, entries);
});

// Right-click a mail row -> context menu.
document.addEventListener("contextmenu", (e) => {
  const row = e.target.closest(".mrow[data-email-id]");
  if (!row) return;
  e.preventDefault();
  const id = row.dataset.emailId;
  const unread = row.dataset.unread === "1";
  const nav = (url) => window.htmx && window.htmx.ajax("GET", url, { target: "#pane", swap: "innerHTML" });
  const entries = [
    { label: "Открыть", action: () => row.click() },
    { label: "Ответить", action: () => nav(`/reply/${id}`) },
    { label: "Ответить всем", action: () => nav(`/reply/${id}?all=1`) },
    { label: "Переслать", action: () => nav(`/forward/${id}`) },
    unread
      ? { label: "Отметить прочитанным", action: () => {
          post(`/email/${id}/read`); row.classList.remove("unread"); row.dataset.unread = "0";
        } }
      : { label: "Отметить непрочитанным", action: () => {
          post(`/email/${id}/unread`); row.classList.add("unread"); row.dataset.unread = "1";
        } },
    { sep: true },
  ];
  for (const t of ["MY_COMMITMENT", "WAITING_FOR_REPLY", "DELEGATED_TASK", "NEED_MY_REPLY"]) {
    entries.push({
      label: "Создать обязательство: " + TYPE_LABEL[t],
      action: () => post(`/item/from-email/${id}`, { type: t }),
    });
  }
  openContextMenu(e.pageX, e.pageY, entries);
});

// Drag & drop. dragData carries what is being dragged.
let dragData = null;
document.addEventListener("dragstart", (e) => {
  const task = e.target.closest(".trow[data-item-id]");
  const row = e.target.closest(".mrow[data-email-id]");
  if (task) {
    dragData = { kind: "item", id: task.dataset.itemId, view: task.dataset.view || "today" };
  } else if (row) {
    dragData = { kind: "email", id: row.dataset.emailId };
  } else {
    return;
  }
  if (e.dataTransfer) e.dataTransfer.effectAllowed = "move";
});
document.addEventListener("dragend", () => {
  dragData = null;
  document.querySelectorAll(".nav-item.drop-hot").forEach((n) => n.classList.remove("drop-hot"));
});
document.addEventListener("dragover", (e) => {
  const target = e.target.closest(".nav-item[data-drop]");
  if (!target || !dragData) return;
  // Emails can only become commitments (type targets), not snooze/archive.
  const drop = target.dataset.drop;
  if (dragData.kind === "email" && !isDropType(drop)) return;
  e.preventDefault();
  target.classList.add("drop-hot");
});
document.addEventListener("dragleave", (e) => {
  const target = e.target.closest(".nav-item[data-drop]");
  if (target) target.classList.remove("drop-hot");
});
document.addEventListener("drop", (e) => {
  const target = e.target.closest(".nav-item[data-drop]");
  if (!target || !dragData) return;
  e.preventDefault();
  target.classList.remove("drop-hot");
  const drop = target.dataset.drop;
  const d = dragData;
  dragData = null;
  if (d.kind === "item") {
    if (isDropType(drop)) {
      // Change type, refreshing the current list in place.
      window.htmx.ajax("POST", `/item/${d.id}/retype`,
        { target: "#list", swap: "innerHTML", values: { type: drop, view: d.view } });
    } else if (drop === "snoozed") {
      window.htmx.ajax("POST", `/item/${d.id}/snooze`,
        { target: "#list", swap: "innerHTML", values: { view: d.view, days: 3 } });
    } else if (drop === "done") {
      window.htmx.ajax("POST", `/item/${d.id}/complete`,
        { target: "#list", swap: "innerHTML", values: { view: d.view } });
    }
  } else if (d.kind === "email" && isDropType(drop)) {
    post(`/item/from-email/${d.id}`, { type: drop });
  }
});

// After a successful send: show a toast and return to the previous screen,
// closing the composer so the same draft can't be sent twice.
document.body.addEventListener("composeSent", (e) => {
  _draftStop();
  const d = (e && e.detail) || {};
  let note = d.to ? "✅ Отправлено: " + d.to : "✅ Отправлено";
  showToast(note);
  const pane = document.getElementById("pane");
  if (d.back && window.htmx) {
    window.htmx.ajax("GET", d.back, { target: "#pane", swap: "innerHTML" });
  } else if (pane) {
    pane.innerHTML = "";
  }
  // Отправка меняет счётчики папок: черновик уходит из своей папки.
  // Сигнал шлём отсюда: в ответе он пришёл бы на форму композера, которую эта же
  // обработка только что убрала со страницы, и до body событие бы не дошло.
  if (window.htmx) window.htmx.trigger(document.body, "countsChanged");
});

// --- Compose attachments: file picker + drag & drop ---
// A DataTransfer stored on the file input is the source of truth (so picks and drops
// accumulate); we mirror it back into input.files so HTMX submits it as multipart.
function _attInput() { return document.getElementById("compose-files"); }
function _attDT() {
  const inp = _attInput();
  if (!inp) return null;
  if (!inp._dt) inp._dt = new DataTransfer();
  return inp._dt;
}
function _attFmt(n) {
  if (n < 1024) return n + " Б";
  if (n < 1048576) return Math.round(n / 1024) + " КБ";
  return (n / 1048576).toFixed(1) + " МБ";
}
function _attRender() {
  const inp = _attInput();
  const box = document.getElementById("att-chips");
  if (!inp || !box) return;
  const dt = _attDT();
  box.innerHTML = "";
  Array.from(dt.files).forEach((f, i) => {
    const chip = document.createElement("span");
    chip.className = "att-chip";
    const name = document.createElement("span");
    name.className = "att-name";
    name.textContent = f.name;
    const size = document.createElement("span");
    size.className = "att-size";
    size.textContent = _attFmt(f.size);
    const x = document.createElement("button");
    x.type = "button";
    x.className = "att-x";
    x.title = "Убрать";
    x.textContent = "✕";
    x.addEventListener("click", () => {
      dt.items.remove(i);
      inp.files = dt.files;
      _attRender();
    });
    chip.append(name, size, x);
    box.appendChild(chip);
  });
}
function _attAdd(files) {
  const inp = _attInput();
  if (!inp) return;
  const dt = _attDT();
  Array.from(files).forEach((f) => dt.items.add(f));
  inp.files = dt.files;
  _attRender();
}
window.composeAttachOpen = function () {
  const inp = _attInput();
  if (inp) inp.click();
};
// A real user pick fires 'change'; the native FileList replaces the input's, so we
// append the new picks to our accumulator (setting .files programmatically won't
// re-fire this).
document.addEventListener("change", (e) => {
  if (e.target && e.target.id === "compose-files") _attAdd(e.target.files);
});

// Read a mailbox-id -> signature JSON map embedded in the composer.
function _sigMap(id) {
  const el = document.getElementById(id);
  try {
    return el ? JSON.parse(el.textContent || "{}") : {};
  } catch (_e) {
    return {};
  }
}
document.addEventListener("change", (e) => {
  if (!e.target || e.target.id !== "compose-account") return;
  // Swap the inline text signature in the body ("-- \n<sig>", before the quote)
  // when the "От" mailbox changes — but ONLY when the previous mailbox's block is
  // still there untouched. An edited or deleted signature is the user's word:
  // never overwrite it.
  const ta = document.getElementById("compose-body");
  const texts = _sigMap("compose-sig-texts");
  const oldText = texts[e.target.dataset.prev] || "";
  const newText = texts[e.target.value] || "";
  if (ta) {
    const oldBlock = oldText ? "-- \n" + oldText : "";
    if (oldBlock && ta.value.includes(oldBlock)) {
      ta.value = ta.value.replace(oldBlock, newText ? "-- \n" + newText : "").replace(/\n{3,}/, "\n\n");
    }
  }
  // Шрифт поля — тот, которым уйдёт письмо: он задан подписью выбранного ящика.
  if (ta) ta.style.cssText = _sigMap("compose-body-fonts")[e.target.value] || "";
  e.target.dataset.prev = e.target.value;
});
// A file dropped anywhere OUTSIDE the composer must not be handled by the webview
// itself: its default action is to open the file, and in the packaged app that replaces
// the whole window with the dropped document — Niti is simply gone until a restart.
// Both events need it: without preventDefault on dragover no drop event fires at all.
["dragover", "drop"].forEach((type) => {
  document.addEventListener(type, (e) => {
    if (!e.dataTransfer) return;
    if (!Array.from(e.dataTransfer.types || []).includes("Files")) return;
    e.preventDefault();
  });
});

// Drag files anywhere over the composer.
document.addEventListener("dragover", (e) => {
  const zone = e.target.closest && e.target.closest(".compose-form");
  if (!zone || !e.dataTransfer) return;
  if (!Array.from(e.dataTransfer.types || []).includes("Files")) return;
  e.preventDefault();
  zone.classList.add("att-drop-hot");
});
document.addEventListener("dragleave", (e) => {
  const zone = e.target.closest && e.target.closest(".compose-form");
  if (zone && !zone.contains(e.relatedTarget)) zone.classList.remove("att-drop-hot");
});
document.addEventListener("drop", (e) => {
  const zone = e.target.closest && e.target.closest(".compose-form");
  if (!zone || !e.dataTransfer || !e.dataTransfer.files.length) return;
  e.preventDefault();
  zone.classList.remove("att-drop-hot");
  _attAdd(e.dataTransfer.files);
});

// The toolbar's selects (inside #list, rendered with it). Mailbox: reload the current
// board for the chosen one; the server remembers it in a cookie for every board, so
// nothing else has to carry it. Grouping: per board, remembered server-side too.
window.boardFilterPick = function (el) {
  const view = document.body.dataset.view || "today";
  const url = "/list/" + encodeURIComponent(view) + "?account=" + encodeURIComponent(el.value || "");
  if (window.htmx) window.htmx.ajax("GET", url, { source: el, target: "#list", swap: "innerHTML" });
};
window.boardGroupPick = function (el) {
  const view = document.body.dataset.view || "today";
  const url = "/list/" + encodeURIComponent(view) + "?group=" + encodeURIComponent(el.value || "");
  if (window.htmx) window.htmx.ajax("GET", url, { source: el, target: "#list", swap: "innerHTML" });
};

// ---- Row selection + bulk bar ----
// Checked ids live here, not in the DOM: the list re-renders after every action, and a
// selection that survived a swap would point at rows that may no longer be there.
const selectedIds = new Set();
let lastCheckedIndex = null;
function toggleSelected(id, on) { if (on) selectedIds.add(id); else selectedIds.delete(id); }
// The toolbar swaps its content in place (docs/ux-tasks.md §13): the normal half hides,
// the selection half shows, same height — the rows never move.
function syncBulkBar() {
  const on = selectedIds.size > 0;
  const tools = document.getElementById("board-tools");
  const main = document.getElementById("tools-main");
  const bar = document.getElementById("bulk-bar");
  if (tools) tools.classList.toggle("selecting", on);
  if (main) main.hidden = on;
  if (bar) bar.hidden = !on;
  const n = document.getElementById("bulk-n");
  if (n) n.textContent = String(selectedIds.size);
  document.querySelectorAll("#list .trow[data-item-id]").forEach((r) => {
    r.classList.toggle("checked", selectedIds.has(r.dataset.itemId));
  });
}
function clearSelection() {
  selectedIds.clear();
  lastCheckedIndex = null;
  document.querySelectorAll("#list .trow-check").forEach((b) => { b.checked = false; });
  syncBulkBar();
}
// Capture phase: the row itself (hx-get) and its .actions span (inline stopPropagation)
// sit between the checkbox and the document, so a bubbling listener would either never
// hear the click or hear it after the pane already opened.
document.addEventListener("click", (e) => {
  const chk = e.target.closest(".trow-chk");
  if (!chk) return;
  e.stopPropagation();  // never reaches the row's hx-get
  const cb = e.target.closest(".trow-check");
  if (!cb) return;      // the label's own click; the browser follows with one on the input
  const boxes = [...document.querySelectorAll("#list .trow-check")];
  const idx = boxes.indexOf(cb);
  if (e.shiftKey && lastCheckedIndex !== null && lastCheckedIndex !== idx) {
    const [a, b] = idx < lastCheckedIndex ? [idx, lastCheckedIndex] : [lastCheckedIndex, idx];
    for (let i = a; i <= b; i++) { boxes[i].checked = cb.checked; toggleSelected(boxes[i].value, cb.checked); }
  } else {
    toggleSelected(cb.value, cb.checked);
  }
  lastCheckedIndex = idx;
  syncBulkBar();
}, true);
function bulkPost(action, extra) {
  if (!selectedIds.size || !window.htmx) return;
  const values = Object.assign(
    { ids: [...selectedIds].join(","), action, view: document.body.dataset.view || "today" }, extra || {});
  window.htmx.ajax("POST", "/items/bulk", { target: "#list", swap: "innerHTML", values });
}
document.addEventListener("click", (e) => {
  const btn = e.target.closest("#bulk-bar [data-bulk]");
  if (!btn) return;
  if (btn.dataset.bulk === "clear") { clearSelection(); return; }
  bulkPost(btn.dataset.bulk);
});
document.addEventListener("change", (e) => {
  const sel = e.target.closest("#bulk-bar [data-bulk-type]");
  if (!sel || !sel.value) return;
  bulkPost("retype", { type: sel.value });
  sel.value = "";
});

// ---- «Люди» (docs/feature-people.md): выбор строк и объединение ----
// Свой набор вместо доски: там выделение живёт на .trow и шлёт /items/bulk, здесь — на
// .prow и одно действие. Общий у них только вид полосы: половины меняются на месте.
const selectedPeople = new Set();
function syncPeopleBar() {
  const on = selectedPeople.size > 0;
  const tools = document.getElementById("people-tools");
  const main = document.getElementById("people-main");
  const bar = document.getElementById("people-sel");
  if (!tools) return;
  tools.classList.toggle("selecting", on);
  if (main) main.hidden = on;
  if (bar) bar.hidden = !on;
  const n = document.getElementById("people-n");
  if (n) n.textContent = String(selectedPeople.size);
  document.querySelectorAll("#list .prow[data-person-key]").forEach((r) => {
    r.classList.toggle("checked", selectedPeople.has(r.dataset.personKey));
  });
}
function clearPeopleSelection() {
  selectedPeople.clear();
  document.querySelectorAll("#list .prow-check").forEach((b) => { b.checked = false; });
  syncPeopleBar();
}
// Capture phase, like the board's checkbox: the row's own hx-get sits between.
document.addEventListener("click", (e) => {
  const chk = e.target.closest(".prow-chk");
  if (!chk) return;
  e.stopPropagation();
  const cb = e.target.closest(".prow-check");
  if (!cb) return;
  if (cb.checked) selectedPeople.add(cb.value); else selectedPeople.delete(cb.value);
  syncPeopleBar();
}, true);
document.addEventListener("click", (e) => {
  const btn = e.target.closest("#people-sel [data-people]");
  if (!btn) return;
  if (btn.dataset.people === "clear") { clearPeopleSelection(); return; }
  if (selectedPeople.size < 2 || !window.htmx) return;
  const q = document.querySelector("#people-tools .tools-search");
  window.htmx.ajax("POST", "/people/merge", {
    target: "#list", swap: "innerHTML",
    values: { keys: [...selectedPeople].join(","), q: (q && q.value) || "" },
  });
  selectedPeople.clear();
});
// The list re-renders after a merge or a search: the checkboxes are gone with it.
document.body.addEventListener("htmx:afterSwap", (e) => {
  const swapped = e.target && (e.target.id === "list" || e.target.id === "people-results");
  if (swapped && document.getElementById("people-tools")) {
    selectedPeople.clear();
    syncPeopleBar();
  }
});

// ---- Quick-add ghost row (docs/ux-tasks.md §13): shown on «Сегодня» / «Мои задачи»,
// revealed elsewhere by «+ Задача» or n; Esc on an empty on-demand row hides it again.
// The row re-renders with the list after a submit, so the focus is put back by hand. ----
function revealQuickAdd() {
  const form = document.getElementById("quick-add");
  if (!form) return false;
  form.hidden = false;
  const q = form.querySelector(".qadd-input");
  if (q) q.focus();
  return true;
}
document.addEventListener("click", (e) => {
  if (e.target.closest("[data-quick-add]")) revealQuickAdd();
});
document.addEventListener("keydown", (e) => {
  const q = e.target.closest && e.target.closest("#quick-add .qadd-input");
  if (!q || e.code !== "Escape") return;
  const form = q.closest("form");
  q.blur();
  if (!q.value && form.hasAttribute("data-on-demand")) form.hidden = true;
});
// Срок будущей задачи: то же меню дат, что у строки, но записывает не на сервер, а в
// скрытые поля формы — карточки ещё нет. `due_picked` отличает выбор руками от
// предзаполнения доски: набранное в строке «до пятницы» перебивает второе, но не первое.
function dueLabel(iso) {
  if (!iso) return "срок";
  const d = new Date(iso + "T00:00:00");
  const now = new Date();
  const days = Math.round((d - new Date(now.getFullYear(), now.getMonth(), now.getDate())) / 86400000);
  if (days < 0) return "просрочено";
  if (days === 0) return "сегодня";
  if (days === 1) return "завтра";
  return days + "д";
}
document.addEventListener("click", (e) => {
  const btn = e.target.closest("[data-qadd-due]");
  if (!btn) return;
  e.preventDefault();
  e.stopPropagation();
  const form = btn.closest("form");
  if (!form) return;
  const set = (iso) => {
    form.querySelector("input[name=due]").value = iso || "";
    form.querySelector("input[name=due_picked]").value = "1";
    btn.dataset.due = iso || "";
    btn.classList.toggle("set", !!iso);
    const lbl = btn.querySelector(".qadd-due-lbl");
    if (lbl) lbl.textContent = dueLabel(iso);
    const q = form.querySelector(".qadd-input");
    if (q) q.focus();
  };
  menuAt(btn, [
    { label: "Сегодня", action: () => set(isoPlusDays(0)) },
    { label: "Завтра", action: () => set(isoPlusDays(1)) },
    { label: "В понедельник", action: () => set(isoNextMonday()) },
    { label: "Через неделю", action: () => set(isoPlusDays(7)) },
    { label: "Выбрать дату…", action: () => pickDate(btn, set, btn.dataset.due) },
    { sep: true },
    { label: "Без срока", action: () => set("") },
  ]);
}, true);

// Добавленная задача не должна пропадать. Если её строка есть на этой доске — подсветить
// и подвести к ней курсор; если нет — сказать, на какой она доске, и дать туда перейти.
// Раньше обе развилки кончались одинаковым тостом, а карточка без срока уходила в конец
// «Мои задачи» — 35-й строкой на доске, которую пользователь даже не открывал.
document.body.addEventListener("taskAdded", (e) => {
  const d = (e && e.detail) || {};
  const row = d.id && document.querySelector('.trow[data-item-id="' + d.id + '"]');
  if (row) {
    setCursor(row);
    // Следующим тиком: и setCursor, и обработчик htmx:afterSwap подкручивают список «до
    // ближайшего», и их прокрутка отменяла нашу. «До ближайшего» здесь мало: строка
    // прижимается к нижней кромке окна и наполовину срезана. Уже видимую не трогаем.
    setTimeout(() => {
      const r = row.getBoundingClientRect();
      const head = document.getElementById("quick-add") || document.getElementById("board-tools");
      const top = head ? head.getBoundingClientRect().bottom : 0;
      // Без behavior:"smooth": он молча не делает ничего (проверено — scrollTop не
      // меняется), а обычная прокрутка работает. Тот же сорт обещания, что showPicker().
      if (r.top < top || r.bottom > window.innerHeight - 8) {
        row.scrollIntoView({ block: "center" });
      }
    }, 0);
    row.classList.add("flash");
    setTimeout(() => row.classList.remove("flash"), 1200);
    return;
  }
  showToast("В «" + (d.board_name || "") + "»: " + (d.title || "") + (d.when || ""), {
    label: "Открыть", ms: 7000,
    action: () => {
      const nav = document.querySelector('.nav [data-board="' + d.board_key + '"]');
      if (nav) nav.click(); else window.location = "/view/" + d.board_key;
    },
  });
});

let quickAddPending = false;
document.body.addEventListener("htmx:beforeRequest", (e) => {
  const elt = e.detail && e.detail.elt;
  if (elt && elt.id === "quick-add") quickAddPending = true;
});
document.body.addEventListener("htmx:afterRequest", (e) => {
  const elt = e.detail && e.detail.elt;
  // Nothing was added (blank line -> 204): the next list swap is somebody else's.
  if (elt && elt.id === "quick-add" && !(e.detail.xhr && e.detail.xhr.status === 200)) {
    quickAddPending = false;
  }
});

// ---- Keyboard on boards: j/k cursor, Enter open, e done, s snooze, # dismiss, x check,
// n quick-add, Esc clears the selection. Keys are matched by e.code so they work on the
// Russian layout too. ----
let cursorId = null;
let cursorIndex = -1;
let cursorNextIds = [];  // соседи курсора ниже и выше — кто займёт его место, если он уйдёт
function boardRows() { return [...document.querySelectorAll("#list .trow[data-item-id]:not(.static)")]; }
function cursorRow() {
  return cursorId ? document.querySelector('#list .trow[data-item-id="' + cursorId + '"]') : null;
}
function setCursor(row) {
  document.querySelectorAll("#list .trow.cur").forEach((r) => r.classList.remove("cur"));
  if (!row) return;
  row.classList.add("cur");
  cursorId = row.dataset.itemId;
  const rows = boardRows();
  cursorIndex = rows.indexOf(row);
  cursorNextIds = [rows[cursorIndex + 1], rows[cursorIndex - 1]]
    .filter(Boolean).map((r) => r.dataset.itemId);
  row.scrollIntoView({ block: "nearest" });
}
// One item action from anywhere — a row, the pane, a menu. The list re-renders; `then`
// runs after the swap (the pane reloads itself when the item is still open there).
function postItem(id, path, values, then) {
  if (!window.htmx) return;
  const p = window.htmx.ajax("POST", "/item/" + id + path, {
    target: "#list", swap: "innerHTML",
    values: Object.assign({ view: document.body.dataset.view || "today" }, values || {}),
  });
  if (then && p && p.then) p.then(then);
}
function itemPost(row, path, values) { postItem(row.dataset.itemId, path, values); }
function reloadPane(id) {
  const open = document.querySelector("#pane [data-open-item]");
  if (!open || open.dataset.openItem !== id || !window.htmx) return;
  const view = document.body.dataset.view || "today";
  window.htmx.ajax("GET", "/item/" + id + "?view=" + encodeURIComponent(view),
    { target: "#pane", swap: "innerHTML" });
}
// The item a button belongs to: its row, or the pane it sits in.
function itemOf(btn) {
  const row = btn.closest(".trow[data-item-id]");
  if (row) return row.dataset.itemId;
  const pane = btn.closest("[data-open-item]");
  return pane ? pane.dataset.openItem : null;
}
// UTC calendar days, to match the server's notion of «today».
function isoPlusDays(n) {
  const d = new Date();
  d.setUTCDate(d.getUTCDate() + n);
  return d.toISOString().slice(0, 10);
}
function isoNextMonday() {
  const d = new Date();
  const ahead = ((1 - d.getUTCDay()) + 7) % 7 || 7;
  d.setUTCDate(d.getUTCDate() + ahead);
  return d.toISOString().slice(0, 10);
}
function menuAt(btn, entries) {
  const r = btn.getBoundingClientRect();
  openContextMenu(r.left + window.scrollX, r.bottom + window.scrollY, entries);
}
document.addEventListener("keydown", (e) => {
  if (e.metaKey || e.ctrlKey || e.altKey) return;
  const t = e.target;
  const tag = t && t.tagName;
  // Typing somewhere: leave the keys alone. A focused row checkbox (just clicked) is
  // not typing — Esc and the row keys must still work from it.
  const typing = (tag === "INPUT" && t.type !== "checkbox") || tag === "TEXTAREA"
    || tag === "SELECT" || (t && t.isContentEditable);
  if (typing) return;
  if (!document.getElementById("list")) return;
  if (e.code === "KeyN") {
    if (revealQuickAdd()) e.preventDefault();
    return;
  }
  if (e.code === "Escape") {
    if (selectedIds.size) { clearSelection(); e.preventDefault(); }
    if (selectedPeople.size) { clearPeopleSelection(); e.preventDefault(); }
    return;
  }
  const rows = boardRows();
  if (!rows.length) return;
  const cur = cursorRow();
  const i = cur ? rows.indexOf(cur) : -1;
  switch (e.code) {
    case "KeyJ": setCursor(rows[Math.min(i + 1, rows.length - 1)]); break;
    case "KeyK": setCursor(rows[Math.max(i - 1, 0)]); break;
    case "Enter": if (!cur) return; cur.click(); break;
    case "KeyE": if (!cur) return; itemPost(cur, "/complete"); break;
    case "KeyS": if (!cur) return; itemPost(cur, "/snooze", { days: 3 }); break;
    case "Digit3": if (!cur || !e.shiftKey) return; itemPost(cur, "/dismiss"); break;
    case "KeyX": {
      const cb = cur && cur.querySelector(".trow-check");
      if (!cb) return;
      cb.checked = !cb.checked;
      toggleSelected(cb.value, cb.checked);
      syncBulkBar();
      break;
    }
    default: return;
  }
  e.preventDefault();
});
// After the list re-renders: the selection is gone with the old rows; the cursor stays
// on the same item if it is still listed, else on the row that took its place.
// После действия над карточкой доска остаётся на месте: сама замена списка сдвигает
// прокрутку (привязка прокрутки цепляется за удалённые строки), поэтому место возвращаем
// руками, а потом — к курсору. Переход на другую доску (GET) открывает её сверху.
let listScroll = null;
document.body.addEventListener("htmx:beforeSwap", (e) => {
  const list = e.detail && e.detail.target;
  const verb = ((e.detail.requestConfig || {}).verb || "").toLowerCase();
  const scroller = list && list.id === "list" && verb === "post" ? list.closest(".content") : null;
  listScroll = scroller ? [scroller, scroller.scrollTop] : null;
});
document.body.addEventListener("htmx:afterSwap", (e) => {
  if (!e.target || e.target.id !== "list") return;
  if (listScroll) { listScroll[0].scrollTop = listScroll[1]; listScroll = null; }
  selectedIds.clear();
  lastCheckedIndex = null;
  syncBulkBar();
  if (quickAddPending) { quickAddPending = false; revealQuickAdd(); }
  const rows = boardRows();
  const byId = (id) => rows.find((r) => r.dataset.itemId === id);
  // Номер строки не годится: разделы доски перестраиваются, и под тем же номером
  // оказывается карточка из другого места — доска уезжала к ней.
  const heir = cursorNextIds.map(byId).find(Boolean);
  setCursor(cursorRow() || heir ||
    (cursorIndex >= 0 ? rows[Math.min(cursorIndex, rows.length - 1)] : null) || null);
});

// ---- Deadline popover from the row's date button ----
// Capture phase for the same reason as the checkbox: .actions stops propagation inline.
document.addEventListener("click", (e) => {
  const btn = e.target.closest("[data-due-btn]");
  if (!btn) return;
  e.preventDefault();
  e.stopPropagation();
  const id = itemOf(btn);
  if (!id) return;
  // From the pane the date is on screen there too, so the pane reloads after the list.
  const inPane = !btn.closest(".trow");
  const setDue = (iso) => postItem(id, "/due", { until: iso }, inPane ? () => reloadPane(id) : null);
  menuAt(btn, [
    { label: "Сегодня", action: () => setDue(isoPlusDays(0)) },
    { label: "Завтра", action: () => setDue(isoPlusDays(1)) },
    { label: "В понедельник", action: () => setDue(isoNextMonday()) },
    { label: "Через неделю", action: () => setDue(isoPlusDays(7)) },
    { label: "Выбрать дату…", action: () => pickDate(btn, setDue, btn.dataset.due) },
    { sep: true },
    { label: "Без срока", action: () => setDue("") },
  ]);
}, true);

// ---- The pane's menus (docs/ux-tasks.md §15): snooze presets; the rare and the
// destructive under «⋯». Both post like the row actions, with advance=1 where the item
// leaves the board — the server then opens the next one (openNext). ----
document.addEventListener("click", (e) => {
  const btn = e.target.closest("[data-snooze-menu]");
  if (!btn) return;
  e.preventDefault();
  e.stopPropagation();
  const id = itemOf(btn);
  if (!id) return;
  const snooze = (values) => postItem(id, "/snooze", Object.assign({ advance: 1 }, values));
  menuAt(btn, [
    { label: "На 3 дня", action: () => snooze({ days: 3 }) },
    { label: "На неделю", action: () => snooze({ days: 7 }) },
    { label: "До понедельника", action: () => snooze({ until: isoNextMonday() }) },
    { label: "До даты…", action: () => pickDate(btn, (iso) => snooze({ until: iso })) },
  ]);
}, true);
document.addEventListener("click", (e) => {
  const btn = e.target.closest("[data-more-menu]");
  if (!btn) return;
  e.preventDefault();
  e.stopPropagation();
  const id = itemOf(btn);
  if (!id) return;
  const entries = [
    { label: "Не обязательство", action: () => postItem(id, "/dismiss", { advance: 1 }) },
    { sep: true },
  ];
  for (const t of Object.keys(TYPE_LABEL)) {
    if (t === btn.dataset.type) continue;
    entries.push({
      label: "Сменить тип: " + TYPE_LABEL[t],
      action: () => postItem(id, "/retype", { type: t }, () => reloadPane(id)),
    });
  }
  entries.push({ sep: true }, { label: "Исправить…", action: () => {
    const d = document.querySelector("#pane details.edit");
    if (!d) return;
    d.open = true;
    d.scrollIntoView({ block: "nearest" });
    const f = d.querySelector("input, select, textarea");
    if (f) f.focus();
  } });
  menuAt(btn, entries);
}, true);
// «Выбрать дату…»: поле даты в таком же поповере, как само меню, у той же кнопки.
// Раньше сюда подставлялся невидимый input (opacity:0, pointer-events:none) и вызывался
// showPicker(); если он не открывал календарь, пользователь не видел НИЧЕГО — и каждый
// клик оставлял в body осиротевший input (focus он не получал, blur не приходил, снять
// его было некому). Видимое поле показывает родной календарь по клику само.
function pickDate(anchor, cb, current) {
  closeContextMenu();
  const pop = document.createElement("div");
  pop.id = "ctx-menu";
  pop.className = "ctx-menu ctx-date";
  pop.addEventListener("click", (ev) => ev.stopPropagation());

  const inp = document.createElement("input");
  inp.type = "date";
  inp.className = "ctx-date-input";
  inp.value = current || "";
  const ok = document.createElement("button");
  ok.type = "button";
  ok.className = "btn primary";
  ok.textContent = "Готово";
  const apply = () => { const v = inp.value; closeContextMenu(); if (v) cb(v); };
  ok.addEventListener("click", apply);
  inp.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter") { ev.preventDefault(); apply(); }
    if (ev.key === "Escape") { ev.preventDefault(); closeContextMenu(); }
  });
  pop.appendChild(inp);
  pop.appendChild(ok);

  const r = anchor.getBoundingClientRect();
  pop.style.left = (r.left + window.scrollX) + "px";
  pop.style.top = (r.bottom + window.scrollY) + "px";
  document.body.appendChild(pop);
  const box = pop.getBoundingClientRect();
  if (box.right > window.innerWidth) pop.style.left = (r.right + window.scrollX - box.width) + "px";
  if (box.bottom > window.innerHeight) pop.style.top = (r.top + window.scrollY - box.height) + "px";
  inp.focus();
}

// Commitment-board switching (sidebar "ИИ-рекомендации" items). When we're already on a
// board layout, swap ONLY #list (the toolbar comes with it) and leave the board head with
// its banners mounted. When we're NOT on a board (e.g. in Mail or Settings), fall back to
// a full navigation so the board head is built correctly.
document.addEventListener("click", (e) => {
  const a = e.target.closest(".nav [data-board]");
  if (!a) return;
  e.preventDefault();
  const href = a.getAttribute("href");
  const key = a.dataset.board;
  const head = document.getElementById("board-head");
  const onBoard = head && head.querySelector("#board-banners");
  if (!onBoard || !window.htmx) { window.location = href; return; }
  // Move the active highlight and title in place (no sidebar re-render, so ПОЧТА stays).
  document.querySelectorAll(".nav .nav-item.active").forEach((n) => n.classList.remove("active"));
  a.classList.add("active");
  const label = a.querySelector(".nav-label");
  const h1 = document.querySelector(".main-head h1");
  if (h1 && label) h1.textContent = label.textContent.trim();
  // Record the current board so the shared filter chips target the right list, and keep
  // the sidebar's own refresh (on countsChanged) highlighting the right item.
  document.body.dataset.view = key;
  // The sidebar reads body[data-view] at request time (htmx:configRequest below) to
  // decide which board to highlight, so just keeping data-view current is enough.
  // The mailbox choice is the server's (cookie): the list comes back filtered as before.
  const url = "/list/" + encodeURIComponent(key);
  window.htmx.ajax("GET", url, { source: a, target: "#list", swap: "innerHTML" });
  history.pushState({}, "", href);
});

// Board switches move the URL via history.pushState (not htmx history), so a browser Back
// onto a board URL must do a real load to rebuild the board head. Settings tabs use htmx's
// own history (hx-push-url) and are restored by htmx, so we only handle /view/* here.
window.addEventListener("popstate", () => {
  if (location.pathname.startsWith("/view/")) window.location.reload();
});

// --- Rich signature editor (Settings → Подписи) ---
// A contenteditable WYSIWYG whose HTML is mirrored into a hidden <textarea name=body>
// so the form submits it. Formatting via execCommand (fine in WKWebView/Safari).
function _sigEditorSync(editor) {
  const ta = editor.closest("form")?.querySelector("textarea[name=body]");
  if (ta) ta.value = editor.innerHTML;
}
document.addEventListener("input", (e) => {
  if (e.target.classList && e.target.classList.contains("sig-editor")) _sigEditorSync(e.target);
});
// Keep the editor's selection when clicking a toolbar button (buttons steal focus).
document.addEventListener("mousedown", (e) => {
  const b = e.target.closest && e.target.closest(".sig-btn");
  if (b) e.preventDefault();
});
// Track the caret/selection inside a signature editor so toolbar controls that steal
// focus (color picker, selects) still apply to what was selected.
let _sigRange = null;
document.addEventListener("selectionchange", () => {
  const s = window.getSelection();
  if (!s.rangeCount) return;
  const n = s.anchorNode;
  const el = n && (n.nodeType === 1 ? n : n.parentElement);
  if (el && el.closest && el.closest(".sig-editor")) _sigRange = s.getRangeAt(0).cloneRange();
});
function _sigExec(toolbar, cmd, value) {
  const ed = toolbar.parentElement.querySelector(".sig-editor");
  if (ed) {
    ed.focus();
    if (_sigRange) { const s = window.getSelection(); s.removeAllRanges(); s.addRange(_sigRange); }
  }
  document.execCommand(cmd, false, value);
  if (ed) _sigEditorSync(ed);
}
document.addEventListener("click", (e) => {
  const b = e.target.closest && e.target.closest(".sig-btn[data-cmd]");
  if (!b) return;
  e.preventDefault();
  _sigExec(b.closest(".sig-toolbar"), b.dataset.cmd, null);
});
document.addEventListener("change", (e) => {
  const el = e.target;
  const tb = el.closest && el.closest(".sig-toolbar");
  if (!tb) return;
  if (el.classList.contains("sig-color")) _sigExec(tb, "foreColor", el.value);
  else if (el.classList.contains("sig-font") && el.value) _sigExec(tb, "fontName", el.value);
  else if (el.classList.contains("sig-size") && el.value) _sigExec(tb, "fontSize", el.value);
});

// Native folder picker (Swift bridge). The shell always calls back into the same
// window.__setProjectFolder, so which field is being filled is remembered here — that
// way the same panel serves the per-project folder and the shared projects root without
// the Swift side having to know about either. In a plain browser (no bridge) the user
// types the path.
let _folderFieldId = "project-folder";
window.__setProjectFolder = function (path) {
  const el = document.getElementById(_folderFieldId);
  if (el && path) el.value = path;
};
function pickProjectFolder(fieldId) {
  _folderFieldId = fieldId || "project-folder";
  const bridge = window.webkit && window.webkit.messageHandlers
    && window.webkit.messageHandlers.pickFolder;
  if (bridge) {
    bridge.postMessage({});
  } else {
    const el = document.getElementById(_folderFieldId);
    if (el) {
      el.focus();
      alert("Введите путь к папке вручную (нативный выбор доступен в приложении).");
    }
  }
}

// ---- Recipient autocomplete («Кому» / «Копия») ----
// Адреса берутся из переписки (email_reader/contacts.py). Поле держит список через
// запятую, поэтому подсказка смотрит на ПОСЛЕДНЮЮ позицию и в неё же подставляет.
const AC_DELAY = 200;
let acTimer = null;

function acDrop(input) {
  const box = input.closest(".ac");
  return box ? box.querySelector(".ac-drop") : null;
}
function acLastToken(value) {
  const cut = Math.max(value.lastIndexOf(","), value.lastIndexOf(";"));
  return value.slice(cut + 1).trim();
}
function acClose(input) {
  const drop = acDrop(input);
  if (drop) drop.innerHTML = "";
}
function acItems(input) {
  const drop = acDrop(input);
  return drop ? Array.from(drop.querySelectorAll(".ac-item")) : [];
}
function acMove(input, step) {
  const items = acItems(input);
  if (!items.length) return false;
  const at = items.findIndex((x) => x.classList.contains("ac-cur"));
  const next = (at + step + items.length) % items.length;
  items.forEach((x) => x.classList.remove("ac-cur"));
  items[next].classList.add("ac-cur");
  items[next].scrollIntoView({ block: "nearest" });
  return true;
}
function acAccept(input, item) {
  if (!item) return false;
  const email = item.dataset.email;
  const cut = Math.max(input.value.lastIndexOf(","), input.value.lastIndexOf(";"));
  const head = cut < 0 ? "" : input.value.slice(0, cut + 1) + " ";
  input.value = head + email + ", ";
  acClose(input);
  input.focus();
  input.setSelectionRange(input.value.length, input.value.length);
  return true;
}
async function acFetch(input) {
  const q = acLastToken(input.value);
  if (q.length < 2) return acClose(input);
  try {
    const r = await fetch("/compose/recipients?q=" + encodeURIComponent(q));
    const drop = acDrop(input);
    if (drop && document.activeElement === input) drop.innerHTML = await r.text();
  } catch (e) { /* нет ответа — просто нет подсказки */ }
}

document.addEventListener("input", (e) => {
  const input = e.target.closest && e.target.closest("input[data-recipients]");
  if (!input) return;
  clearTimeout(acTimer);
  acTimer = setTimeout(() => acFetch(input), AC_DELAY);
});
document.addEventListener("keydown", (e) => {
  const input = e.target.closest && e.target.closest("input[data-recipients]");
  if (!input) return;
  const open = acItems(input).length > 0;
  if (e.key === "ArrowDown" && open) { e.preventDefault(); acMove(input, 1); }
  else if (e.key === "ArrowUp" && open) { e.preventDefault(); acMove(input, -1); }
  else if (e.key === "Escape" && open) { e.preventDefault(); acClose(input); }
  else if ((e.key === "Enter" || e.key === "Tab") && open) {
    // Enter в поле «Кому» иначе отправит форму с недописанным адресом.
    if (acAccept(input, acDrop(input).querySelector(".ac-item.ac-cur"))) e.preventDefault();
  }
});
document.addEventListener("mousedown", (e) => {
  const item = e.target.closest && e.target.closest(".ac-item");
  if (item) {
    e.preventDefault();  // не терять фокус до подстановки
    acAccept(item.closest(".ac").querySelector("input[data-recipients]"), item);
    return;
  }
  if (!(e.target.closest && e.target.closest(".ac"))) {
    document.querySelectorAll(".ac-drop").forEach((d) => { d.innerHTML = ""; });
  }
});

// ---- Раскрытие беседы в почтовом списке ----
// Группировка по теме показывает одну строку на беседу; стрелка раскрывает её письма
// на месте. Письма грузятся один раз, дальше строка просто прячется и показывается.
// Перехват, а не всплытие: обработчик строки («открыть письмо») висит на самой строке
// и сработал бы раньше, чем всплытие дошло бы до документа.
document.addEventListener("click", async (e) => {
  const btn = e.target.closest && e.target.closest(".mrow-exp");
  if (!btn) return;
  e.stopPropagation();
  e.preventDefault();
  const row = btn.closest("li");
  const kids = row && row.nextElementSibling;
  if (!kids || !kids.classList.contains("mrow-kids")) return;
  const open = !kids.hidden;
  if (open) {
    kids.hidden = true;
    btn.setAttribute("aria-expanded", "false");
    row.classList.remove("expanded");
    return;
  }
  if (!kids.innerHTML.trim()) {
    const box = document.body.dataset.view === "sent" ? "sent" : "inbox";
    kids.innerHTML = "<div class='mrow-kids-wait'>Загружаю…</div>";
    try {
      const r = await fetch(`/mail/thread/${btn.dataset.thread}/rows?box=${box}`);
      kids.innerHTML = await r.text();
      // htmx не знает про узлы, вставленные мимо него: без этого hx-get на строках
      // беседы мёртв — клик подсвечивал строку, а справа оставалось прежнее письмо.
      if (window.htmx) window.htmx.process(kids);
    } catch (err) {
      kids.innerHTML = "<div class='mrow-kids-wait'>Не удалось загрузить беседу</div>";
    }
  }
  kids.hidden = false;
  btn.setAttribute("aria-expanded", "true");
  row.classList.add("expanded");
}, true);

// ---- Прочтение письма гасит точку у всей беседы ----
// Головная строка группы — это последнее письмо беседы, и она же продублирована первой
// внутри группы. Пока страницу не перезагрузили, прочтение одной строки не касалось
// другой: точка висела над раскрытой и уже вычитанной группой.
document.addEventListener("click", (e) => {
  const row = e.target.closest && e.target.closest(".mrow[data-email-id]");
  if (!row || (e.target.closest && e.target.closest(".mrow-exp"))) return;
  const id = row.dataset.emailId;
  document.querySelectorAll(`.mrow[data-email-id="${id}"]`).forEach((twin) => {
    twin.classList.remove("unread");
    twin.dataset.unread = "0";
  });
  const kids = row.classList.contains("sub") ? row.closest(".mrow-kids") : null;
  if (!kids) return;
  if (!kids.querySelector(".mrow.unread")) {
    const head = kids.previousElementSibling;
    if (head && head.classList.contains("mrow")) {
      head.classList.remove("unread");
      head.dataset.unread = "0";
    }
  }
}, false);


// ---- Кнопка «скопировать» (макрос copy_value) ----
// Значение берём из data-copy, а не из текста рядом: в разметке оно может быть перенесено
// и обрезано, а в буфер должно лечь целиком. На localhost clipboard API доступен, но в
// старом WebView его может не быть — тогда прячем textarea и копируем через execCommand.
function copyText(text) {
  // Clipboard API требует «жеста пользователя» и отказывает, например, когда клик
  // пришёл из скрипта; execCommand в этом случае ещё работает, поэтому не «или», а
  // «сначала одно, потом другое».
  if (navigator.clipboard && navigator.clipboard.writeText) {
    return navigator.clipboard.writeText(text).catch(() => legacyCopy(text));
  }
  return legacyCopy(text);
}

function legacyCopy(text) {
  return new Promise((resolve, reject) => {
    const box = document.createElement("textarea");
    box.value = text;
    box.setAttribute("readonly", "");
    box.style.position = "fixed";
    box.style.opacity = "0";
    document.body.appendChild(box);
    box.select();
    const ok = document.execCommand && document.execCommand("copy");
    document.body.removeChild(box);
    ok ? resolve() : reject(new Error("copy failed"));
  });
}

document.body.addEventListener("click", (e) => {
  const btn = e.target.closest && e.target.closest("[data-copy]");
  if (!btn) return;
  e.preventDefault();
  copyText(btn.getAttribute("data-copy") || "").then(() => {
    btn.classList.add("copied");
    setTimeout(() => btn.classList.remove("copied"), 1500);
  });
});

// ---- Календарь: открыть сетку на первом часе ----
// Сетка на все сутки выше экрана, а встречи начинаются утром. Прокручиваем .content так,
// чтобы час из data-first-hour (его считает сервер) встал сразу под липкой шапкой недели.
function scrollCalendar(root) {
  const cal = (root || document).querySelector(".cal[data-first-hour]");
  if (!cal) return;
  const scroller = cal.closest(".content");
  const grid = cal.querySelector(".cal-grid");
  if (!scroller || !grid) return;
  const head = cal.querySelector(".cal-top");
  const hour = parseInt(cal.dataset.firstHour || "8", 10);
  const target = grid.getBoundingClientRect().top + (grid.offsetHeight / 24) * hour;
  const edge = scroller.getBoundingClientRect().top + (head ? head.offsetHeight : 0);
  scroller.scrollTop += target - edge;
}

scrollCalendar();
// Закрыть событие: панель пустеет (на экране календаря она при этом прячется), и
// отметка на блоке снимается — иначе в сетке подсвечено то, чего уже нигде не видно.
window.closeEvent = function () {
  document.querySelectorAll(".cal-ev.sel").forEach((b) => b.classList.remove("sel"));
  window.closePane();
};

// Открытое в панели событие отмечено в сетке — как выбранная строка в любом списке.
document.body.addEventListener("click", (e) => {
  const block = e.target.closest && e.target.closest(".cal-ev");
  if (!block) return;
  document.querySelectorAll(".cal-ev.sel").forEach((b) => b.classList.remove("sel"));
  block.classList.add("sel");
});
document.body.addEventListener("htmx:afterSwap", (e) => {
  const target = e.detail && e.detail.target;
  if (target && target.id === "list") scrollCalendar(target);
});

// ---- Календарь: клавиши как в Календаре macOS ----
// ← → — назад и вперёд, T — сегодня, 1 2 3 — день, неделя, месяц, Esc — закрыть событие
// или календарик. Работают, только когда календарь на экране и курсор не в поле ввода.
const CAL_KEYS = {
  ArrowLeft: "prev", ArrowRight: "next", KeyT: "today",
  Digit1: "day", Digit2: "week", Digit3: "month", KeyN: "new",
};
document.addEventListener("keydown", (e) => {
  if (e.metaKey || e.ctrlKey || e.altKey) return;
  const cal = document.querySelector("#list .cal");
  if (!cal) return;
  const t = e.target;
  const tag = t && t.tagName;
  if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || (t && t.isContentEditable)) return;
  const palette = document.getElementById("palette");
  if (palette && !palette.hidden) return;
  if (e.code === "Escape") {
    const pick = cal.querySelector(".cal-pick[open]");
    if (pick) { pick.open = false; e.preventDefault(); return; }
    if (document.querySelector("#pane [data-event-id]")) { window.closeEvent(); e.preventDefault(); }
    return;
  }
  const key = CAL_KEYS[e.code];
  const button = key && cal.querySelector('[data-cal-key="' + key + '"]');
  if (!button) return;
  e.preventDefault();
  button.click();
});

// Шапка календаря липкая, и фокус на её кнопке заставляет окно «показать» кнопку — то
// есть прокрутить сетку к её исходному месту, в самый верх, на 00:00. Щелчок мышью
// фокус не берёт; сам щелчок (и раскрытие календарика) срабатывает как обычно.
document.addEventListener("mousedown", (e) => {
  const el = e.target.closest && e.target.closest(".cal-top button, .cal-top summary");
  if (el) e.preventDefault();
});

// Календарик перехода на дату закрывается щелчком мимо него, как любое всплывающее меню.
document.addEventListener("click", (e) => {
  const open = document.querySelector(".cal-pick[open]");
  if (open && !open.contains(e.target)) open.open = false;
});


// ---- Календарь: новое событие щелчком или протяжкой по пустому месту (feature-calendar §11) ----
// Неделя и день: время — по высоте столбца (сутки = вся высота), шаг 15 минут; щелчок —
// час от этого места, протяжка — отрезок. Месяц: щелчок по пустому месту дня — 10:00.
function calTime(day, y) {
  const r = day.getBoundingClientRect();
  const min = Math.max(0, Math.min(1439, Math.round(((y - r.top) / r.height) * 1440 / 15) * 15));
  return String(Math.floor(min / 60)).padStart(2, "0") + ":" + String(min % 60).padStart(2, "0");
}
function calNew(params) {
  if (!window.htmx) return;
  window.htmx.ajax("GET", "/calendar/event/new?" + new URLSearchParams(params).toString(),
    { target: "#pane", swap: "innerHTML" });
}
let calDrag = null;
document.addEventListener("mousedown", (e) => {
  const day = e.target.closest && e.target.closest("#list .cal-day[data-date]");
  if (!day || e.button !== 0 || e.target.closest(".cal-ev, .cal-now")) return;
  calDrag = { day, y: e.clientY };
});
document.addEventListener("mouseup", (e) => {
  if (!calDrag) return;
  const { day, y } = calDrag;
  calDrag = null;
  let from = calTime(day, Math.min(y, e.clientY));
  let to = calTime(day, Math.max(y, e.clientY));
  if (to <= from) {
    const [h, m] = from.split(":").map(Number);
    to = String(Math.min(h + 1, 23)).padStart(2, "0") + ":" + String(m).padStart(2, "0");
  }
  calNew({ date: day.dataset.date, start: from, end: to });
});
document.addEventListener("click", (e) => {
  const cell = e.target.closest && e.target.closest("#list .cal-mday[data-date]");
  if (!cell || e.target.closest("button, a")) return;
  calNew({ date: cell.dataset.date, start: "10:00", end: "11:00" });
});
// «Весь день» прячет время: у такого события его нет.
document.addEventListener("change", (e) => {
  if (!e.target.matches || !e.target.matches("[data-ev-allday]")) return;
  const form = e.target.closest("form");
  form.querySelectorAll("[data-ev-time]").forEach((i) => { i.hidden = e.target.checked; });
});
