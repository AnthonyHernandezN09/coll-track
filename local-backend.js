const LocalBackend = (() => {
  const address = 'http://127.0.0.1:46387';

  async function request(path, body) {
    let response;
    try {
      response = await fetch(address + path, {
        method: body === undefined ? 'GET' : 'PUT',
        headers: {
          'X-Coll-Track': 'extension',
          ...(body === undefined ? {} : { 'Content-Type': 'application/json' })
        },
        body: body === undefined ? undefined : JSON.stringify(body),
        redirect: 'error',
        signal: AbortSignal.timeout(4000)
      });
    } catch {
      throw new Error('The Coll-Track service on your Mac is unreachable.');
    }
    if (!response.ok) {
      const result = await response.json().catch(() => ({}));
      throw new Error(result.error || 'The local service could not complete the request.');
    }
    return response.json();
  }

  return { request, address };
})();
