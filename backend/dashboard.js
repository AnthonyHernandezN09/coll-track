const updatesList = document.querySelector('#updates');
const trackersList = document.querySelector('#trackers');
const schools = document.querySelector('#schools');
const message = document.querySelector('#message');
const trackerMessage = document.querySelector('#tracker-message');
const filters = [...document.querySelectorAll('[data-filter]')];
const settingsDialog = document.querySelector('#settings-dialog');
const defaultsForm = document.querySelector('#tracker-defaults');
let trackerRecords = [];
let trackerProfiles = [];
const trackerStatus = {
  checked: 'Portal checked', sign_in: 'Set up a saved portal login.',
  permission: 'Set up a saved portal login.',
  missing_login: 'Save a login for this portal.',
  choose_login: 'Choose a saved login for this portal.',
  login_failed: 'Review the saved login. The portal did not complete sign-in.',
  verification: 'The portal requires verification or uses an unsupported sign-in.',
  unreadable: 'Checklist could not be read', error: 'Check failed. Will retry.'
};
let updates = [];
let filter = 'unread';

function add(parent, tag, text = '') {
  const element = document.createElement(tag);
  element.textContent = text;
  parent.append(element);
  return element;
}

async function request(path, body) {
  const response = await fetch(path, body === undefined ? undefined : {
    method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body)
  });
  if (!response.ok) {
    const details = await response.json().catch(() => null);
    throw new Error(details?.error || 'Could not save the change. Try again.');
  }
  return response.json();
}

const time = stamp => new Date(stamp).toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' });
function itemStatus(item) {
  return item.status === 'unknown' ? 'Status unknown' : (item.rawStatus || item.status).replace(/^status\s*:\s*/i, '');
}
function itemDetails(item) {
  return [itemStatus(item), item.dateText].filter(Boolean).join(' · ');
}

function describeChange({ before, after }) {
  if (!after) return 'Removed · ' + itemDetails(before);
  if (!before) return 'Added · ' + itemDetails(after);
  const oldDetails = itemDetails(before);
  const newDetails = itemDetails(after);
  return oldDetails === newDetails ? 'Item details changed · ' + newDetails : oldDetails + ' → ' + newDetails;
}

function renderUpdates() {
  const visible = updates.filter(update => filter === 'all' || update.read === (filter === 'read'));
  for (const button of filters) {
    button.setAttribute('aria-pressed', String(button.dataset.filter === filter));
    if (button.dataset.filter === 'unread') button.textContent = `Unread (${updates.filter(item => !item.read).length})`;
  }
  updatesList.replaceChildren();
  message.textContent = visible.length ? '' : {
    unread: 'No unread changes. Portal changes will appear here.',
    read: 'No updates marked as read.', all: 'No portal changes recorded yet.'
  }[filter];
  for (const update of visible) {
    const row = add(updatesList, 'li');
    add(row, 'h3', update.item);
    add(row, 'small', `${update.school} · ${time(update.at)}`);
    if (Array.isArray(update.changes)) {
      const details = add(row, 'details');
      add(details, 'summary', 'View changes');
      if (update.changes.length) {
        add(details, 'h3', 'Checklist');
        const list = add(details, 'ul');
        for (const change of update.changes) add(list, 'li', change.item + ' · ' + describeChange(change));
      }
      if (update.page) {
        for (const [key, title] of [['added', 'Added to the page'], ['removed', 'Removed from the page']]) {
          if (!update.page[key].length) continue;
          add(details, 'h3', title);
          const list = add(details, 'ul');
          for (const line of update.page[key]) add(list, 'li', line);
        }
        if (!update.page.added.length && !update.page.removed.length) add(details, 'p', 'Page content was reordered.');
      }
    } else {
      add(row, 'p', describeChange(update));
    }
    const button = add(row, 'button', update.read ? 'Mark unread' : 'Mark read');
    button.type = 'button';
    button.addEventListener('click', async () => {
      button.disabled = true;
      try {
        await request('/updates/read', { id: update.id, read: !update.read });
        update.read = !update.read;
        renderUpdates();
        renderCardActivity();
        filters.find(button => button.dataset.filter === filter).focus();
      } catch (error) {
        message.textContent = error.message;
        button.disabled = false;
      }
    });
  }
}

