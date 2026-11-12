() => {
  if (!document.body || document.querySelector('input[type="password"], iframe[src*="captcha" i]')) return null;
  const text = document.body.innerText.split(/\r?\n/).map(line => line.replace(/\s+/g, ' ').trim()).filter(Boolean);
  if (!text.length) return null;
  const resources = [...document.querySelectorAll('a[href], img[src]')].flatMap(node => {
    if (!node.getClientRects().length || getComputedStyle(node).visibility !== 'visible') return [];
    const target = new URL(node.href || node.currentSrc || node.src, location.href);
    if (target.protocol !== 'https:' || target.username || target.password) return [];
    return [node.matches('a') ? 'Link: ' + target.origin + target.pathname : 'Image: ' + target.origin + target.pathname];
  });
  return { ...globalThis.CollTrack.readChecklists(document), page: [...text, ...resources] };
}
