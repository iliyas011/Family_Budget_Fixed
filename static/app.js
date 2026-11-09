'use strict';
const $ = (id) => document.getElementById(id);
const form = $('entry-form');
// Stable stored category values preserve existing database records.
const categoryNames = {'Жалақы':'Salary', 'Қосымша табыс':'Side income', 'Басқа кіріс':'Other income',
  'Азық-түлік':'Groceries', 'Көлік':'Transport', 'Коммуналдық':'Utilities', 'Демалыс':'Leisure', 'Басқа шығыс':'Other expenses'};
const categoryMeta = {'Жалақы':['bag','c-salary'], 'Қосымша табыс':['laptop','c-extra'], 'Басқа кіріс':['coins','c-other'],
  'Азық-түлік':['cart','c-food'], 'Көлік':['car','c-transport'], 'Коммуналдық':['home','c-bills'], 'Демалыс':['fun','c-fun'], 'Басқа шығыс':['dots','c-other']};
const categoryName = (value) => categoryNames[value] || value;
function icon(name) {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  const use = document.createElementNS('http://www.w3.org/2000/svg', 'use');
  svg.setAttribute('class', 'i'); svg.setAttribute('aria-hidden', 'true');
  use.setAttribute('href', '#i-' + name); svg.append(use); return svg;
}
function catBadge(value) {
  const [name, cls] = categoryMeta[value] || ['dots', 'c-other'];
  const badge = document.createElement('span'); badge.className = 'cat ' + cls; badge.append(icon(name)); return badge;
}
let categories = null;
let editingId = null;
let appliedFilters = '';
let refreshVersion = 0;
const money = (minor) => new Intl.NumberFormat('en-US', {maximumFractionDigits: 2, minimumFractionDigits: minor % 100 ? 2 : 0}).format(minor / 100) + ' ₸';
const today = new Date();
$('date').value = `${today.getFullYear()}-${String(today.getMonth() + 1).padStart(2, '0')}-${String(today.getDate()).padStart(2, '0')}`;
async function request(path, options) {
  const response = await fetch(path, options);
  if (response.status === 401) { window.location.replace('/login'); throw new Error('Please sign in again.'); }
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || 'Request failed.');
  return data;
}
function setCategories() {
  if (!categories) return;
  $('category').replaceChildren(...categories[form.elements.kind.value].map((name) => new Option(categoryName(name), name)));
}
form.addEventListener('change', (event) => { if (event.target.name === 'kind') setCategories(); });
function element(tag, text, className) {
  const node = document.createElement(tag);
  node.textContent = text;
  if (className) node.className = className;
  return node;
}

// Функция для удаления транзакции
async function deleteTransaction(id) {
  try {
    await request('/api/transactions/delete', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ id: id })
    });
    if (editingId === id) cancelEdit();
    await refresh();
  } catch (error) {
    alert(error.message || 'Could not delete transaction.');
  }
}

