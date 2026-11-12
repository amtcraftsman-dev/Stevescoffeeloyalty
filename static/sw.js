self.addEventListener('fetch', event => {
  if (event.request.mode !== 'navigate') return;
  event.respondWith(fetch(event.request).catch(() => new Response(
    '<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>Steve’s Coffee | Offline</title><main><h1>You’re offline</h1><p>Reconnect to check rewards or place an order.</p><p><a href="/">Try again</a></p></main></html>',
    { headers: { 'Content-Type': 'text/html; charset=utf-8' }, status: 503 },
  )));
});
