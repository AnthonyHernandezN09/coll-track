if (location.origin === 'http://127.0.0.1:46387' && location.pathname === '/') {
  document.addEventListener('click', event => {
    const link = event.target.closest('a[data-quick-launch], a[data-edit-login]');
    if (!link || !event.isTrusted || event.button !== 0 ||
        event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    if (!chrome.runtime?.id) return;

    event.preventDefault();
    const edit = link.hasAttribute('data-edit-login');
    const type = edit ? 'edit-login' : 'quick-launch';
    const source = edit ? link.dataset.editLogin : link.dataset.quickLaunch;
    chrome.runtime.sendMessage({ type, source })
      .then(result => {
        if (!edit && !result?.opened) window.location.assign(link.href);
      })
      .catch(() => { if (!edit) window.location.assign(link.href); });
  }, true);
}