async function refresh() {
  const version = ++refreshVersion;
  const data = await request('/api/budget' + (appliedFilters ? '?' + appliedFilters : ''));
  if (version !== refreshVersion) return;
  $('income').textContent = money(data.summary.income_minor);
  $('expense').textContent = money(data.summary.expense_minor);
  $('balance').textContent = money(data.summary.balance_minor);
  $('count').textContent = `${data.summary.count} records`;
  $('records').replaceChildren(...data.transactions.map((row) => {
    const li = document.createElement('li'); li.className = 'tx';
    const text = document.createElement('div');
    text.append(element('span', categoryName(row.category), 'row-title'), element('small', row.note || 'No note'));
    const side = document.createElement('div'); side.className = 'side';
    side.append(element('span', `${row.kind === 'income' ? '+' : '−'}${money(row.amount_minor)}`, `amount ${row.kind}`),
      element('span', row.date.split('-').reverse().join('.'), 'date-cell'));
    const del = document.createElement('button');
    del.type = 'button'; del.className = 'delete-btn'; del.title = 'Delete transaction'; del.setAttribute('aria-label', 'Delete transaction');
    del.append(icon('trash')); del.addEventListener('click', () => deleteTransaction(row.id));
    const edit = element('button', 'Edit', 'edit-btn');
    edit.type = 'button'; edit.setAttribute('aria-label', 'Edit transaction');
    edit.addEventListener('click', () => editTransaction(row));
    const actions = element('div', '', 'tx-actions'); actions.append(edit, del);
    li.append(catBadge(row.category), text, side, actions);
    return li;
  }));
  $('empty').hidden = data.transactions.length !== 0;
  const chart = $('category-chart');
  chart.replaceChildren();
  const entries = Object.entries(data.expenses_by_category);
  if (!entries.length) chart.append(element('p', 'Add an expense to see your spending breakdown.', 'muted'));
  for (const [name, amount] of entries) {
    const percent = Math.round(amount / data.summary.expense_minor * 100);
    const item = element('div', '', 'chart-item');
    const body = document.createElement('div');
    const line = element('div', '', 'chart-label');
    line.append(element('span', categoryName(name)), element('span', `${money(amount)} · ${percent}%`));
    const track = document.createElement('div'); track.className = 'track ' + (categoryMeta[name] || [0, 'c-other'])[1];
    track.setAttribute('role', 'img'); track.setAttribute('aria-label', `${categoryName(name)}: ${percent}%`);
    const fill = document.createElement('i'); fill.style.width = percent + '%'; track.append(fill);
    body.append(line, track); item.append(catBadge(name), body); chart.append(item);
  }
  renderDonut(entries, data.summary.expense_minor);
  $('filter-status').textContent = appliedFilters ? `${data.summary.count} matching records · Filters applied` : 'All transactions';
  $('load-error').hidden = true;
}
form.addEventListener('submit', async (event) => {
  event.preventDefault();
  if ($('save').disabled) return;
  $('save').disabled = true;
  const message = $('form-message');
  message.textContent = 'Saving…'; message.className = '';
  const payload = Object.fromEntries(new FormData(form).entries());
  if (editingId !== null) payload.id = editingId;
  try {
    await request(editingId === null ? '/api/transactions' : '/api/transactions/update', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)});
  } catch (error) {
    message.textContent = error instanceof TypeError ? 'Connection lost. Refresh and check for the record before trying again.' : error.message;
    message.className = 'error'; $('save').disabled = false; return;
  }
  cancelEdit();
  $('amount').value = ''; $('note').value = '';
  message.textContent = 'Transaction saved'; message.className = 'success';
  try { await refresh(); }
  catch (_) { $('load-error').textContent = 'Transaction saved, but the list could not refresh. Reload the page.'; $('load-error').hidden = false; }
  $('save').disabled = false; $('amount').focus();
});
async function initialize() {
  try {
    const session = await request('/api/session');
    $('account-name').textContent = session.username;
    $('avatar').textContent = (session.username || '?').trim().charAt(0).toUpperCase();
    await telegramStatus();
    categories = await request('/api/categories'); setCategories();
    $('filter-category').append(...Object.values(categories).flat().map(name => new Option(categoryName(name), name))); await refresh(); $('save').disabled = false;
  } catch (_) {
    $('load-error').textContent = 'Cannot connect to the server. Start it and reload this page.';
    $('load-error').hidden = false;
  }
}
$('logout').addEventListener('click', async () => {
  $('logout').disabled = true;
  try {
    await request('/api/logout', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}'});
    window.location.replace('/login');
  } catch (error) {
    $('load-error').textContent = 'Could not sign out. Check your connection and try again.';
    $('load-error').hidden = false; $('logout').disabled = false;
  }
});
// Recheck the session when a browser restores a page from its back/forward cache.
window.addEventListener('pageshow', (event) => { if (event.persisted) window.location.reload(); });
initialize();

