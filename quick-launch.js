const launchForm = document.querySelector('#launch-form');
const profileSelect = document.querySelector('#launch-profile');
const notice = document.querySelector('#launch-message');
const manual = document.querySelector('#manual');
const manageLogins = document.querySelector('#manage-logins');
let portal;
let profiles = [];
let launching = false;

const wait = ms => new Promise(resolve => setTimeout(resolve, ms));

async function openPortal(profile) {
  if (launching) return;
  launching = true;
  launchForm.hidden = true;
  manageLogins.hidden = true;
  notice.textContent = 'Opening ' + portal.hostname + '…';

  let id;
  try {
    const launcher = await chrome.tabs.getCurrent();
    ({ id } = await chrome.tabs.create({ url: portal.href, active: false }));
    let deadline = Date.now() + 18000;
    let submitted = false;
    let followedLoginLink = false;
    while (Date.now() < deadline) {
      await wait(350);
      const tab = await chrome.tabs.get(id);
      if (!tab.url || tab.pendingUrl || tab.url === 'about:blank') continue;
      const current = tab.url ? new URL(tab.url) : null;
      if (current?.origin !== portal.origin) {
        notice.textContent = 'Continue sign-in in the portal tab.';
        break;
      }
      if (!current.pathname.startsWith('/account/')) {
        const [state] = await chrome.scripting.executeScript({
          target: { tabId: id }, injectImmediately: true,
          args: [current.href, !submitted && !followedLoginLink],
          func: (expected, findLogin) => {
            if (location.href !== expected || document.readyState === 'loading' ||
                !document.body || document.querySelector('input[type="password"]')) return null;
            const loginLinks = new Set();
            if (findLogin) {
              for (const anchor of document.querySelectorAll('a[href]')) {
                if (!(anchor instanceof HTMLAnchorElement) ||
                    !/^(log|sign) in$/i.test((anchor.getAttribute('aria-label') || anchor.innerText).trim()) ||
                    !anchor.getClientRects().length || getComputedStyle(anchor).visibility === 'hidden') continue;
                let url;
                try {
                  url = new URL(anchor.href);
                } catch {
                  continue;
                }
                if (url.origin === location.origin && !url.username && !url.password &&
                    url.pathname.replace(/\/$/, '') === '/account/login') loginLinks.add(url.href);
              }
            }
            return { loginUrl: loginLinks.size === 1 ? [...loginLinks][0] : null };
          }
        }).catch(() => []);
        if (!state?.result) continue;
        if (state.result.loginUrl) {
          followedLoginLink = true;
          await chrome.tabs.update(id, { url: state.result.loginUrl });
          continue;
        }
        await chrome.tabs.update(id, { active: true });
        await chrome.tabs.remove(launcher.id);
        return;
      }
      if (current.pathname.replace(/\/$/, '') !== '/account/login') {
        notice.textContent = 'Complete the remaining sign-in step in the portal tab.';
        break;
      }
      if (submitted) {
        const [state] = await chrome.scripting.executeScript({
          target: { tabId: id }, injectImmediately: true,
          func: () => {
            const text = document.body?.innerText ?? '';
            return /invalid email address or password|incorrect password|invalid credentials/i.test(text);
          }
        });
        if (state?.result) {
          notice.textContent = 'The portal rejected this login. Check the saved details in Manage logins.';
          manageLogins.hidden = false;
          break;
        }
        continue;
      }

      const [ready] = await chrome.scripting.executeScript({
        target: { tabId: id }, injectImmediately: true, func: submitSlateLogin, args: [current.href]
      }).catch(() => []);
      if (ready?.result !== 'supported') {
        if (!ready || ready.result === 'changed' || tab.status !== 'complete') continue;
        notice.textContent = 'Continue sign-in in the portal tab.';
        break;
      }
      const [attempt] = await chrome.scripting.executeScript({
        target: { tabId: id }, injectImmediately: true, func: submitSlateLogin,
        args: [current.href, profile.email, profile.password]
      });
      if (attempt?.result === 'changed') continue;
      if (attempt?.result !== 'submitted') {
        notice.textContent = attempt?.result === 'verification'
          ? 'Complete verification in the portal tab.'
          : 'Continue sign-in in the portal tab.';
        break;
      }

      submitted = true;
      deadline = Date.now() + 12000;
      notice.textContent = 'Signing in…';
    }
    await chrome.tabs.update(id, { active: true });
    if (Date.now() >= deadline) {
      notice.textContent = submitted
        ? 'Sign-in has not finished. Check the portal tab.'
        : 'The portal is still loading. Continue in the portal tab.';
    }
  } catch {
    if (id != null) await chrome.tabs.update(id, { active: true }).catch(() => {});
    notice.textContent = 'Quick launch could not finish. Continue in the portal.';
  } finally {
    launching = false;
    launchForm.hidden = false;
  }
}

