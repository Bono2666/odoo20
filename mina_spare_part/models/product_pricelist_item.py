from odoo import fields, models


class ProductPricelistItem(models.Model):
    _inherit = 'product.pricelist.item'

    mina_part_rule_id = fields.Many2one(
        'mina.part.price.rule', string="Setting Harga Part", readonly=True, index='btree_not_null',
        ondelete='set null',
        help="Harga reseller yang diisi otomatis oleh Setting Harga part ini.",
    )
