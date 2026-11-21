from odoo import api, fields, models


class StockMove(models.Model):
    _inherit = 'stock.move'

    mina_done_by_id = fields.Many2one('res.users', string="Divalidasi Oleh", readonly=True, copy=False)

    def _action_done(self, cancel_backorder=False):
        moves = super()._action_done(cancel_backorder=cancel_backorder)
        moves.filtered(lambda move: move.state == 'done' and not move.mina_done_by_id).write({
            'mina_done_by_id': self.env.user.id,
        })
        return moves


class StockMoveLine(models.Model):
    _inherit = 'stock.move.line'

    mina_partner_id = fields.Many2one(
        'res.partner', string="Vendor / Partner", related='picking_id.partner_id', store=True, index='btree_not_null',
    )
    mina_purchase_ref = fields.Char(related='move_id.purchase_line_id.order_id.name', string="No. PO")
    mina_purchase_currency_id = fields.Many2one(related='move_id.purchase_line_id.currency_id', string="Mata Uang PO")
    mina_purchase_price = fields.Float(related='move_id.purchase_line_id.price_unit', string="Harga Beli")
    mina_done_by_id = fields.Many2one(related='move_id.mina_done_by_id')

    @api.model
    def _mina_incoming_domain(self):
        """Done move lines bringing goods into a warehouse location (receipt, inventory adjustment, return)."""
        return [
            ('state', '=', 'done'),
            ('location_dest_id.usage', '=', 'internal'),
            ('location_id.usage', 'not in', ('internal', 'transit')),
        ]