for (const button of filters) {
  button.addEventListener('click', () => {
    filter = button.dataset.filter;
    renderUpdates();
  });
}

function trackerSummary(tracker) {
  if (!tracker.enabled) return 'Paused';
  if (tracker.checking_until > Date.now()) return 'Checking portal…';
  const summary = trackerStatus[tracker.result] ?? 'Waiting for first check';
  return tracker.checked ? `${summary} · ${time(tracker.checked)}` : summary;
}

function renderTrackers(records) {
  trackersList.replaceChildren();
  trackerMessage.textContent = records.length ? '' :
    'Choose Enable checks on a university card to start tracking.';
  for (const tracker of records) {
    const row = add(trackersList, 'li');
    add(row, 'h3', tracker.school);
    const status = add(row, 'small', trackerSummary(tracker));
    status.dataset.trackerSource = tracker.source;
    const controls = add(row, 'div');
    controls.className = 'track-controls';
    const label = add(controls, 'label', 'Check every (minutes)');
    const select = add(label, 'input');
    select.type = 'number';
    select.min = '1';
    select.max = '10080';
    select.required = true;
    select.setAttribute('aria-label', 'Check interval for ' + tracker.school);
    select.value = tracker.interval;
    const host = new URL(tracker.url).hostname;
    const loginLabel = add(controls, 'label', 'Saved login');
    const login = add(loginLabel, 'select');
    login.setAttribute('aria-label', 'Saved login for ' + tracker.school);
    add(login, 'option', 'Use the only matching login').value = '';
    for (const profile of trackerProfiles.filter(profile => profile.hosts.includes(host))) {
      add(login, 'option', profile.name).value = profile.id;
    }
    login.value = tracker.profile_id ?? '';
    const saveInterval = add(controls, 'button', 'Save settings');
    saveInterval.type = 'button';
    const toggle = add(controls, 'button', tracker.enabled ? 'Pause' : 'Resume');
    toggle.type = 'button';

    async function save(enabled) {
      if (!select.reportValidity()) return;
      select.disabled = login.disabled = saveInterval.disabled = toggle.disabled = true;
      try {
        await request('/trackers/settings', { source: tracker.source, interval: Number(select.value), enabled, profileId: login.value || null });
        renderTrackers(await request('/trackers'));
        trackerMessage.textContent = 'Check settings saved.';
      } catch (error) {
        trackerMessage.textContent = error.message;
        select.value = tracker.interval;
        select.disabled = login.disabled = saveInterval.disabled = toggle.disabled = false;
      }
    }
    saveInterval.addEventListener('click', () => save(Boolean(tracker.enabled)));
    toggle.addEventListener('click', () => save(!tracker.enabled));
  }
}

document.querySelector('#settings-open').addEventListener('click', async () => {
  settingsDialog.showModal();
  defaultsForm.inert = true;
  try {
    const [defaults, records, profiles] = await Promise.all([request('/trackers/defaults'), request('/trackers'), request('/trackers/profiles')]);
    trackerProfiles = profiles;
    defaultsForm.elements.interval.value = defaults.interval;
    renderTrackers(records);
  } catch (error) {
    trackerMessage.textContent = error.message;
  } finally {
    defaultsForm.inert = false;
  }
});
document.querySelector('#settings-close').addEventListener('click', () => settingsDialog.close());
defaultsForm.addEventListener('submit', async event => {
  event.preventDefault();
  defaultsForm.inert = true;
  try {
    await request('/trackers/defaults', { interval: defaultsForm.elements.interval.valueAsNumber });
    renderTrackers(await request('/trackers'));
    trackerMessage.textContent = 'Check interval saved for all portals.';
  } catch (error) {
    trackerMessage.textContent = error.message;
  } finally {
    defaultsForm.inert = false;
  }
});

