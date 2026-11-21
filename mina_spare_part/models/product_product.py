from odoo import models


class ProductProduct(models.Model):
    _inherit = 'product.product'

    # The product variant form inherits the template form: its spare part buttons run on the template.

    def action_mina_view_change_log(self):
        return self.product_tmpl_id.action_mina_view_change_log()

    def action_mina_compute_part_price(self):
        return self.product_tmpl_id.action_mina_compute_part_price()
