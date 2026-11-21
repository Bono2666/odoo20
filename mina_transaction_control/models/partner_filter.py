from odoo import api, fields, models

# The partner of a document must have the tag of the document (or one of its sub-tags), directly or through
# its company. No filter when no tag is set on the company.
PARTNER_DOMAIN = (
    "mina_partner_category_id and ['|', ('category_id', 'child_of', mina_partner_category_id), "
    "('commercial_partner_id.category_id', 'child_of', mina_partner_category_id)] or []"
)


class SaleOrder(models.Model):
    _inherit = 'sale.order'

    partner_id = fields.Many2one(domain=PARTNER_DOMAIN)
    mina_partner_category_id = fields.Many2one(
        'res.partner.category', string="Tag Customer", compute='_compute_mina_partner_category_id',
    )

    @api.depends('company_id')
    def _compute_mina_partner_category_id(self):
        for order in self:
            order.mina_partner_category_id = (order.company_id or self.env.company).mina_customer_category_id


class PurchaseOrder(models.Model):
    _inherit = 'purchase.order'

    partner_id = fields.Many2one(domain=PARTNER_DOMAIN)
    mina_partner_category_id = fields.Many2one(
        'res.partner.category', string="Tag Vendor", compute='_compute_mina_partner_category_id',
    )

    @api.depends('company_id')
    def _compute_mina_partner_category_id(self):
        for order in self:
            order.mina_partner_category_id = (order.company_id or self.env.company).mina_supplier_category_id


class AccountMove(models.Model):
    _inherit = 'account.move'

    partner_id = fields.Many2one(domain=PARTNER_DOMAIN)
    mina_partner_category_id = fields.Many2one(
        'res.partner.category', string="Tag Partner", compute='_compute_mina_partner_category_id',
    )

    @api.depends('company_id', 'move_type')
    def _compute_mina_partner_category_id(self):
        for move in self:
            company = move.company_id or self.env.company
            if move.is_sale_document(include_receipts=True):
                move.mina_partner_category_id = company.mina_customer_category_id
            elif move.is_purchase_document(include_receipts=True):
                move.mina_partner_category_id = company.mina_supplier_category_id
            else:
                move.mina_partner_category_id = False
