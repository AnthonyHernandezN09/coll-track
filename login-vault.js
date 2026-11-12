const LoginVault = (() => {
  async function load() {
    return LocalBackend.request('/profiles');
  }

  async function save(session, profiles) {
    const result = await LocalBackend.request('/profiles', { profiles, previous: session.revision });
    session.revision = result.revision;
    session.profiles = profiles;
  }

  function portalHosts(input) {
    return [...new Set(input.trim().split(/[\s,]+/).filter(Boolean).map(entry => {
      const url = new URL(entry.includes('://') ? entry : 'https://' + entry);
      if (url.protocol !== 'https:' || url.port || url.username || url.password ||
          !/^[a-z0-9-]+(?:\.[a-z0-9-]+)+$/.test(url.hostname)) {
        throw new Error('Enter an HTTPS portal address for each school.');
      }
      return url.hostname;
    }))];
  }

  return { load, save, portalHosts };
})();