function renderCardActivity() {
  const checking = new Set(trackerRecords.filter(record => record.checking_until > Date.now())
    .map(record => new URL(record.source).hostname));
  const unread = new Set(updates.filter(update => !update.read).map(update => new URL(update.source).hostname));
  const tracked = new Set(trackerRecords.map(record => new URL(record.source).hostname));
  for (const card of schools.querySelectorAll('.school-card')) {
    const enable = card.querySelector('.enable-checks');
    if (enable) enable.hidden = tracked.has(card.dataset.host);
    const light = card.querySelector('.portal-light');
    const active = checking.has(card.dataset.host);
    const changed = unread.has(card.dataset.host);
    light.hidden = !active && !changed;
    light.classList.toggle('checking', active);
    light.classList.toggle('unread', changed);
    const text = active && changed ? 'Checking portal · unread updates' : active ? 'Checking portal' : 'Unread updates';
    light.title = text;
    if (light.textContent !== text) light.textContent = text;
  }
}

const arrange = document.querySelector('#arrange');
const arrangeMessage = document.querySelector('#arrange-message');
const orderKey = 'coll-track-university-order';
let universityRecords = [];
let arranging = false;
let draggedSchool;
let order = [];
let savingOrder = false;
let orderRevision = 0;

function legacyOrder() {
  try {
    const saved = JSON.parse(localStorage.getItem(orderKey));
    if (Array.isArray(saved)) return saved.filter(value => typeof value === 'string');
  } catch {}
  return [];
}

async function moveSchool(host, destination, focusAction) {
  const from = order.indexOf(host);
  if (savingOrder || from < 0 || destination < 0 || destination >= order.length || from === destination) return;
  const next = [...order];
  next.splice(from, 1);
  next.splice(destination, 0, host);

  savingOrder = true;
  orderRevision++;
  try {
    const saved = await request('/schools/order', { hosts: next });
    order = saved.hosts;
    lastData = undefined;
    renderSnapshots(universityRecords);
    const card = [...schools.children].find(element => element.dataset.host === host);
    const button = card?.querySelector(`[data-move="${focusAction}"]:not(:disabled)`);
    (button ?? card?.querySelector('button:not(:disabled)') ?? arrange).focus();
    arrangeMessage.textContent = `${card.querySelector('h2').textContent} moved to position ${order.indexOf(host) + 1}.`;
  } catch {
    arrangeMessage.textContent = 'Could not save the card order. Try moving the card again.';
  } finally {
    savingOrder = false;
  }
}

arrange.addEventListener('click', () => {
  arranging = !arranging;
  renderSnapshots(universityRecords);
  arrangeMessage.textContent = arranging ? 'Click a card to edit it, or drag it to move it.' : '';
});

schools.addEventListener('dragend', () => {
  draggedSchool = undefined;
  schools.querySelector('.drop-target')?.classList.remove('drop-target');
});

