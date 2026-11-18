from odoo import api, fields, models


class SaleOrderLine(models.Model):
    _inherit = 'sale.order.line'

    mina_part_availability = fields.Selection(related='product_id.part_availability', string="Status Part")
    mina_substitute_count = fields.Integer(compute='_compute_mina_substitute_count', string="Jumlah Substitusi")

    @api.depends('product_id')
    def _compute_mina_substitute_count(self):
        for line in self:
            line.mina_substitute_count = len(line.product_id.substitute_product_ids)

    @api.onchange('product_id')
    def _mina_onchange_product_substitute(self):
        if not self.product_id or self.env.context.get('sale_onchange_first_call'):
            return None
        message = self.product_id.product_tmpl_id._mina_substitute_warning(check_stock=True)
        if message:
            return {'warning': {'title': self.env._("Part Substitusi"), 'message': message}}
        return None

    def action_mina_open_substitute(self):
        self.ensure_one()
        return self.env['mina.part.substitute.wizard']._open_for_line(self)

    def _mina_replace_product(self, product):
        """Replace the product of the line by a substitute, keeping the quantity."""
        self.ensure_one()
        self.product_id = product
        self._reset_price_unit()
