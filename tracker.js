chrome.runtime.onMessage.addListener((message, sender, respond) => {
  if (message?.type !== 'quick-launch' || sender.url !== 'http://127.0.0.1:46387/' ||
      sender.frameId !== 0 || typeof message.source !== 'string') return;
  const query = new URLSearchParams({ source: message.source });
  chrome.tabs.create({ url: chrome.runtime.getURL('quick-launch.html') + '?' + query })
    .then(() => respond({ opened: true }), () => respond({ opened: false }));
  return true;
});
