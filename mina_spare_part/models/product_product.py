from odoo import models


class ProductProduct(models.Model):
    _inherit = 'product.product'

    # The product variant form inherits the template form: its spare part buttons run on the template.

    def action_mina_view_change_log(self):
        return self.product_tmpl_id.action_mina_view_change_log()

    def action_mina_compute_part_price(self):
        return self.product_tmpl_id.action_mina_compute_part_price()

    def action_mina_view_incoming(self):
        return self.product_tmpl_id.action_mina_view_incoming()

    def action_mina_view_stock(self):
        """Stock of the products in each warehouse location, in a dialog."""
        return {
            'type': 'ir.actions.act_window',
            'name': self.env._("Stok per Lokasi: %s", self[:1].display_name) if len(self) == 1
            else self.env._("Stok per Lokasi"),
            'res_model': 'stock.quant',
            'view_mode': 'list',
            'views': [(self.env.ref('mina_spare_part.mina_stock_quant_view_list').id, 'list')],
            'domain': [('product_id', 'in', self.ids), ('location_id.usage', '=', 'internal')],
            'context': {'create': False, 'edit': False, 'delete': False},
            'target': 'new',
        }