const brandingRequests = new Map();
function applyBranding(host, card, mark, initials) {
  mark.textContent = initials;
  function showLogo(src) {
    mark.replaceChildren();
    const icon = add(mark, 'img');
    icon.alt = '';
    icon.referrerPolicy = 'no-referrer';
    icon.addEventListener('error', () => { mark.textContent = initials; }, { once: true });
    icon.src = src;
  }
  if (!brandingRequests.has(host)) {
    brandingRequests.set(host, request('/branding?host=' + encodeURIComponent(host)).catch(() => ({})));
  }
  brandingRequests.get(host).then(brand => {
    if (!card.isConnected) return;
    if (/^#[0-9a-f]{6}$/i.test(brand.color ?? '')) card.style.setProperty('--school-color', brand.color);
    if (brand.logo) showLogo(brand.logo);
  });
}

function dropSide(card, event) {
  const box = card.getBoundingClientRect();
  const singleColumn = getComputedStyle(schools).gridTemplateColumns.split(' ').length === 1;
  const before = singleColumn ? event.clientY < box.top + box.height / 2 : event.clientX < box.left + box.width / 2;
  return before ? 'before' : 'after';
}

function renderSnapshots(records) {
  universityRecords = records;
  const open = new Set([...schools.querySelectorAll('details[open]')].map(element => element.dataset.source));
  const byHost = new Map();
  for (const record of records) {
    const url = new URL(record.source);
    if (!byHost.has(url.hostname)) byHost.set(url.hostname, record);
  }
  order = [...new Set([...order.filter(host => byHost.has(host)), ...byHost.keys()])];
  arrange.setAttribute('aria-label', arranging ? 'Finish editing cards' : 'Edit cards');
  arrange.title = arrange.getAttribute('aria-label');
  arrange.setAttribute('aria-pressed', String(arranging));
  schools.replaceChildren();
  if (!order.length) add(schools, 'p', 'Use + to add a university, or import a checklist from Chrome.');
  for (const [index, host] of order.entries()) {
    const record = byHost.get(host);
    const card = add(schools, 'article');
    card.className = 'school-card';
    card.dataset.host = host;
    card.draggable = arranging;
    if (arranging) {
      card.addEventListener('click', event => {
        if (!event.target.closest('button, a, summary, .drag-handle')) openSchool(record);
      });
    }
    const header = add(card, 'div');
    header.className = 'school-card-header';
    const mark = add(header, 'div');
    mark.className = 'school-mark';
    mark.setAttribute('aria-hidden', 'true');
    const initials = record.school.split(/\s+/).filter(Boolean).slice(0, 3).map(word => word[0]).join('').toUpperCase();
    applyBranding(host, card, mark, initials);
    add(header, 'h2', record.school);
    const light = add(header, 'span');
    light.className = 'portal-light';
    light.setAttribute('role', 'status');
    light.hidden = true;
    const body = add(card, 'div');
    body.className = 'school-body';
    const items = record.checklists.flatMap(checklist => checklist.items);
    const pending = items.filter(item => ['pending', 'missing'].includes(item.status)).length;
    add(body, 'p', !record.importedAt ? 'No checklist imported yet.' : `${items.length} checklist ${items.length === 1 ? 'item' : 'items'}` +
      (pending ? ` · ${pending} awaiting or missing` : ''));
    if (record.importedAt) add(body, 'small', 'Saved ' + time(record.importedAt));
    const section = add(body, 'details');
    section.hidden = !record.importedAt;
    section.dataset.source = record.source;
    section.open = open.has(record.source);
    add(section, 'summary', 'View checklist');
    const footer = add(card, 'div');
    footer.className = 'school-footer';
    const portal = new URL(record.portal ?? record.source);
    if (portal.protocol === 'https:' && !portal.username && !portal.password) {
      const launch = add(footer, 'a', 'Quick launch');
      launch.href = portal.href;
      launch.dataset.quickLaunch = record.source;
      launch.target = '_blank';
      launch.rel = 'noopener noreferrer';
      launch.className = 'launch';
      launch.setAttribute('aria-label', 'Quick launch ' + record.school);
      const enable = add(footer, 'button', 'Enable checks');
      enable.type = 'button';
      enable.className = 'enable-checks';
      enable.addEventListener('click', async () => {
        const statusUrl = new URL(portal);
        statusUrl.hash = '';
        if (statusUrl.pathname.startsWith('/account/')) {
          const next = new URL(statusUrl.searchParams.get('r') || '/apply/status', statusUrl);
          if (next.origin !== statusUrl.origin || next.pathname.startsWith('/account/')) {
            arrangeMessage.textContent = 'Edit this school and enter its application status page address.';
            return;
          }
          statusUrl.href = next.origin + next.pathname;
          for (const key of ['cmd', 'tab']) {
            const value = next.searchParams.get(key);
            if (value && /^[a-z0-9_-]{1,40}$/i.test(value)) statusUrl.searchParams.set(key, value);
          }
        }
        enable.disabled = true;
        try {
          await request('/trackers/register', {
            source: statusUrl.origin + statusUrl.pathname, url: statusUrl.href, school: record.school
          });
          arrangeMessage.textContent = 'Automatic checks enabled for ' + record.school + '.';
          await pollTrackers();
        } catch (error) {
          arrangeMessage.textContent = error.message;
        } finally {
          enable.disabled = false;
        }
      });
    }
    for (const checklist of record.checklists) {
      add(section, 'h3', checklist.title);
      const list = add(section, 'ul');
      for (const item of checklist.items) {
        const row = add(list, 'li', item.item);
        add(row, 'small', itemDetails(item));
      }
    }

    const handle = add(header, 'span', '⠿');
    handle.className = 'drag-handle';
    handle.draggable = true;
    handle.title = 'Drag to reorder';
    handle.setAttribute('aria-hidden', 'true');
    card.addEventListener('dragstart', event => {
      if (event.target !== handle && event.target !== card) {
        event.preventDefault();
        return;
      }
      draggedSchool = host;
      event.dataTransfer.setData('text/plain', host);
      event.dataTransfer.effectAllowed = 'move';
      const bounds = card.getBoundingClientRect();
      event.dataTransfer.setDragImage(card, event.clientX - bounds.left, event.clientY - bounds.top);
    });
    card.addEventListener('dragover', event => {
      if (!draggedSchool || draggedSchool === host) return;
      event.preventDefault();
      event.dataTransfer.dropEffect = 'move';
      schools.querySelector('.drop-target')?.classList.remove('drop-target');
      card.classList.add('drop-target');
      card.dataset.drop = dropSide(card, event);
    });
    card.addEventListener('drop', event => {
      if (!draggedSchool || draggedSchool === host) return;
      event.preventDefault();
      const from = order.indexOf(draggedSchool);
      const insertion = index + (dropSide(card, event) === 'after' ? 1 : 0);
      moveSchool(draggedSchool, insertion - (from < insertion ? 1 : 0));
      card.classList.remove('drop-target');
      draggedSchool = undefined;
    });
    if (arranging) {
      const moves = add(card, 'div');
      moves.className = 'card-moves';
      const edit = add(moves, 'button', 'Edit school');
      edit.type = 'button';
      edit.addEventListener('click', () => openSchool(record));
      for (const [delta, direction] of [[-1, 'earlier'], [1, 'later']]) {
        const button = add(moves, 'button', 'Move ' + direction);
        button.type = 'button';
        button.dataset.move = direction;
        button.setAttribute('aria-label', `Move ${record.school} ${direction}`);
        button.disabled = index + delta < 0 || index + delta >= order.length;
        button.addEventListener('click', () => moveSchool(host, index + delta, direction));
      }
    }
  }
  renderCardActivity();
}

let lastData;
let refreshing = false;
async function refresh() {
  if (refreshing) return;
  refreshing = true;
  const revision = orderRevision;
  try {
    const data = await Promise.all(['/updates', '/snapshots', '/schools/order'].map(path => request(path)));
    if (savingOrder || revision !== orderRevision) return;
    if (data[2].hosts === null && data[1].length) {
      data[2] = await request('/schools/order', { hosts: legacyOrder(), initialize: true });
      if (savingOrder || revision !== orderRevision) return;
      try { localStorage.removeItem(orderKey); } catch {}
    }
    const serialized = data.map(value => JSON.stringify(value));
    if (lastData?.[0] !== serialized[0]) {
      updates = data[0];
      renderUpdates();
    }
    if (lastData?.[1] !== serialized[1] || lastData?.[2] !== serialized[2]) {
      order = data[2].hosts ?? [];
      renderSnapshots(data[1]);
    }
    lastData = serialized;
    renderCardActivity();
  } catch {
    message.textContent = 'Could not load the dashboard. Refresh to try again.';
  } finally {
    refreshing = false;
  }
}
refresh();
let pollingTrackers = false;
async function pollTrackers() {
  if (document.hidden || pollingTrackers) return;
  pollingTrackers = true;
  try {
    const records = await request('/trackers');
    const completed = records.some(record => record.checked &&
      !trackerRecords.some(previous => previous.source === record.source && previous.checked === record.checked));
    trackerRecords = records;
    if (settingsDialog.open) {
      const statuses = new Map(records.map(record => [record.source, trackerSummary(record)]));
      for (const label of trackersList.querySelectorAll('[data-tracker-source]')) {
        const status = statuses.get(label.dataset.trackerSource);
        if (status && label.textContent !== status) label.textContent = status;
      }
    }
    renderCardActivity();
    if (completed && !draggedSchool) await refresh();
  } catch {
    renderCardActivity();
  } finally {
    pollingTrackers = false;
  }
}
pollTrackers();
setInterval(pollTrackers, 1000);
document.addEventListener('visibilitychange', pollTrackers);
setInterval(() => {
  const focused = document.activeElement;
  if (!document.hidden && !arranging && !savingOrder && !draggedSchool && !document.querySelector('dialog[open]') && !focused.closest('#trackers, #updates, #schools')) refresh();
}, 30000);

const brandingDialog = document.querySelector('#branding-dialog');
const modeForm = document.querySelector('#branding-mode');
const schoolForm = document.querySelector('#school-branding');
const schoolSelect = document.querySelector('#branding-school');
const colorInput = document.querySelector('#branding-color');
const logoInput = document.querySelector('#branding-logo');
const keyInput = document.querySelector('#branding-key');
const brandingMessage = document.querySelector('#branding-message');
let brandingSettings;
const brandingHosts = document.querySelector('#branding-hosts');
const selectAllSchools = document.querySelector('#branding-select-all');
const hostChecks = () => [...brandingHosts.querySelectorAll('input')];

function updateSchoolCount() {
  const checks = hostChecks();
  const selected = checks.filter(input => input.checked).length;
  selectAllSchools.checked = checks.length > 0 && selected === checks.length;
  selectAllSchools.indeterminate = selected > 0 && selected < checks.length;
  selectAllSchools.disabled = !checks.length;
  document.querySelector('#branding-count').textContent = checks.length
    ? `${selected} of ${checks.length} selected` : 'Import a checklist to add a school.';
}

selectAllSchools.addEventListener('change', () => {
  for (const input of hostChecks()) input.checked = selectAllSchools.checked;
  updateSchoolCount();
});
brandingHosts.addEventListener('change', updateSchoolCount);

function updateKeyField() {
  const automatic = new FormData(modeForm).get('mode') === 'automatic';
  document.querySelector('#branding-key-row').hidden = !automatic;
  document.querySelector('#branding-school-list').hidden = !automatic;
  keyInput.required = automatic && !brandingSettings?.configured;
  document.querySelector('#branding-key-hint').textContent = brandingSettings?.configured
    ? 'A key is saved on this Mac. Leave blank to keep it.' : 'Enter a key from your Brandfetch developer account.';
}

let styleLoad = 0;
async function loadSchoolStyle() {
  const load = ++styleLoad;
  const host = schoolSelect.value;
  colorInput.value = '';
  logoInput.value = '';
  if (!host) return;
  const controls = [...schoolForm.querySelectorAll('input, button')];
  controls.forEach(control => { control.disabled = true; });
  try {
    const brand = await request('/branding?host=' + encodeURIComponent(host));
    if (load === styleLoad) colorInput.value = brand.custom?.color ?? '';
  } catch (error) {
    if (load === styleLoad) brandingMessage.textContent = error.message;
  } finally {
    if (load === styleLoad) controls.forEach(control => { control.disabled = false; });
  }
}

function openBranding(host) {
  brandingDialog.classList.toggle('single-school', Boolean(host));
  modeForm.hidden = Boolean(host);
  brandingMessage.textContent = '';
  modeForm.elements.mode.value = brandingSettings?.mode ?? 'manual';
  updateKeyField();
  schoolSelect.replaceChildren();
  brandingHosts.replaceChildren();
  const selected = new Set(brandingSettings?.hosts ?? []);
  const legacyAutomatic = brandingSettings?.mode === 'automatic' && brandingSettings.hosts == null;
  for (const card of schools.querySelectorAll('.school-card')) {
    const school = card.querySelector('h2').textContent;
    const host = card.dataset.host;
    add(schoolSelect, 'option', school).value = host;
    const label = add(brandingHosts, 'label');
    label.className = 'branding-choice';
    const input = add(label, 'input');
    input.type = 'checkbox';
    input.value = host;
    input.checked = legacyAutomatic || selected.has(host);
    add(label, 'span', school);
  }
  updateSchoolCount();
  schoolForm.hidden = !schoolSelect.options.length;
  if (host) schoolSelect.value = host;
  schoolSelect.hidden = Boolean(host);
  schoolForm.querySelector('label[for=branding-school]').hidden = Boolean(host);
  if (!brandingDialog.open) brandingDialog.showModal();
  loadSchoolStyle();
}

document.querySelector('#branding-open').addEventListener('click', () => { schoolDialog.close(); openBranding(); });
document.querySelector('#branding-close').addEventListener('click', () => brandingDialog.close());
brandingDialog.addEventListener('close', () => { keyInput.value = ''; });
modeForm.addEventListener('change', updateKeyField);
schoolSelect.addEventListener('change', loadSchoolStyle);

modeForm.addEventListener('submit', async event => {
  event.preventDefault();
  const mode = new FormData(modeForm).get('mode');
  const button = modeForm.querySelector('button');
  button.disabled = true;
  try {
    const hosts = hostChecks().filter(input => input.checked).map(input => input.value);
    await request('/branding/settings', { mode, hosts, key: mode === 'automatic' ? keyInput.value.trim() : '' });
    brandingSettings = await request('/branding/settings');
    keyInput.value = '';
    updateKeyField();
    brandingRequests.clear();
    renderSnapshots(universityRecords);
    brandingMessage.textContent = mode === 'automatic' ? 'Automatic branding enabled.' : 'Manual branding enabled.';
  } catch (error) {
    brandingMessage.textContent = error.message;
  } finally {
    button.disabled = false;
  }
});

function readLogo(file) {
  if (file.size > 512 * 1024 || !['image/png', 'image/jpeg', 'image/webp'].includes(file.type)) {
    throw new Error('Choose a PNG, JPEG, or WebP image under 512 KB.');
  }
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result);
    reader.onerror = () => reject(new Error('Could not read the image.'));
    reader.readAsDataURL(file);
  });
}

