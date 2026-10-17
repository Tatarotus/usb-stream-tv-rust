// Service Worker para PWA do Controle Remoto
const CACHE_NAME = 'controle-tv-v23';
const STATIC_ASSETS = [
    '/',
    '/manifest.json',
    '/api/app-icon.png',
    '/api/app-icon-512.png',
    '/api/screenshot-desktop.png',
    '/api/screenshot-mobile.png'
];

self.addEventListener('install', (event) => {
    event.waitUntil(
        caches.open(CACHE_NAME).then((cache) => {
            return cache.addAll(STATIC_ASSETS).catch((err) => {
                console.warn('SW pre-cache error:', err);
            });
        })
    );
    self.skipWaiting();
});

self.addEventListener('activate', (event) => {
    event.waitUntil(
        caches.keys().then((keys) => {
            return Promise.all(
                keys.map((key) => {
                    if (key !== CACHE_NAME) {
                        return caches.delete(key);
                    }
                })
            );
        }).then(() => self.clients.claim())
    );
});

self.addEventListener('fetch', (event) => {
    if (event.request.method !== 'GET') return;
    const url = new URL(event.request.url);

    // 1. Intercepta Compartilhamento do YouTube (Web Share Target) diretamente no Service Worker
    if ((url.pathname === '/' || url.pathname === '') && (url.searchParams.has('text') || url.searchParams.has('url') || url.searchParams.has('title'))) {
        const rawText = url.searchParams.get('text') || '';
        const rawUrl = url.searchParams.get('url') || '';
        const rawTitle = url.searchParams.get('title') || '';
        const combined = (rawUrl + ' ' + rawText + ' ' + rawTitle).trim();
        const match = combined.match(/https?:\/\/[^\s"'<>]+/);

        if (match && match[0]) {
            const targetUrl = match[0].replace(/[),;.]+$/, '');
            let videoTitle = rawTitle.trim();
            if (!videoTitle && rawText) {
                const before = rawText.replace(/https?:\/\/[^\s"'<>]+.*/, '').trim();
                if (before) videoTitle = before;
            }
            if (!videoTitle) videoTitle = 'Vídeo do YouTube';

            // Dispara download em segundo plano no servidor
            event.waitUntil(
                fetch('/api/vod/prepare', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json', 'X-Auth-PIN': '1233' },
                    body: JSON.stringify({ url: targetUrl, title: videoTitle, pin: '1233' })
                }).then(r => r.json()).then(data => {
                    if (data && data.success) {
                        return self.registration.showNotification('📥 Salvando no Cinema da TV!', {
                            body: `"${videoTitle}" foi adicionado à fila do Cinema. Você será avisado quando terminar.`,
                            icon: '/api/app-icon.png',
                            badge: '/api/app-icon.png',
                            data: { url: '/?tab=vod' }
                        });
                    }
                }).catch(() => {})
            );
        }

        // Responde com o app normalmente
        event.respondWith(
            fetch(event.request).catch(() => caches.match('/'))
        );
        return;
    }

    if (url.pathname.startsWith('/live') || url.pathname.startsWith('/stream') || url.pathname.startsWith('/vod/') || url.pathname.startsWith('/api/status') || url.pathname.startsWith('/api/catalog') || url.pathname.startsWith('/api/vod/status') || url.pathname.startsWith('/api/favorites') || url.pathname.startsWith('/api/logo')) {
        return;
    }

    event.respondWith(
        fetch(event.request)
            .then((response) => {
                if (response && response.status === 200 && response.type === 'basic') {
                    const cloned = response.clone();
                    caches.open(CACHE_NAME).then((c) => c.put(event.request, cloned));
                }
                return response;
            })
            .catch(() => caches.match(event.request).then((res) => res || caches.match('/')))
    );
});

// ==================== PUSH NOTIFICATIONS ====================
self.addEventListener('push', (event) => {
    let data = { title: '🍿 Controle da TV', body: 'Seu filme já está pronto para assistir na TV!', url: '/?tab=vod' };
    if (event.data) {
        try {
            data = Object.assign(data, event.data.json());
        } catch (e) {
            data.body = event.data.text();
        }
    }
    const options = {
        body: data.body,
        icon: '/api/app-icon.png',
        badge: '/api/app-icon.png',
        vibrate: [200, 100, 200],
        tag: data.tag || 'vod-ready-notification',
        renotify: true,
        data: { url: data.url || '/?tab=vod' }
    };
    event.waitUntil(self.registration.showNotification(data.title, options));
});

self.addEventListener('notificationclick', (event) => {
    event.notification.close();
    const targetUrl = (event.notification.data && event.notification.data.url) || '/?tab=vod';
    event.waitUntil(
        clients.matchAll({ type: 'window', includeUncontrolled: true }).then((windowClients) => {
            for (let client of windowClients) {
                if ('focus' in client) {
                    return client.focus();
                }
            }
            if (clients.openWindow) {
                return clients.openWindow(targetUrl);
            }
        })
    );
});

// Mensagens internas para exibição de notificação pelo Service Worker
self.addEventListener('message', (event) => {
    if (event.data && event.data.type === 'SHOW_NOTIFICATION') {
        const payload = event.data.payload || {};
        self.registration.showNotification(payload.title || '🍿 Filme Pronto na TV!', {
            body: payload.body || 'Seu vídeo já está disponível para assistir!',
            icon: '/api/app-icon.png',
            badge: '/api/app-icon.png',
            vibrate: [200, 100, 200],
            tag: payload.tag || 'vod-status-update',
            renotify: true,
            data: { url: payload.url || '/?tab=vod' }
        });
    }
});
