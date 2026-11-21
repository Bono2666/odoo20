from odoo import fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    mina_customer_category_id = fields.Many2one(related='company_id.mina_customer_category_id', readonly=False)
    mina_supplier_category_id = fields.Many2one(related='company_id.mina_supplier_category_id', readonly=False)
