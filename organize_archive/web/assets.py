"""Static asset paths and bodies for the app shell.

``Path(__file__).with_name(...)`` resolves relative to *this* module, so it
must live in ``web/``, beside ``index.html`` and ``vendor/`` -- not in
``web/routes/``, one level deeper, where the same expression would silently
point at a directory that doesn't exist. Both ``server.py`` and
``routes/static.py`` import the resolved paths from here instead of
recomputing them.
"""

from __future__ import annotations

from pathlib import Path

INDEX = Path(__file__).with_name("index.html")
VENDOR_DIR = Path(__file__).with_name("vendor")

MANIFEST = {
    "name": "Trove",
    "short_name": "Trove",
    "start_url": "/",
    "scope": "/",
    "display": "standalone",
    "background_color": "#f5f5f7",
    "theme_color": "#f5f5f7",
    "icons": [
        {"src": "/icon-192.png", "sizes": "192x192", "type": "image/png"},
        {
            "src": "/icon-512.png",
            "sizes": "512x512",
            "type": "image/png",
            "purpose": "any maskable",
        },
    ],
}
SW = (
    "self.addEventListener('install',e=>self.skipWaiting());\n"
    "self.addEventListener('activate',e=>self.clients.claim());\n"
    "self.addEventListener('fetch',e=>{});\n"
)
