chrome.runtime.onMessage.addListener((message, sender, respond) => {
  if (!['quick-launch', 'edit-login'].includes(message?.type) || sender.url !== 'http://127.0.0.1:46387/' ||
      sender.frameId !== 0 || typeof message.source !== 'string') return;
  const query = new URLSearchParams({ source: message.source });
  const page = message.type === 'edit-login' ? 'profiles.html' : 'quick-launch.html';
  if (message.type === 'edit-login') query.set('edit', '1');
  chrome.tabs.create({ url: chrome.runtime.getURL(page) + '?' + query })
    .then(() => respond({ opened: true }), () => respond({ opened: false }));
  return true;
});
