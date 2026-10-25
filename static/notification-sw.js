self.addEventListener('notificationclick', event => {
  const target = new URL(event.notification.data.url, self.location.origin);
  event.notification.close();
  event.waitUntil(self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then(clients => {
    const existing = clients.find(client => new URL(client.url).origin === target.origin);
    if (existing) {
      existing.navigate(target.href);
      return existing.focus();
    }
    return self.clients.openWindow(target.href);
  }));
});
