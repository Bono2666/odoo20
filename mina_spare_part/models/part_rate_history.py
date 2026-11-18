from odoo import api, fields, models


class MinaPartRateHistory(models.Model):
    _name = 'mina.part.rate.history'
    _description = "Spare Part Exchange Rate History"
    _order = 'date desc, id desc'

    rule_id = fields.Many2one(
        'mina.part.price.rule', string="Setting Harga", required=True, index=True, ondelete='cascade',
    )
    company_id = fields.Many2one(related='rule_id.company_id', store=True, string="Perusahaan")
    currency_id = fields.Many2one('res.currency', string="Mata Uang", required=True)
    date = fields.Date(string="Tanggal Berlaku", required=True, default=fields.Date.context_today)
    old_rate = fields.Float(string="Kurs Lama", digits=(16, 4))
    new_rate = fields.Float(string="Kurs Baru", digits=(16, 4), required=True)
    change_pct = fields.Float(string="Perubahan (%)", compute='_compute_change_pct', store=True, digits=(16, 2))
    odoo_rate_updated = fields.Boolean(string="Kurs Odoo Diupdate")
    products_updated = fields.Integer(string="Produk Berubah Harga")
    user_id = fields.Many2one('res.users', string="Diupdate Oleh", default=lambda self: self.env.user)
    note = fields.Char(string="Keterangan")

    @api.depends('old_rate', 'new_rate')
    def _compute_change_pct(self):
        for history in self:
            if history.old_rate:
                history.change_pct = (history.new_rate - history.old_rate) / history.old_rate * 100
            else:
                history.change_pct = 0.0
