{
    "name": "Mina Web Fixes",
    "summary": "Perbaikan kecil pada web client Odoo (misalnya pencarian many2one saat koneksi terputus)",
    "version": "20.0.1.0.0",
    "category": "Hidden/Tools",
    "author": "Mina",
    "license": "LGPL-3",
    "depends": ["web"],
    "assets": {
        "web.assets_backend": [
            "mina_web_fix/static/src/**/*.js",
        ],
        "web.assets_unit_tests": [
            "mina_web_fix/static/tests/**/*",
        ],
    },
    "installable": True,
    "application": False,
    "auto_install": False,
}
