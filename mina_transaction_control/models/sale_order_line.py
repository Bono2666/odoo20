from odoo import api, fields, models
from odoo.tools import float_compare

# Changes of these fields on a confirmed order are checked against the price approval.
APPROVAL_FIELDS = ('product_id', 'product_uom_id', 'product_uom_qty', 'price_unit', 'discount')


class SaleOrderLine(models.Model):
    _inherit = 'sale.order.line'

    mina_reference_price = fields.Float(
        string="Harga Pricelist", digits='Product Price', compute='_compute_mina_price_approval', store=True,
        help="Harga satuan bersih sesuai pricelist order (setelah diskon pricelist).",
    )
    mina_needs_approval = fields.Boolean(
        string="Perlu Approval", compute='_compute_mina_price_approval', store=True,
        help="Harga bersih (harga x (1 - diskon)) lebih rendah dari harga pricelist.",
    )

    @api.depends(
        'product_id', 'product_uom_id', 'product_uom_qty', 'price_unit', 'discount', 'display_type',
        'is_downpayment', 'pricelist_item_id', 'order_id.pricelist_id', 'order_id.fiscal_position_id', 'currency_id',
    )
    def _compute_mina_price_approval(self):
        digits = self.env['decimal.precision'].precision_get('Product Price')
        for line in self:
            reference = 0.0
            needs_approval = False
            if line._mina_price_approval_applies():
                reference = line._mina_get_reference_price()
                if reference > 0:
                    net_price = line.price_unit * (1.0 - (line.discount or 0.0) / 100.0)
                    needs_approval = float_compare(net_price, reference, precision_digits=digits) < 0
            line.mina_reference_price = reference
            line.mina_needs_approval = needs_approval

    def _mina_price_approval_applies(self):
        self.ensure_one()
        return bool(
            self.product_id
            and self.order_id
            and not self.display_type
            and not self.is_downpayment
            and not self.combo_item_id
            and self.product_type != 'combo'
            and not self._is_global_discount()
            and not getattr(self, 'is_delivery', False)
        )

    def _mina_get_reference_price(self):
        """Net unit price given by the pricelist, in the tax mode of the order (as set by the price reset)."""
        self.ensure_one()
        line = self.with_company(self.company_id)
        price = line._get_pricelist_price()
        product_taxes = line.product_id.taxes_id._filter_taxes_by_company(line.company_id)
        price = line.product_id._get_tax_included_unit_price_from_price(
            price,
            product_taxes=product_taxes,
            fiscal_position=line.order_id.fiscal_position_id,
            document_tax_mode=line.document_tax_mode,
        )
        return line.product_id._adapt_price_unit_to_document_tax_mode(
            price, product_taxes, line.product_uom_id, line.document_tax_mode,
        )

    @api.model_create_multi
    def create(self, vals_list):
        lines = super().create(vals_list)
        lines.order_id._mina_check_confirmed_approval()
        return lines

    def write(self, vals):
        result = super().write(vals)
        if set(vals) & set(APPROVAL_FIELDS):
            self.order_id._mina_check_confirmed_approval()
        return result