launchForm.addEventListener('submit', async event => {
  event.preventDefault();
  const profile = profiles.find(entry => entry.id === profileSelect.value);
  if (!profile || launching) return;
  const button = launchForm.querySelector('button');
  button.disabled = true;
  try {
    const granted = await chrome.permissions.request({ origins: [portal.origin + '/*'] });
    if (granted) await openPortal(profile);
    else notice.textContent = 'Open the portal manually or grant access to use a saved login.';
  } catch {
    notice.textContent = 'Could not get portal access. Open the portal manually.';
  } finally {
    button.disabled = false;
  }
});

const setup = document.querySelector('#setup-login');
const setupMode = document.querySelector('#setup-mode');
const setupCredentials = document.querySelector('#setup-credentials');
const setupHint = document.querySelector('#setup-default-hint');
let hasDefault = false;

function updateSetup() {
  const useDefault = setupMode.value === 'default';
  setupCredentials.hidden = useDefault;
  setup.elements.email.disabled = setup.elements.password.disabled = useDefault;
  setupHint.hidden = !hasDefault || setupMode.value !== 'new-default';
}

setupMode.addEventListener('change', updateSetup);
setup.addEventListener('submit', async event => {
  event.preventDefault();
  const fields = new FormData(setup);
  const mode = fields.get('mode');
  const login = { mode };
  if (mode !== 'default') {
    login.email = fields.get('email').trim();
    login.password = fields.get('password');
  }
  setup.inert = true;
  try {
    await LocalBackend.request('/schools/login', { host: portal.hostname, url: portal.href, login });
    setup.elements.password.value = '';
    const { profiles: savedProfiles } = await LocalBackend.request('/profiles');
    profiles = savedProfiles.filter(profile => profile.hosts.includes(portal.hostname));
    const preferred = profiles.find(profile => profile.id === 'coll-track-default' && mode !== 'separate') ??
      profiles.find(profile => profile.name === portal.hostname + ' · School login');
    showProfiles(preferred);
    setup.hidden = true;
    notice.textContent = 'Login saved. Open the portal to sign in.';
    launchForm.querySelector('button').focus();
  } catch (error) {
    notice.textContent = error.message;
  } finally {
    setup.inert = false;
  }
});

function showProfiles(preferred) {
  profileSelect.replaceChildren(...profiles.map(profile =>
    new Option(profile.name + ' · ' + profile.email, profile.id)));
  if (preferred) profileSelect.value = preferred.id;
  else profileSelect.selectedIndex = -1;
  launchForm.hidden = false;
}

async function initializeLaunch() {
  try {
    const source = new URLSearchParams(location.search).get('source');
    const [snapshots, trackers] = await Promise.all([
      LocalBackend.request('/snapshots'), LocalBackend.request('/trackers')
    ]);
    const saved = snapshots.find(snapshot => snapshot.source === source) ??
      snapshots.find(snapshot => new URL(snapshot.source).origin + '/' === source);
    if (!saved) throw new Error('This portal is no longer in your saved checklists.');
    const base = new URL(saved.source);
    const tracker = trackers.find(tracker => tracker.source === source) ??
      trackers.find(tracker => new URL(tracker.source).hostname === base.hostname);
    const trackedUrl = tracker?.url;
    portal = new URL(saved.portal ?? trackedUrl ?? saved.source);
    if (portal.protocol !== 'https:' || portal.origin !== base.origin || portal.port ||
        portal.username || portal.password) throw new Error('This portal address is unsupported.');

    document.querySelector('#title').textContent = saved.school;
    manual.href = portal.href;
    manual.hidden = false;

    const { profiles: allProfiles } = await LocalBackend.request('/profiles');
    profiles = allProfiles.filter(profile => profile.hosts.includes(portal.hostname));
    if (!profiles.length) {
      hasDefault = allProfiles.some(profile => profile.id === 'coll-track-default');
      if (hasDefault) {
        setupMode.add(new Option('Use default login', 'default'), 0);
        setupMode.value = 'default';
        setupMode.querySelector('option[value="new-default"]').textContent = 'Replace and use default login';
      }
      updateSetup();
      setup.hidden = false;
      notice.textContent = 'Choose a login for this school.';
      setupMode.focus();
      return;
    }

    const allowed = await chrome.permissions.contains({ origins: [portal.origin + '/*'] });
    const preferred = profiles.find(profile => profile.id === tracker?.profile_id) ??
      (profiles.length === 1 ? profiles[0] : null);
    if (preferred && allowed) {
      await openPortal(preferred);
      return;
    }
    showProfiles(preferred);
    notice.textContent = allowed
      ? 'Choose a saved login for ' + portal.hostname + '.'
      : 'Chrome will ask for access to ' + portal.hostname + ' the first time.';
    profileSelect.focus();
  } catch (error) {
    notice.textContent = error.message;
  }
}

initializeLaunch();
