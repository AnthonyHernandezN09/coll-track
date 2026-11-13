const chooseLogin = document.querySelector('#choose-login');
const loginNotice = document.querySelector('#login-notice');
const loginSelect = document.querySelector('#saved-login');
let matchingLogins = [];
let loginTab;

async function findLoginTab() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab?.url || tab.id == null) return null;
  const url = new URL(tab.url);
  if (url.protocol !== 'https:' || url.port || url.pathname.replace(/\/$/, '') !== '/account/login') return null;
  return { id: tab.id, url };
}

document.querySelector('#manage-profiles').addEventListener('click', () => {
  chrome.runtime.openOptionsPage();
});

const dashboardButton = document.querySelector('#open-dashboard');
LocalBackend.request('/capabilities').then(({ dashboard }) => {
  dashboardButton.hidden = !dashboard;
}).catch(() => {});
dashboardButton.addEventListener('click', () => {
  chrome.tabs.create({ url: LocalBackend.address });
});

async function showMatchingLogins() {
  try {
    loginTab = await findLoginTab();
    if (!loginTab) return;
    const { profiles } = await LoginVault.load();
    matchingLogins = profiles.filter(profile => profile.hosts.includes(loginTab.url.hostname));
    if (!matchingLogins.length) {
      loginNotice.textContent = 'No profiles match ' + loginTab.url.hostname + '.';
      return;
    }

    for (const profile of matchingLogins) {
      const option = document.createElement('option');
      option.value = profile.id;
      option.textContent = profile.name + ' · ' + profile.email;
      loginSelect.append(option);
    }
    if (matchingLogins.length > 1) loginSelect.selectedIndex = -1;
    loginNotice.textContent = loginTab.url.hostname;
    chooseLogin.hidden = false;
  } catch (error) {
    loginNotice.textContent = error.message;
  }
}

showMatchingLogins();

chooseLogin.addEventListener('submit', async event => {
  event.preventDefault();
  const profile = matchingLogins.find(saved => saved.id === loginSelect.value);
  if (!profile) return;
  chooseLogin.inert = true;
  loginNotice.textContent = 'Submitting login…';

  try {
    const current = await findLoginTab();
    if (current?.id !== loginTab.id || current.url.href !== loginTab.url.href) {
      chooseLogin.hidden = true;
      loginNotice.textContent = 'The active tab changed. Open the portal and try again.';
      return;
    }
    const [attempt] = await chrome.scripting.executeScript({
      target: { tabId: loginTab.id },
      func: submitSlateLogin,
      args: [loginTab.url.href, profile.email, profile.password]
    });
    const messages = {
      submitted: 'Login submitted. Check the portal for the result.',
      verification: 'Complete the verification on the portal.',
      changed: 'The login page changed. Open it and try again.',
      unsupported: 'Could not verify a standard Slate login form on this page.'
    };
    if (attempt?.result === 'submitted' || attempt?.result === 'changed') chooseLogin.hidden = true;
    loginNotice.textContent = messages[attempt?.result] ?? 'Could not submit login.';
  } catch {
    loginNotice.textContent = 'Could not submit login. Check the portal before trying again.';
  } finally {
    chooseLogin.inert = false;
  }
});

const schoolToggle = document.querySelector('#add-school');
const importSchool = document.querySelector('#import-school');
const schoolForm = document.querySelector('#add-school-form');
const schoolName = document.querySelector('#new-school-name');
const schoolPortal = document.querySelector('#new-school-portal');
const schoolStatus = document.querySelector('#add-school-message');
const setUpLogin = document.querySelector('#set-up-login');
let detectedSchool;

function showSchoolForm(show) {
  schoolForm.hidden = !show;
  schoolToggle.setAttribute('aria-expanded', String(show));
  if (show) schoolName.focus();
}

schoolToggle.addEventListener('click', () => showSchoolForm(schoolForm.hidden));
importSchool.addEventListener('click', () => {
  schoolName.value = detectedSchool.name;
  schoolPortal.value = detectedSchool.portal;
  showSchoolForm(true);
  schoolName.select();
});

async function detectSchool() {
  try {
    const tab = await findLoginTab();
    if (!tab) return;
    const [scan] = await chrome.scripting.executeScript({
      target: { tabId: tab.id }, func: submitSlateLogin, args: [tab.url.href]
    });
    if (scan?.result !== 'supported') return;
    const saved = await LocalBackend.request('/snapshots');
    if (saved.some(record => new URL(record.source).hostname === tab.url.hostname)) return;
    const target = new URL(tab.url.searchParams.get('r') || '/apply/status', tab.url);
    const safeTarget = new URL(tab.url.origin);
    safeTarget.pathname = target.pathname;
    for (const key of ['cmd', 'tab']) {
      const value = target.searchParams.get(key);
      if (value && /^[a-z0-9_-]{1,40}$/i.test(value)) safeTarget.searchParams.set(key, value);
    }
    detectedSchool = { name: tab.url.hostname, portal: safeTarget.href };
    importSchool.hidden = false;
  } catch {
    // Manual add is available when the active tab cannot be inspected.
  }
}
detectSchool();

schoolForm.addEventListener('submit', async event => {
  event.preventDefault();
  schoolForm.inert = true;
  schoolStatus.textContent = '';
  setUpLogin.hidden = true;
  try {
    const { source } = await LocalBackend.request('/schools', { name: schoolName.value, portal: schoolPortal.value });
    const host = new URL(schoolPortal.value).hostname;
    schoolStatus.textContent = 'Added to My schools.';
    if (detectedSchool && new URL(detectedSchool.portal).hostname === host) importSchool.hidden = true;
    setUpLogin.href = chrome.runtime.getURL('quick-launch.html') + '?' + new URLSearchParams({ source });
    setUpLogin.target = '_blank';
    setUpLogin.hidden = false;
  } catch (error) {
    schoolStatus.textContent = error.message;
  } finally {
    schoolForm.inert = false;
  }
});
