{
    "name": "Kontrol Transaksi",
    "summary": "Filter customer/vendor berdasarkan tag kontak, dan approval harga jual di bawah pricelist pada Sales Order",
    "version": "20.0.1.0.0",
    "category": "Sales/Sales",
    "author": "Mina",
    "license": "LGPL-3",
    "depends": ["sale", "purchase"],
    "data": [
        "views/res_config_settings_views.xml",
        "views/sale_order_views.xml",
        "views/partner_filter_views.xml",
    ],
    "post_init_hook": "post_init_hook",
    "installable": True,
    "application": False,
}