async function telegramStatus() {
  const state = await request('/api/telegram');
  $('telegram-status').textContent = !state.enabled ? 'The bot has not been set up by the site owner yet.' : state.linked ? 'Telegram is linked. Password recovery is available.' : 'Link Telegram in advance to receive recovery codes.';
  $('telegram-form').hidden = !state.enabled;
}
$('telegram-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  $('link-telegram').disabled = true;
  try {
    const data = await request('/api/telegram/link', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({password: $('link-password').value})});
    $('link-password').value = '';
    $('telegram-link').href = data.url;
    $('telegram-link').hidden = false;
    $('telegram-status').textContent = 'Open the link and press Start within 5 minutes. Do not share the link.';
  } catch (error) { $('telegram-status').textContent = error.message; }
  finally { $('link-telegram').disabled = false; }
});
window.addEventListener('focus', () => { telegramStatus().catch(() => {}); });

function renderDonut(entries, total) {
  const box = $('donut'); box.replaceChildren();
  if (!entries.length) return;
  const NS = 'http://www.w3.org/2000/svg', R = 52, C = 2 * Math.PI * R;
  const mk = (tag, attrs) => { const n = document.createElementNS(NS, tag); for (const k in attrs) n.setAttribute(k, attrs[k]); return n; };
  const svg = mk('svg', {viewBox: '0 0 140 140', class: 'donut-svg', role: 'img', 'aria-label': 'Spending breakdown by category'});
  svg.append(mk('circle', {cx: 70, cy: 70, r: R, class: 'donut-bg'}));
  let offset = 0;
  for (const [name, amount] of entries) {
    const len = amount / total * C;
    svg.append(mk('circle', {cx: 70, cy: 70, r: R, class: 'donut-seg ' + (categoryMeta[name] || [0, 'c-other'])[1],
      'stroke-dasharray': `${Math.max(len - 2, 0.1)} ${C}`, 'stroke-dashoffset': -offset, transform: 'rotate(-90 70 70)'}));
    offset += len;
  }
  const label = mk('text', {x: 70, y: 66, 'text-anchor': 'middle', class: 'donut-label'}); label.textContent = 'Spent';
  const sum = mk('text', {x: 70, y: 84, 'text-anchor': 'middle', class: 'donut-total'}); sum.textContent = money(total);
  svg.append(label, sum); box.append(svg);
}

function cancelEdit() {
  editingId = null;
  $('entry-title').textContent = 'New transaction';
  $('save').textContent = 'Save transaction';
  $('cancel-edit').hidden = true;
  form.reset(); setCategories();
  $('date').value = `${today.getFullYear()}-${String(today.getMonth() + 1).padStart(2, '0')}-${String(today.getDate()).padStart(2, '0')}`;
}
function editTransaction(row) {
  editingId = row.id;
  form.elements.kind.value = row.kind; setCategories();
  $('category').value = row.category;
  $('amount').value = (row.amount_minor / 100).toFixed(2);
  $('date').value = row.date; $('note').value = row.note;
  $('entry-title').textContent = 'Edit transaction';
  $('save').textContent = 'Save changes';
  $('cancel-edit').hidden = false;
  $('form-message').textContent = '';
  form.scrollIntoView({behavior: 'smooth', block: 'center'}); $('amount').focus();
}
$('cancel-edit').addEventListener('click', cancelEdit);
$('report-filters').addEventListener('submit', async event => {
  event.preventDefault();
  const params = new URLSearchParams();
  for (const [key, value] of new FormData(event.currentTarget)) if (value) params.set(key, value);
  const previous = appliedFilters; appliedFilters = params.toString();
  try { await refresh(); }
  catch (error) { appliedFilters = previous; $('filter-status').textContent = error.message; }
});
$('report-filters').addEventListener('reset', async () => {
  appliedFilters = '';
  try { await refresh(); }
  catch (error) { $('filter-status').textContent = error.message; }
});
