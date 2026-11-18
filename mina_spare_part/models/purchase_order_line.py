from odoo import api, fields, models


class PurchaseOrderLine(models.Model):
    _inherit = 'purchase.order.line'

    mina_part_availability = fields.Selection(related='product_id.part_availability', string="Status Part")
    mina_substitute_count = fields.Integer(compute='_compute_mina_substitute_count', string="Jumlah Substitusi")

    @api.depends('product_id')
    def _compute_mina_substitute_count(self):
        for line in self:
            line.mina_substitute_count = len(line.product_id.substitute_product_ids)

    @api.onchange('product_id')
    def _mina_onchange_product_substitute(self):
        if not self.product_id:
            return None
        message = self.product_id.product_tmpl_id._mina_substitute_warning()
        if message:
            return {'warning': {'title': self.env._("Part Substitusi"), 'message': message}}
        return None

    def action_mina_open_substitute(self):
        self.ensure_one()
        return self.env['mina.part.substitute.wizard']._open_for_line(self)

    def _mina_replace_product(self, product):
        """Replace the product of the line by a substitute, keeping the quantity.

        The price is reset so it is taken again from the vendor price (or the cost) of the new product.
        """
        self.ensure_one()
        self.product_id = product
        self._product_id_change()
        self.update({'price_unit': 0.0, 'technical_price_unit': 0.0})
        self._compute_price_unit_and_date_planned_and_name()
