if ('serviceWorker' in navigator) {
  navigator.serviceWorker.register('/sw.js').catch(error => {
    console.warn('Could not register the Steve’s Coffee service worker.', error);
  });
}
