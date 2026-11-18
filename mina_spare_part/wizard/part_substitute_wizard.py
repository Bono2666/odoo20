from odoo import api, fields, models
from odoo.exceptions import UserError
from odoo.fields import Command

EDITABLE_ORDER_STATES = ('draft', 'sent')


class MinaPartSubstituteWizard(models.TransientModel):
    _name = 'mina.part.substitute.wizard'
    _description = "Replace an Order Line Part by a Substitute"

    sale_line_id = fields.Many2one('sale.order.line', string="Baris Sales Order", readonly=True, ondelete='cascade')
    purchase_line_id = fields.Many2one(
        'purchase.order.line', string="Baris Purchase Order", readonly=True, ondelete='cascade',
    )
    product_tmpl_id = fields.Many2one('product.template', string="Part di Order", readonly=True)
    part_availability = fields.Selection(related='product_tmpl_id.part_availability')
    qty_available = fields.Float(related='product_tmpl_id.qty_available', string="Stock Mina")
    line_ids = fields.One2many('mina.part.substitute.wizard.line', 'wizard_id', string="Part Substitusi")

    @api.model
    def _open_for_line(self, order_line):
        """Open the wizard listing the substitutes of the part of a sale or purchase order line."""
        order_line.ensure_one()
        if order_line.order_id.state not in EDITABLE_ORDER_STATES:
            raise UserError(self.env._("Part hanya bisa diganti selama order masih berupa draft/penawaran."))
        template = order_line.product_id.product_tmpl_id
        substitutes, levels = template._mina_get_substitutes()
        if not substitutes:
            raise UserError(self.env._("Part %s tidak punya part substitusi.", template.display_name))
        line_field = 'sale_line_id' if order_line._name == 'sale.order.line' else 'purchase_line_id'
        wizard = self.create({
            line_field: order_line.id,
            'product_tmpl_id': template.id,
            'line_ids': [
                Command.create({'product_tmpl_id': substitute.id, 'level': levels[substitute.id]})
                for substitute in substitutes
            ],
        })
        return {
            'type': 'ir.actions.act_window',
            'name': self.env._("Ganti dengan Part Substitusi"),
            'res_model': self._name,
            'res_id': wizard.id,
            'view_mode': 'form',
            'target': 'new',
        }

    def _replace_with(self, template):
        self.ensure_one()
        order_line = self.sale_line_id or self.purchase_line_id
        if not order_line:
            raise UserError(self.env._("Baris order tidak ditemukan."))
        if order_line.order_id.state not in EDITABLE_ORDER_STATES:
            raise UserError(self.env._("Part hanya bisa diganti selama order masih berupa draft/penawaran."))
        product = template.product_variant_id
        if not product:
            raise UserError(self.env._("Part %s tidak punya varian produk aktif.", template.display_name))
        old_product = order_line.product_id
        order_line._mina_replace_product(product)
        order_line.order_id.message_post(body=self.env._(
            "Part %(old)s diganti dengan part substitusi %(new)s.",
            old=old_product.display_name, new=product.display_name,
        ))


class MinaPartSubstituteWizardLine(models.TransientModel):
    _name = 'mina.part.substitute.wizard.line'
    _description = "Substitute Part Candidate"
    _order = 'level, id'

    wizard_id = fields.Many2one('mina.part.substitute.wizard', required=True, ondelete='cascade')
    product_tmpl_id = fields.Many2one('product.template', string="Part Substitusi", required=True, readonly=True)
    level = fields.Integer(
        string="Level", readonly=True,
        help="1 = substitusi langsung, 2 = substitusi dari part substitusi, dan seterusnya.",
    )
    default_code = fields.Char(related='product_tmpl_id.default_code', string="Part No.")
    part_availability = fields.Selection(related='product_tmpl_id.part_availability')
    qty_available = fields.Float(related='product_tmpl_id.qty_available', string="Stock Mina")
    part_stock_jpn = fields.Float(related='product_tmpl_id.part_stock_jpn')
    part_lead_time = fields.Integer(related='product_tmpl_id.part_lead_time')
    currency_id = fields.Many2one(related='product_tmpl_id.currency_id')
    part_reseller_price = fields.Monetary(related='product_tmpl_id.part_reseller_price', currency_field='currency_id')
    list_price = fields.Float(related='product_tmpl_id.list_price', string="Harga End User")

    def action_select(self):
        self.ensure_one()
        self.wizard_id._replace_with(self.product_tmpl_id)
        return {'type': 'ir.actions.act_window_close'}
