if (location.origin === 'http://127.0.0.1:46387' && location.pathname === '/') {
  document.addEventListener('click', event => {
    const link = event.target.closest('a[data-quick-launch]');
    if (!link || !event.isTrusted || event.button !== 0 ||
        event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    if (!chrome.runtime?.id) return;

    event.preventDefault();
    chrome.runtime.sendMessage({ type: 'quick-launch', source: link.dataset.quickLaunch })
      .then(result => {
        if (!result?.opened) window.location.assign(link.href);
      })
      .catch(() => window.location.assign(link.href));
  }, true);
}
