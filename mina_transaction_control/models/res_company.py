from odoo import fields, models


class ResCompany(models.Model):
    _inherit = 'res.company'

    mina_customer_category_id = fields.Many2one(
        'res.partner.category', string="Tag Customer",
        help="Di Quotation/Sales Order dan Invoice customer, kolom Customer hanya menampilkan kontak dengan tag ini "
             "(atau sub-tag-nya). Kosongkan untuk menampilkan semua kontak.",
    )
    mina_supplier_category_id = fields.Many2one(
        'res.partner.category', string="Tag Vendor",
        help="Di Purchase Order dan Vendor Bill, kolom Vendor hanya menampilkan kontak dengan tag ini "
             "(atau sub-tag-nya). Kosongkan untuk menampilkan semua kontak.",
    )
