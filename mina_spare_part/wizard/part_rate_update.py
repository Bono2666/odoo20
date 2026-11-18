from odoo import api, fields, models
from odoo.exceptions import UserError


class MinaPartRateUpdate(models.TransientModel):
    _name = 'mina.part.rate.update'
    _description = "Update Spare Part Exchange Rate"

    rule_id = fields.Many2one('mina.part.price.rule', string="Setting Harga", required=True)
    currency_id = fields.Many2one(related='rule_id.currency_id', string="Mata Uang")
    company_currency_id = fields.Many2one(related='rule_id.company_currency_id')
    rate_source = fields.Selection(related='rule_id.rate_source')
    old_rate = fields.Float(related='rule_id.current_rate', string="Kurs Saat Ini")
    new_rate = fields.Float(string="Kurs Baru", digits=(16, 4), required=True)
    date = fields.Date(string="Tanggal Berlaku", required=True, default=fields.Date.context_today)
    note = fields.Char(string="Keterangan")
    apply_same_currency = fields.Boolean(
        string="Terapkan ke Semua Setting Mata Uang Ini", default=True,
        help="Kurs baru juga dipakai oleh setting harga lain dengan mata uang yang sama "
             "(misalnya Engine dan Airend sama-sama USD).",
    )
    update_odoo_rate = fields.Boolean(
        string="Update Kurs di Odoo",
        compute='_compute_update_odoo_rate', store=True, readonly=False,
        help="Simpan juga kurs ini ke Currency Rate Odoo (dipakai modul lain seperti Accounting).",
    )
    recompute_prices = fields.Boolean(string="Hitung Ulang Harga Jual", default=True)
    rule_ids = fields.Many2many('mina.part.price.rule', string="Setting yang Diupdate", compute='_compute_rule_ids')

    @api.depends('rate_source')
    def _compute_update_odoo_rate(self):
        for wizard in self:
            wizard.update_odoo_rate = wizard.rate_source == 'odoo'

    @api.depends('rule_id', 'apply_same_currency')
    def _compute_rule_ids(self):
        for wizard in self:
            rules = wizard.rule_id
            if wizard.apply_same_currency and wizard.rule_id:
                rules |= self.env['mina.part.price.rule'].search([
                    ('company_id', '=', wizard.rule_id.company_id.id),
                    ('currency_id', '=', wizard.rule_id.currency_id.id),
                ])
            wizard.rule_ids = rules

    def action_confirm(self):
        self.ensure_one()
        if self.new_rate <= 0:
            raise UserError(self.env._("Kurs baru harus lebih besar dari 0."))
        rules = self.rule_ids
        if self.currency_id == self.company_currency_id:
            raise UserError(self.env._("Mata uang setting harga sama dengan mata uang perusahaan, tidak perlu kurs."))
        old_rates = {rule: rule.current_rate for rule in rules}
        rules.manual_rate = self.new_rate
        odoo_updated = self.update_odoo_rate or any(rule.rate_source == 'odoo' for rule in rules)
        if odoo_updated:
            self._update_odoo_rate()
        rules.invalidate_recordset(['current_rate'])
        histories = self.env['mina.part.rate.history'].create([{
            'rule_id': rule.id,
            'currency_id': rule.currency_id.id,
            'date': self.date,
            'old_rate': old_rates[rule],
            'new_rate': rule.current_rate,
            'odoo_rate_updated': odoo_updated,
            'note': self.note,
        } for rule in rules])
        if self.recompute_prices:
            action = rules._start_recompute('rate', histories)
            action['params']['next'] = {'type': 'ir.actions.act_window_close'}
            action['params']['title'] = self.env._("Kurs %s diupdate", self.currency_id.name)
            return action
        return {'type': 'ir.actions.act_window_close'}

    def _update_odoo_rate(self):
        """Store the rate (1 foreign unit = new_rate company currency) as an Odoo currency rate."""
        self.ensure_one()
        company = self.rule_id.company_id.root_id
        # Currency rates are restricted to settings/accounting users: the Spare Part manager
        # is allowed to maintain the rate of the pricing currency from this wizard only.
        CurrencyRate = self.env['res.currency.rate'].sudo().with_company(company)
        rate = CurrencyRate.search([
            ('currency_id', '=', self.currency_id.id),
            ('company_id', '=', company.id),
            ('name', '=', self.date),
        ], limit=1)
        if rate:
            rate.inverse_company_rate = self.new_rate
        else:
            CurrencyRate.create({
                'currency_id': self.currency_id.id,
                'company_id': company.id,
                'name': self.date,
                'inverse_company_rate': self.new_rate,
            })
        if not self.currency_id.active:
            self.currency_id.sudo().active = True