async function saveSchoolStyle(reset) {
  const host = schoolSelect.value;
  const color = reset ? null : colorInput.value.trim() || null;
  const file = logoInput.files[0];
  schoolForm.inert = true;
  try {
    const data = { host, color };
    if (reset) data.logo = null;
    else if (file) data.logo = await readLogo(file);
    await request('/branding/school', data);
    brandingRequests.delete(host);
    renderSnapshots(universityRecords);
    if (reset) colorInput.value = '';
    logoInput.value = '';
    brandingMessage.textContent = reset ? 'School style reset.' : 'School style saved.';
  } catch (error) {
    brandingMessage.textContent = error.message;
  } finally {
    schoolForm.inert = false;
  }
}

schoolForm.addEventListener('submit', event => {
  event.preventDefault();
  saveSchoolStyle(false);
});
document.querySelector('#branding-reset').addEventListener('click', () => saveSchoolStyle(true));

request('/branding/settings').then(settings => {
  brandingSettings = settings;
  if (settings.mode) return;
  const firstRender = new MutationObserver(() => {
    firstRender.disconnect();
    openBranding();
  });
  if (schools.childElementCount) openBranding();
  else firstRender.observe(schools, { childList: true });
}).catch(() => {});

const schoolDialog = document.querySelector('#school-dialog');
const fields = document.querySelector('#school-fields');
const schoolName = document.querySelector('#school-name');
const schoolPortal = document.querySelector('#school-portal');
const schoolMessage = document.querySelector('#school-message');
const loginLink = document.querySelector('#school-login');
let editingSchool;

