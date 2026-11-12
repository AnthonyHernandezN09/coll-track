const byId = id => document.getElementById(id);
const editor = byId('editor');
const profileForm = byId('profile');
const notice = byId('notice');
let session;
let editingId;
let launchSource;
let launchHost;

function edit(profile) {
  editingId = profile?.id;
  byId('profile-name').value = profile?.name ?? '';
  byId('profile-hosts').value = profile?.hosts.join('\n') ?? '';
  byId('profile-email').value = profile?.email ?? '';
  byId('profile-password').value = '';
  byId('profile-password').required = !profile;
  byId('password-hint').hidden = !profile;
  byId('delete-profile').hidden = !profile;
  profileForm.hidden = false;
  byId('profile-name').focus();
}

function renderProfiles() {
  const list = byId('profiles');
  list.replaceChildren();
  for (const profile of session.profiles) {
    const row = document.createElement('li');
    const button = document.createElement('button');
    button.type = 'button';
    button.textContent = profile.name;
    button.addEventListener('click', () => edit(profile));
    const hosts = document.createElement('span');
    hosts.textContent = profile.hosts.join(', ');
    row.append(button, hosts);
    list.append(row);
  }
  profileForm.reset();
  profileForm.hidden = true;
  editingId = undefined;
}

async function loadProfiles() {
  try {
    session = await LoginVault.load();
    renderProfiles();
    editor.hidden = false;
    notice.textContent = session.profiles.length ? '' : 'Create your first login profile.';
    const source = new URLSearchParams(location.search).get('source');
    if (!source) return;
    const snapshots = await LocalBackend.request('/snapshots');
    const school = snapshots.find(snapshot => snapshot.source === source || (new URL(source).pathname === '/' &&
      new URL(snapshot.source).origin === new URL(source).origin));
    if (!school) return;
    const url = new URL(school.source);
    if (url.protocol !== 'https:' || url.username || url.password || url.port) return;
    launchSource = source;
    launchHost = url.hostname;
    const matches = session.profiles.filter(profile => profile.hosts.includes(launchHost));
    if (new URLSearchParams(location.search).has('edit') && matches.length) {
      if (matches.length === 1) edit(matches[0]);
      else notice.textContent = 'Choose a profile for ' + launchHost + ' from the list.';
      return;
    }
    edit();
    byId('profile-name').value = school.school;
    byId('profile-hosts').value = launchHost;
    byId('profile-email').focus();
  } catch (error) {
    notice.textContent = error.message;
  }
}

byId('add-profile').addEventListener('click', () => edit());

async function save(profiles, success) {
  editor.inert = true;
  try {
    await LoginVault.save(session, profiles);
    renderProfiles();
    notice.textContent = success;
    const link = byId('resume-launch');
    const ready = launchSource && profiles.some(profile => profile.hosts.includes(launchHost));
    link.hidden = !ready;
    if (ready) {
      link.href = 'quick-launch.html?' + new URLSearchParams({ source: launchSource });
      link.focus();
    }
  } catch (error) {
    notice.textContent = error.message || 'Could not save profiles.';
  } finally {
    editor.inert = false;
  }
}

profileForm.addEventListener('submit', async event => {
  event.preventDefault();
  try {
    const hosts = LoginVault.portalHosts(byId('profile-hosts').value);
    const name = byId('profile-name').value.trim();
    if (!name || !hosts.length) throw new Error('Enter a name and at least one portal.');
    const previous = session.profiles.find(profile => profile.id === editingId);
    const profile = {
      id: editingId ?? crypto.randomUUID(), name, hosts,
      email: byId('profile-email').value.trim(),
      password: byId('profile-password').value || previous?.password
    };
    const profiles = previous
      ? session.profiles.map(saved => saved.id === editingId ? profile : saved)
      : [...session.profiles, profile];
    await save(profiles, 'Profile saved.');
  } catch (error) {
    notice.textContent = error.message;
  }
});

byId('delete-profile').addEventListener('click', async () => {
  await save(session.profiles.filter(profile => profile.id !== editingId), 'Profile deleted.');
});

loadProfiles();
