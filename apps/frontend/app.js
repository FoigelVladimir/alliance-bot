import { load, save, reset, canWork, canReply, searchSources } from './store.js';

let state = load();
let actor = 'manager';
let selected = null;
let filter = 'queue';
let search = '';
let sourceQuery = '';
const drafts = new Map();
const app = document.querySelector('#app');
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
const person = () => state.people.find(p => p.id === actor);
const roleName = role => role === 'admin' ? 'Администратор · демо' : 'Менеджер · демо';
const label = (text, tone = '') => '<span class="badge ' + tone + '">' + esc(text) + '</span>';
const empty = text => '<div class="empty">' + esc(text) + '</div>';
const action = (name, text, extra = '', style = '') => '<button type="button" data-action="' + name + '" ' + extra + ' class="' + style + '">' + text + '</button>';
function notify(text) {
  document.querySelector('#notice').textContent = text;
  clearTimeout(notify.timer);
  notify.timer = setTimeout(() => { document.querySelector('#notice').textContent = ''; }, 6000);
}
function commit(change) {
  const before = structuredClone(state);
  change();
  try { save(state); return true; }
  catch { state = before; notify('Не удалось сохранить в браузере. Изменение не применено.'); return false; }
}
function icon(path) { return '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" aria-hidden="true"><path d="' + path + '"/></svg>'; }
const links = [
  ['home', 'Главная', 'M3 11 12 3l9 8M5 10v10h5v-6h4v6h5V10'],
  ['inbox', 'Обращения', 'M4 4h16v13H9l-5 4V4Z'],
  ['knowledge', 'База знаний', 'M4 4h7v16H4V4Zm9 0h7v16h-7V4Z'],
  ['staff', 'Сотрудники', 'M16 21v-3a4 4 0 0 0-8 0v3M12 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8Z']
];
function heading(title, subtitle, button = '') { return '<div class="heading"><div><h1>' + title + '</h1><p>' + subtitle + '</p></div>' + button + '</div>'; }
function render() {
  const route = location.hash.slice(1).split('/')[0] || 'home';
  const user = person();
  const demo = '<div class="demo"><div><strong>Демонстрационный кабинет</strong> · синтетические данные, без ВК и AI</div><div class="demo-controls"><label for="actor">Просмотр от лица</label><select id="actor">' + state.people.map(p => '<option value="' + p.id + '" ' + (actor === p.id ? 'selected' : '') + '>' + esc(p.name) + '</option>').join('') + '</select>' + action('reset', 'Сбросить демо') + '</div></div>';
  let body;
  if (!canWork(user)) body = accessView(user);
  else {
    const nav = links.filter(([id]) => id !== 'staff' || user.role === 'admin').map(([id, text, path]) => '<a href="#' + id + '" ' + (route === id || route === 'material' && id === 'knowledge' ? 'aria-current="page"' : '') + ' title="' + text + '">' + icon(path) + '<span class="nav-label">' + text + '</span></a>').join('');
    const content = route === 'home' ? homeView() : route === 'inbox' ? inboxView() : route === 'knowledge' ? knowledgeView() : route === 'material' ? materialView() : route === 'staff' ? staffView() : heading('Страница не найдена', 'Откройте нужный раздел через меню.');
    body = '<div class="layout"><aside><div class="brand"><span class="logo">А</span><div class="brand-text"><strong>АЛЬЯНС</strong><small>Кабинет поддержки</small></div></div><nav class="nav" aria-label="Разделы кабинета">' + nav + '</nav><div class="account"><strong>' + esc(user.name) + '</strong><small><br>' + roleName(user.role) + '</small>' + action('logout', 'Выход') + '</div></aside><div class="workspace"><header class="topbar"><strong>Спортивный клуб · первая очередь</strong><span>ВК и модель не подключены</span></header><main id="main" tabindex="-1">' + content + '</main></div></div>';
  }
  app.innerHTML = demo + body;
  document.title = 'Альянс · ' + (links.find(l => l[0] === route)?.[1] || 'Кабинет') + ' · демо';
}
function accessView(user) {
  const text = !user ? 'Выберите демонстрационный профиль. Настоящий вход VK ID ещё не подключён.' : user.status === 'pending' ? 'Заявка отправлена. Рабочие разделы откроются после одобрения.' : user.status === 'blocked' ? 'Рабочий доступ заблокирован.' : 'Заявка отклонена. Уточните доступ у руководителя клуба.';
  return '<main id="main" class="login panel" tabindex="-1"><div class="logo" style="display:inline-block;margin-bottom:24px">А</div><h1>Кабинет сотрудников</h1><p>' + esc(text) + '</p>' + (user ? '<p><strong>' + esc(user.name) + '</strong></p>' : action('demo-login', 'Открыть профиль заявителя', '', 'primary')) + '<p class="muted">Переключение профилей сверху — инструмент демонстрации, не авторизация.</p></main>';
}
function ticketLink(t) { return '<div class="row"><div><strong>' + esc(t.name) + '</strong><p>' + esc(t.topic) + '</p><small>№ ' + t.id + ' · ' + esc(t.tag) + '</small></div>' + action('open-ticket', 'Открыть →', 'data-id="' + t.id + '"') + '</div>'; }
function homeView() {
  const open = state.tickets.filter(t => t.status !== 'closed');
  const mine = open.filter(t => t.owner === actor);
  const queue = open.filter(t => !t.owner);
  const important = open.filter(t => t.important);
  const attention = open.filter(t => !t.owner || t.owner === actor)
    .sort((a, b) => Number(b.important) - Number(a.important));
  const docs = state.docs.filter(d => d.active && d.published);
  const focusCount = mine.length || queue.length;
  const focusTitle = mine.length ? 'Ждут вашего ответа' : queue.length ? 'Можно помочь с очередью' : 'Очередь под контролем';
  const focusText = mine.length ? 'Продолжите диалог со студентом или верните вопрос в общую очередь, если нужна помощь.' : queue.length ? 'В клубе есть обращения без назначенного менеджера. Откройте вопрос и возьмите его в работу.' : 'Сейчас нет ваших незавершённых обращений и вопросов без назначенного менеджера.';
  const focusFilter = mine.length ? 'mine' : 'queue';
  const focusAction = mine.length ? 'Открыть мои обращения' : 'Открыть общую очередь';
  const summary = [[queue.length, 'Без менеджера', 'queue'], [important.length, 'Требуют внимания', 'important']]
    .map(([n, title, f]) => '<a class="home-metric" href="#inbox" data-filter="' + f + '"><span>' + title + '</span><strong>' + n + '</strong><span class="metric-arrow" aria-hidden="true">↗</span></a>').join('');
  const rows = attention.slice(0, 4).map(t => '<div class="home-task"><div class="task-symbol" aria-hidden="true">' + (t.important ? '!' : '↗') + '</div><div class="task-copy"><strong>' + esc(t.name) + '</strong><p>' + esc(t.topic) + '</p><small>№ ' + t.id + ' · ' + esc(t.tag) + '</small></div><div class="task-action">' + label(t.owner === actor ? 'Назначено вам' : 'Свободно', t.owner === actor ? 'green' : 'amber') + action('open-ticket', 'Открыть →', 'data-id="' + t.id + '"') + '</div></div>').join('');
  const materials = docs.slice(0, 3).map(d => '<a class="home-source" href="#material/' + encodeURIComponent(d.id) + '"><span class="source-symbol" aria-hidden="true">' + icon('M5 3h10l4 4v14H5V3Zm9 0v5h5M8 12h8M8 16h6') + '</span><span><strong>' + esc(d.published.title) + '</strong><small>Опубликован · версия ' + d.published.version + '</small></span><span aria-hidden="true">→</span></a>').join('');
  return heading('Главная', esc(person().name) + ' · личная работа и общая очередь')
    + '<div class="home-overview"><section class="home-focus"><div><span class="home-eyebrow">Поддержка клуба</span><h2>' + focusTitle + '</h2><p>' + focusText + '</p><a class="home-primary" href="#inbox" data-filter="' + focusFilter + '">' + focusAction + '<span aria-hidden="true">→</span></a></div><strong class="focus-count" aria-label="Обращений: ' + focusCount + '">' + focusCount + '</strong></section><section class="home-summary" aria-label="Общая ситуация">' + summary + '<p>Всего открыто: <strong>' + open.length + '</strong>. Категории могут пересекаться.</p></section></div>'
    + '<div class="home-work"><section class="panel home-tasks"><div class="home-section-head"><div><h2>Требует действий</h2><p class="muted">Ваши обращения и свободные вопросы клуба</p></div><span class="badge">' + attention.length + '</span></div>' + (rows || empty('Всё обработано. Новые обращения появятся здесь.')) + (attention.length > 4 ? '<a href="#inbox" data-filter="' + focusFilter + '">Открыть очередь →</a>' : '') + '</section><section class="panel home-materials"><div class="home-section-head"><div><h2>Материалы для ответов</h2><p class="muted">Опубликовано: ' + docs.length + '</p></div></div>' + (materials || empty('Опубликованных материалов пока нет.')) + '<a class="home-library-link" href="#knowledge">Вся база знаний <span aria-hidden="true">→</span></a></section></div>';
}
function inboxView() {
  const items = state.tickets.filter(t => filter === 'closed' ? t.status === 'closed' : t.status !== 'closed' && (filter === 'mine' ? t.owner === actor : filter === 'important' ? t.important : !t.owner)).filter(t => (t.name + t.topic).toLowerCase().includes(search.toLowerCase()));
  const ticket = state.tickets.find(t => t.id === selected);
  const allowed = canReply(person(), ticket);
  const filters = [['queue', 'Общая очередь'], ['mine', 'Мои'], ['important', 'Важные'], ['closed', 'Закрытые']].map(([id, title]) => action('filter', title, 'data-filter="' + id + '"', filter === id ? 'selected' : '')).join('');
  const queue = '<section class="panel queue"><form id="queue-search" class="form"><label>Поиск обращений<input name="query" value="' + esc(search) + '" placeholder="Имя или тема"></label><button>Найти</button></form><div style="height:16px"></div>' + (items.map(t => '<button class="ticket ' + (t.id === selected ? 'active' : '') + '" data-action="select-ticket" data-id="' + t.id + '"><strong>' + esc(t.name) + '</strong><p>' + esc(t.topic) + '</p>' + label(t.important ? 'Требует внимания' : 'Обычное', t.important ? 'amber' : '') + '<small>№ ' + t.id + (t.owner ? ' · ' + esc(state.people.find(p => p.id === t.owner)?.name) : ' · Не назначено') + '</small></button>').join('') || empty('В этой очереди обращений нет.')) + '</section>';
  let chat = '<section class="panel">' + empty('Выберите обращение, чтобы прочитать переписку.') + '</section>';
  let context = '';
  if (ticket) {
    const owner = state.people.find(p => p.id === ticket.owner);
    chat = '<section class="panel chat"><div class="chat-head">' + action('back-queue', '← К очереди', '', 'mobile-back') + '<h2>' + esc(ticket.name) + '</h2><p>' + esc(ticket.topic) + ' · № ' + ticket.id + '</p>' + label(ticket.status === 'closed' ? 'Закрыто' : owner ? 'В работе: ' + owner.name : 'Ожидает менеджера', ticket.status === 'closed' ? '' : 'amber') + '<div class="toolbar">' + (ticket.status !== 'closed' ? (!owner ? action('take', 'Взять в работу', '', 'primary') : allowed ? action('return', 'Вернуть в очередь') + action('close', 'Закрыть обращение') : '<small>Отвечает назначенный менеджер</small>') : '') + '</div></div><div class="messages">' + ticket.messages.map(m => '<div class="message ' + (m.by === 'manager' ? 'manager' : '') + '"><small>' + ({ student: 'Студент', bot: 'Бот клуба', manager: 'Менеджер · демонстрация' }[m.by]) + '</small>' + esc(m.text) + '</div>').join('') + '</div><form id="reply" class="composer"><label for="reply-text">Ответ студенту</label><textarea id="reply-text" name="text" ' + (!allowed ? 'disabled' : '') + ' placeholder="' + (allowed ? 'Напишите ответ…' : 'Возьмите обращение в работу') + '">' + esc(drafts.get(ticket.id) || '') + '</textarea><div class="toolbar"><small>Демо: сохранение сообщения в браузере</small><button class="primary" ' + (!allowed ? 'disabled' : '') + '>Сохранить демоответ</button></div></form></section>';
    const hits = searchSources(state.docs, sourceQuery);
    context = '<section class="panel context"><details open><summary>Сведения и материалы</summary><h3>Причина передачи</h3><p>' + esc(ticket.reason) + '</p><p class="muted">AI после передачи не отвечает. Реальная модель в демо отсутствует.</p><h3>Профиль</h3><p class="muted">Дополнительных подтверждённых сведений нет.</p><form id="source-search" class="form"><label>Поиск по словам · демо<input name="query" value="' + esc(sourceQuery) + '" placeholder="Например: занятие"></label><button>Найти в материалах</button></form>' + (sourceQuery ? hits.map(d => '<div class="source"><strong>' + esc(d.title) + '</strong><small> · версия ' + d.version + '</small><p>' + esc(d.text) + '</p><div class="toolbar">' + action('insert', 'Вставить в ответ', 'data-id="' + esc(d.id) + '" ' + (!allowed ? 'disabled' : '')) + '<a href="#material/' + encodeURIComponent(d.id) + '">Источник</a></div></div>').join('') || empty('Подходящих материалов не найдено.') : '<p class="muted">Поиск использует опубликованный текст, без нейросети.</p>') + '</details></section>';
  }
  return heading('Обращения', 'Общая очередь · ответы из кабинета') + '<div class="toolbar">' + filters + '</div><div class="inbox ' + (ticket ? 'has-ticket' : '') + '">' + queue + chat + context + '</div>';
}
function knowledgeView() {
  return heading('База знаний', 'Материалы для ответов клуба', '<a href="#material/new">Добавить материал →</a>') + '<section class="panel">' + (state.docs.length ? '<table class="list-table"><thead><tr><th>Материал</th><th>Публикация</th><th>Подготовка</th><th></th></tr></thead><tbody>' + state.docs.map(d => '<tr><td><strong>' + esc(d.draft?.title || d.published?.title) + '</strong><small>' + (d.published ? 'Версия ' + d.published.version : 'Новый материал') + '</small></td><td data-label="Публикация">' + label(d.active ? 'Опубликован' : 'Не опубликован', d.active ? 'green' : '') + '</td><td data-label="Подготовка">' + (d.draft ? 'Есть черновик' : 'Текст готов · демо') + '</td><td><a href="#material/' + encodeURIComponent(d.id) + '">Открыть →</a></td></tr>').join('') + '</tbody></table>' : empty('Материалов пока нет. Добавьте первый тестовый документ.')) + '</section>';
}
function materialView() {
  const id = decodeURIComponent(location.hash.split('/')[1] || 'new');
  const doc = state.docs.find(d => d.id === id);
  if (!doc && id !== 'new') return heading('Материал не найден', 'Вернитесь к списку материалов.');
  const content = doc?.draft || doc?.published;
  return heading(doc ? 'Материал' : 'Новый материал', 'Черновик и публикация — разные действия', '<a href="#knowledge">← К базе знаний</a>') + '<div class="grid"><section class="panel"><form id="material" data-id="' + esc(id) + '" class="form"><label>Название<input name="title" required maxlength="180" value="' + esc(content?.title || '') + '"></label><label>Текст<textarea name="text" required style="min-height:240px">' + esc(content?.text || '') + '</textarea></label><label>Или тестовый файл .txt<input type="file" id="text-file" accept=".txt,text/plain"><small>До 200 КБ. PDF и DOCX ещё не поддерживаются. Не загружайте реальные личные данные.</small></label><button class="primary">Сохранить черновик</button></form></section><section class="panel"><h2>Публикация</h2>' + label(doc?.active ? 'Опубликован' : 'Не опубликован', doc?.active ? 'green' : '') + '<p>Опубликованный текст может использоваться в ответах студентам. В этой сборке он доступен только локальному поиску.</p>' + (doc?.draft ? '<p class="notice">Есть сохранённый черновик. Старая опубликованная версия действует до публикации новой.</p>' : '') + '<div class="toolbar">' + (doc ? action('publish', doc.draft ? 'Опубликовать черновик' : 'Опубликовать снова', 'data-id="' + esc(id) + '" ' + (!doc.draft && doc.active ? 'disabled' : ''), 'primary') + (doc.active ? action('unpublish', 'Снять с публикации', 'data-id="' + esc(id) + '"') : '') : '<p class="muted">Сначала сохраните черновик.</p>') + '</div>' + (doc?.published ? '<h3>Опубликованная версия ' + doc.published.version + '</h3><div class="document">' + esc(doc.published.text) + '</div>' : '') + '</section></div>';
}
function staffView() {
  if (person().role !== 'admin') return heading('Доступ закрыт', 'Управлять сотрудниками могут только уполномоченные пользователи.');
  return heading('Сотрудники', 'Заявки и действующий доступ · демонстрационные профили') + '<section class="panel"><table class="list-table"><thead><tr><th>Сотрудник</th><th>Состояние</th><th>Действия</th></tr></thead><tbody>' + state.people.map(p => '<tr><td><strong>' + esc(p.name) + '</strong><small>' + (p.role ? roleName(p.role) : 'Роль не назначена') + '</small></td><td data-label="Состояние">' + label({ active: 'Доступ открыт', pending: 'Ожидает решения', rejected: 'Отказ', blocked: 'Заблокирован' }[p.status], p.status === 'active' ? 'green' : 'amber') + '</td><td><div class="toolbar">' + (p.status === 'pending' ? action('approve', 'Одобрить как менеджера', 'data-id="' + p.id + '"', 'primary') + action('reject', 'Отклонить', 'data-id="' + p.id + '"') : p.status === 'active' && p.role !== 'admin' ? action('block', 'Закрыть доступ', 'data-id="' + p.id + '"', 'danger') : '<small>Изменение недоступно в демо</small>') + '</div></td></tr>').join('') + '</tbody></table><p class="muted">Демо не подтверждает личность. Настоящие роли и проверка полномочий должны выполняться сервером.</p></section>';
}
async function confirmAction(text) {
  const dialog = document.createElement('dialog');
  dialog.innerHTML = '<h2>Подтверждение</h2><p>' + esc(text) + '</p><form method="dialog" class="toolbar"><button value="cancel">Отмена</button><button class="primary" value="ok">Подтвердить</button></form>';
  document.body.append(dialog); dialog.showModal();
  return new Promise(resolve => dialog.addEventListener('close', () => { const ok = dialog.returnValue === 'ok'; dialog.remove(); resolve(ok); }, { once: true }));
}
app.addEventListener('input', e => { if (e.target.id === 'reply-text' && selected) drafts.set(selected, e.target.value); });
app.addEventListener('change', async e => {
  if (e.target.id === 'actor') { actor = e.target.value; selected = null; render(); }
  if (e.target.id === 'text-file') {
    const file = e.target.files[0];
    if (!file) return;
    if (!file.name.toLowerCase().endsWith('.txt') || file.size > 200 * 1024) { notify('Нужен текстовый файл .txt размером до 200 КБ.'); e.target.value = ''; return; }
    try { const text = await file.text(); const form = document.querySelector('#material'); if (form) { form.elements.text.value = text; if (!form.elements.title.value) form.elements.title.value = file.name.replace(/\.txt$/i, ''); } }
    catch { notify('Не удалось прочитать файл. Попробуйте другой .txt.'); }
  }
});
app.addEventListener('click', async e => {
  const button = e.target.closest('[data-action]');
  const filterLink = e.target.closest('a[data-filter]');
  if (filterLink) { filter = filterLink.dataset.filter; selected = null; if (location.hash === '#inbox') render(); }
  if (!button || button.disabled) return;
  const name = button.dataset.action;
  if (name === 'reset') { if (await confirmAction('Сбросить только локальные демонстрационные данные?')) { try { state = reset(); actor = 'manager'; drafts.clear(); selected = null; location.hash = '#home'; render(); } catch { notify('Хранилище браузера недоступно.'); } } return; }
  if (name === 'logout') { actor = ''; render(); return; }
  if (name === 'demo-login') { actor = 'applicant'; render(); return; }
  if (!canWork(person())) return;
  if (name === 'filter') { filter = button.dataset.filter; selected = null; render(); return; }
  if (name === 'select-ticket' || name === 'open-ticket') { selected = button.dataset.id; location.hash = '#inbox'; render(); return; }
  if (name === 'back-queue') { selected = null; render(); return; }
  const ticket = state.tickets.find(t => t.id === selected);
  if (name === 'take' && ticket && !ticket.owner && ticket.status !== 'closed') commit(() => { ticket.owner = actor; });
  if (name === 'return' && canReply(person(), ticket)) commit(() => { ticket.owner = null; ticket.important = true; });
  if (name === 'close' && canReply(person(), ticket)) { if (!await confirmAction('Закрыть обращение № ' + ticket.id + '? Переписка сохранится.')) return; commit(() => { ticket.status = 'closed'; }); }
  if (name === 'insert' && canReply(person(), ticket)) {
    const doc = state.docs.find(d => d.id === button.dataset.id && d.active);
    if (doc) drafts.set(ticket.id, [drafts.get(ticket.id), doc.published.text].filter(Boolean).join('\n\n'));
  }
  if (['approve', 'reject', 'block'].includes(name) && person().role === 'admin') {
    const target = state.people.find(p => p.id === button.dataset.id);
    if (target && target.role !== 'admin' && (name === 'block' ? target.status === 'active' : target.status === 'pending')) {
      if (!await confirmAction((name === 'approve' ? 'Выдать менеджерский доступ: ' : name === 'reject' ? 'Отклонить заявку: ' : 'Закрыть доступ: ') + target.name + '?')) return;
      commit(() => { target.status = name === 'approve' ? 'active' : name === 'reject' ? 'rejected' : 'blocked'; if (name === 'approve') target.role = 'manager'; });
    }
  }
  const doc = state.docs.find(d => d.id === button.dataset.id);
  if (name === 'publish' && doc && (doc.draft || doc.published)) {
    if (!await confirmAction('Опубликовать сохранённый текст? Несохранённые изменения формы в него не входят.')) return;
    commit(() => { if (doc.draft) { doc.published = { ...doc.draft, version: (doc.published?.version || 0) + 1 }; doc.draft = null; } doc.active = true; });
  }
  if (name === 'unpublish' && doc?.active) {
    if (!await confirmAction('Убрать материал из новых результатов поиска? Сохранённая версия останется.')) return;
    commit(() => { doc.active = false; });
  }
  render();
});
app.addEventListener('submit', e => {
  e.preventDefault(); if (!canWork(person())) return;
  const form = e.target; const data = new FormData(form);
  if (form.id === 'queue-search') { search = String(data.get('query')); render(); }
  if (form.id === 'source-search') { sourceQuery = String(data.get('query')); render(); }
  if (form.id === 'reply') {
    const ticket = state.tickets.find(t => t.id === selected); const text = String(data.get('text') || '').trim();
    if (!text || !canReply(person(), ticket)) { notify('Нужен непустой ответ и назначенное вам обращение.'); return; }
    if (commit(() => { ticket.messages.push({ by: 'manager', text }); })) { drafts.delete(ticket.id); notify('Демоответ сохранён локально. В ВК ничего не отправлялось.'); }
    render();
  }
  if (form.id === 'material') {
    const title = String(data.get('title') || '').trim(); const text = String(data.get('text') || '').trim();
    if (!title || !text || title.length > 180 || text.length > 200000) { notify('Укажите название до 180 символов и непустой текст до 200 000 символов.'); return; }
    let id = form.dataset.id;
    if (commit(() => { const doc = state.docs.find(d => d.id === id); if (doc) doc.draft = { title, text }; else { id = crypto.randomUUID(); state.docs.push({ id, draft: { title, text }, published: null, active: false }); } })) { location.hash = '#material/' + id; notify('Черновик сохранён. Для поиска его нужно опубликовать.'); render(); }
  }
});
window.addEventListener('hashchange', render);
window.addEventListener('beforeunload', e => { if ([...drafts.values()].some(Boolean)) { e.preventDefault(); e.returnValue = ''; } });
render();

document.querySelector('.skip').addEventListener('click', e => { e.preventDefault(); document.querySelector('#main')?.focus(); });
