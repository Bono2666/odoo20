{
    "name": "Online Users",
    "summary": "List of users currently active in Odoo",
    "version": "20.0.1.1.0",
    "category": "Tools",
    "author": "Mina",
    "license": "LGPL-3",
    "depends": ["base", "mail"],
    "data": [
        "views/online_users_views.xml",
    ],
    "assets": {
        "web.assets_backend": [
            "mina_online_users/static/src/**/*.js",
        ],
    },
    "installable": True,
    "application": False,
}
