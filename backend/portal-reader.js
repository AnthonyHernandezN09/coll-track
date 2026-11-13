() => {
  if (!document.body || document.querySelector('input[type="password"], iframe[src*="captcha" i]')) return null;
  const text = document.body.innerText.split(/\r?\n/).map(line => line.replace(/\s+/g, ' ').trim()).filter(Boolean);
  if (!text.length) return null;
  const resources = [...document.querySelectorAll('a[href]')].flatMap(node => {
    if (!node.getClientRects().length || getComputedStyle(node).visibility !== 'visible') return [];
    const address = node.getAttribute('href');
    if (!address) return [];
    let target;
    try {
      target = new URL(address, document.baseURI);
    } catch {
      return [];
    }
    if (target.protocol !== 'https:' || target.username || target.password) return [];
    return ['Link: ' + target.origin + target.pathname];
  });
  return { ...globalThis.CollTrack.readChecklists(document), page: [...text, ...resources] };
}