function openSchool(record) {
  editingSchool = record;
  document.querySelector('#school-dialog-title').textContent = record ? 'Edit school' : 'Add school';
  document.querySelector('#school-edit-actions').hidden = !record;
  schoolName.value = record?.school ?? '';
  schoolPortal.value = record?.portal ?? record?.source ?? '';
  loginLink.dataset.editLogin = record?.source ?? '';
  loginLink.href = '#';
  schoolMessage.textContent = '';
  schoolDialog.showModal();
  schoolName.focus();
}

document.querySelector('#add-school').addEventListener('click', () => openSchool());
document.querySelector('#school-close').addEventListener('click', () => schoolDialog.close());
document.querySelector('#school-style').addEventListener('click', () => {
  schoolDialog.close();
  openBranding(new URL(editingSchool.source).hostname);
});
loginLink.addEventListener('click', event => {
  if (event.defaultPrevented) return;
  event.preventDefault();
  schoolMessage.textContent = 'Open this dashboard in Chrome with the Coll-Track extension enabled to edit saved logins.';
});

fields.addEventListener('submit', async event => {
  event.preventDefault();
  fields.inert = true;
  try {
    await request('/schools', {
      name: schoolName.value, portal: schoolPortal.value,
      host: editingSchool ? new URL(editingSchool.source).hostname : undefined
    });
    schoolDialog.close();
    await refresh();
  } catch (error) {
    schoolMessage.textContent = error.message;
  } finally {
    fields.inert = false;
  }
});
