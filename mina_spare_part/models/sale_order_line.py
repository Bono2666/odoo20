from odoo import api, fields, models


class SaleOrderLine(models.Model):
    _inherit = 'sale.order.line'

    mina_part_type = fields.Selection(related='product_id.part_type', string="Jenis Part")
    mina_part_availability = fields.Selection(related='product_id.part_availability', string="Status Part")
    mina_part_stock_jpn = fields.Float(related='product_id.part_stock_jpn', string="Stock JPN")
    mina_part_lead_time = fields.Integer(related='product_id.part_lead_time', string="Lead Time Principal (Hari)")
    mina_substitute_count = fields.Integer(compute='_compute_mina_substitute_count', string="Jumlah Substitusi")
    mina_part_replacement_id = fields.Many2one(
        'product.template', string="Part Pengganti", compute='_compute_mina_part_chain',
        help="Part substitusi terdekat (juga substitusi dari substitusi) yang sudah punya harga jual.",
    )
    mina_part_replacement_price = fields.Monetary(
        string="Harga Pengganti", compute='_compute_mina_part_chain', currency_field='currency_id',
        help="Harga jual part pengganti sesuai pricelist order ini.",
    )
    mina_part_earliest_ids = fields.Many2many(
        'product.template', string="Part Paling Awal", compute='_compute_mina_part_chain',
        help="Part paling awal diproduksi dari rangkaian substitusi part ini.",
    )

    @api.depends('product_id')
    def _compute_mina_substitute_count(self):
        for line in self:
            line.mina_substitute_count = len(line.product_id.substitute_product_ids)

    @api.depends('product_id', 'product_uom_qty', 'order_id.pricelist_id', 'order_id.date_order', 'currency_id')
    def _compute_mina_part_chain(self):
        for line in self:
            template = line.product_id.product_tmpl_id
            replacement = earliest = template.browse()
            if template.part_type:
                substitutes = template._mina_get_substitutes()[0]
                replacement = template._mina_get_recommended_substitute(substitutes)
                earliest = template._mina_get_earliest_parts()
            line.mina_part_replacement_id = replacement
            line.mina_part_replacement_price = line._mina_order_price(replacement) if replacement else 0.0
            line.mina_part_earliest_ids = earliest

    @api.onchange('product_id')
    def _mina_onchange_product_substitute(self):
        if not self.product_id or self.env.context.get('sale_onchange_first_call'):
            return None
        message = self.product_id.product_tmpl_id._mina_substitute_warning(
            check_stock=True, price_func=self._mina_order_price, currency=self.currency_id,
        )
        if message:
            return {'warning': {'title': self.env._("Part Substitusi"), 'message': message}}
        return None

    def action_mina_open_substitute(self):
        self.ensure_one()
        return self.env['mina.part.substitute.wizard']._open_for_line(self)

    def _mina_order_price(self, template):
        """Selling price of a part for this order: pricelist of the customer, order currency."""
        self.ensure_one()
        product = template.product_variant_id
        if not product:
            return 0.0
        order = self.order_id
        return order.pricelist_id._get_product_price(
            product, self.product_uom_qty or 1.0,
            currency=self.currency_id or order.currency_id or order.company_id.currency_id,
            date=order.date_order or fields.Datetime.now(),
        )

    def _mina_replace_product(self, product):
        """Replace the product of the line by a substitute, keeping the quantity."""
        self.ensure_one()
        self.product_id = product
        self._reset_price_unit()
