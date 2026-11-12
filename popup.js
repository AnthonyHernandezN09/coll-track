const importButton = document.querySelector('#import');
const downloadButton = document.querySelector('#download');
const message = document.querySelector('#message');
const preview = document.querySelector('#preview');
let currentImport = null;
let trackingUrl = null;
const trackButton = document.querySelector('#track');

function add(parent, tag, value) {
  const element = document.createElement(tag);
  element.textContent = value;
  parent.append(element);
  return element;
}

function render(result) {
  for (const checklist of result.checklists) {
    const kind = { admissions: 'Admissions', enrollment: 'Enrollment', forms: 'Forms' };
    const heading = `${kind[checklist.kind]} · ${checklist.title}`;
    add(preview, 'h2', heading);
    if (!checklist.items.length) add(preview, 'p', 'No readable items in this checklist.');
    if (checklist.skippedRows) add(preview, 'p', `${checklist.skippedRows} rows could not be read.`);
    if (!checklist.items.length) continue;
    const list = add(preview, 'ul', '');
    for (const item of checklist.items) {
      const row = add(list, 'li', '');
      add(row, 'strong', item.item);
      const status = item.status === 'unknown'
        ? 'Status unknown' : item.rawStatus?.replace(/^status\s*:\s*/i, '') || item.status;
      const date = item.dateText ? ` · ${checklist.dateLabel}: ${item.dateText}` : '';
      add(row, 'span', status + date);
    }
  }
  for (const section of result.unreadable) {
    add(preview, 'p', `Could not match the columns in ${section.title}.`);
  }
}

importButton.addEventListener('click', async () => {
  currentImport = null;
  trackingUrl = null;
  trackButton.hidden = true;
  preview.replaceChildren();
  preview.hidden = true;
  downloadButton.hidden = true;
  importButton.disabled = true;
  message.textContent = 'Reading checklist…';

  try {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    const url = tab?.url ? new URL(tab.url) : null;
    if (url?.protocol !== 'https:' || tab.id == null) {
      message.textContent = 'Open your college checklist in an HTTPS tab.';
      return;
    }
    const records = await LocalBackend.request('/snapshots').catch(() => []);
    const saved = records.find(record => new URL(record.source).hostname === url.hostname);
    const school = saved?.school ?? url.hostname;
    if (url.pathname.startsWith('/account/')) {
      message.textContent = 'Sign in and open your application checklist.';
      return;
    }

    const target = { tabId: tab.id };
    const [injection] = await chrome.scripting.executeScript({ target, files: ['checklist-reader.js'] });
    const [scan] = await chrome.scripting.executeScript({
      target: { tabId: tab.id, documentIds: [injection.documentId] },
      args: [url.origin, url.pathname],
      func: (origin, path) => {
        if (location.origin !== origin || location.pathname !== path) return null;
        return globalThis.CollTrack.readChecklists(document);
      }
    });

    const result = scan?.result;
    if (!result) {
      message.textContent = 'The tab changed. Open your checklist and try again.';
      return;
    }
    render(result);
    preview.hidden = !preview.childElementCount;
    const count = result.checklists.reduce((sum, checklist) => sum + checklist.items.length, 0);
    if (!count) {
      message.textContent = result.unreadable.length || result.checklists.length
        ? `${school} · No checklist rows to export.`
        : `${school} · Open the application’s checklist and try again.`;
      return;
    }

    currentImport = { school, source: url.origin + url.pathname, importedAt: new Date().toISOString(), ...result };
    message.textContent = `${school} · ${count} checklist ${count === 1 ? 'item' : 'items'}`;
    downloadButton.hidden = false;
    url.hash = '';
    trackingUrl = url;
    try {
      await LocalBackend.request('/snapshots', currentImport);
      message.textContent += ' · Saved on this Mac';
      trackButton.hidden = false;
    } catch (error) {
      add(preview, 'p', error.message);
    }
  } catch {
    message.textContent = 'Could not read this tab. Open your checklist and try again.';
  } finally {
    importButton.disabled = false;
  }
});

downloadButton.addEventListener('click', () => {
  if (!currentImport) return;
  const file = new Blob([JSON.stringify(currentImport, null, 2)], { type: 'application/json' });
  const url = URL.createObjectURL(file);
  const link = document.createElement('a');
  link.href = url;
  link.download = 'coll-track-checklist.json';
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
});

trackButton.addEventListener('click', async () => {
  const url = trackingUrl;
  const snapshot = currentImport;
  if (!url || !snapshot) return;
  trackButton.disabled = true;
  importButton.disabled = true;
  try {
    await LocalBackend.request('/trackers/register', {
      url: url.href, source: snapshot.source, school: snapshot.school
    });
    message.textContent = 'Tracking is on. Change the interval in the dashboard.';
    trackButton.hidden = true;
  } catch (error) {
    message.textContent = error.message;
  } finally {
    trackButton.disabled = false;
    importButton.disabled = false;
  }
});
