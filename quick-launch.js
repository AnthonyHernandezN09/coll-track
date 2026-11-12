const launchForm = document.querySelector('#launch-form');
const profileSelect = document.querySelector('#launch-profile');
const notice = document.querySelector('#launch-message');
const manual = document.querySelector('#manual');
let portal;
let profiles = [];
let launching = false;

const wait = ms => new Promise(resolve => setTimeout(resolve, ms));

async function openPortal(profile) {
  if (launching) return;
  launching = true;
  launchForm.hidden = true;
  notice.textContent = 'Opening ' + portal.hostname + '…';

  let id;
  try {
    const launcher = await chrome.tabs.getCurrent();
    ({ id } = await chrome.tabs.create({ url: portal.href, active: false }));
    let deadline = Date.now() + 18000;
    let submitted = false;
    while (Date.now() < deadline) {
      await wait(350);
      const tab = await chrome.tabs.get(id);
      if (tab.status !== 'complete' || tab.pendingUrl || tab.url === 'about:blank') continue;
      const current = tab.url ? new URL(tab.url) : null;
      if (current?.origin !== portal.origin) {
        notice.textContent = 'Continue sign-in in the portal tab.';
        break;
      }
      if (!current.pathname.startsWith('/account/')) {
        await chrome.tabs.update(id, { active: true });
        await chrome.tabs.remove(launcher.id);
        return;
      }
      if (current.pathname !== '/account/login') {
        notice.textContent = 'Complete the remaining sign-in step in the portal tab.';
        break;
      }
      if (submitted) {
        const [state] = await chrome.scripting.executeScript({
          target: { tabId: id },
          func: () => {
            const text = document.body?.innerText ?? '';
            return /invalid email address or password|incorrect password|invalid credentials/i.test(text);
          }
        });
        if (state?.result) {
          notice.textContent = 'The portal rejected this login. Check the saved details in Manage logins.';
          break;
        }
        continue;
      }

      const [attempt] = await chrome.scripting.executeScript({
        target: { tabId: id }, func: submitSlateLogin,
        args: [current.href, profile.email, profile.password]
      });
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

async function initializeLaunch() {
  try {
    const source = new URLSearchParams(location.search).get('source');
    const [snapshots, trackers] = await Promise.all([
      LocalBackend.request('/snapshots'), LocalBackend.request('/trackers')
    ]);
    const saved = snapshots.find(snapshot => snapshot.source === source);
    if (!saved) throw new Error('This portal is no longer in your saved checklists.');
    const base = new URL(saved.source);
    const trackedUrl = trackers.find(tracker => tracker.source === source)?.url;
    portal = new URL(saved.portal ?? trackedUrl ?? saved.source);
    if (portal.protocol !== 'https:' || portal.origin !== base.origin || portal.port ||
        portal.username || portal.password) throw new Error('This portal address is unsupported.');

    document.querySelector('#title').textContent = saved.school;
    manual.href = portal.href;
    manual.hidden = false;

    const { profiles: allProfiles } = await LocalBackend.request('/profiles');
    profiles = allProfiles.filter(profile => profile.hosts.includes(portal.hostname));
    if (!profiles.length) {
      const addLogin = document.querySelector('#add-login');
      addLogin.href = 'profiles.html?' + new URLSearchParams({ source: saved.source });
      addLogin.hidden = false;
      notice.textContent = 'No saved login for ' + portal.hostname + '.';
      addLogin.focus();
      return;
    }

    for (const profile of profiles) {
      const option = new Option(profile.name + ' · ' + profile.email, profile.id);
      profileSelect.add(option);
    }
    const allowed = await chrome.permissions.contains({ origins: [portal.origin + '/*'] });
    if (profiles.length === 1 && allowed) {
      await openPortal(profiles[0]);
      return;
    }
    if (profiles.length > 1) profileSelect.selectedIndex = -1;
    launchForm.hidden = false;
    notice.textContent = allowed
      ? 'Choose a saved login for ' + portal.hostname + '.'
      : 'Chrome will ask for access to ' + portal.hostname + ' the first time.';
    profileSelect.focus();
  } catch (error) {
    notice.textContent = error.message;
  }
}

initializeLaunch();
