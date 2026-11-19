import math

from odoo import api, fields, models
from odoo.exceptions import UserError, ValidationError
from odoo.fields import Command, Domain
from odoo.tools import float_compare, float_round
from odoo.tools.safe_eval import safe_eval

from .product_template import LOG_RULE_KEY

DEFAULT_COST_FORMULA = (
    "price * (1 - discount / 100) * kurs * (1 + freight / 100) * (1 + duty / 100) * (1 + other / 100)"
)
DEFAULT_RESELLER_FORMULA = "cost * (1 + margin_reseller / 100)"
DEFAULT_SALE_FORMULA = "cost * (1 + margin / 100)"

RECOMPUTE_SYNC_LIMIT = 1000
RECOMPUTE_BATCH_SIZE = 1000

# Errors raised by safe_eval on a broken or impossible formula.
FORMULA_ERRORS = (ValueError, SyntaxError, NameError, TypeError, ZeroDivisionError, ArithmeticError)


class MinaPartPriceRule(models.Model):
    _name = 'mina.part.price.rule'
    _description = "Spare Part Pricing Rule"
    _inherit = ['mail.thread']
    _order = 'sequence, id'
    _check_company_auto = True

    def _default_currency_id(self):
        return self.env.ref('base.JPY', raise_if_not_found=False) or self.env.company.currency_id

    name = fields.Char(string="Nama", required=True, tracking=True)
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)
    company_id = fields.Many2one('res.company', string="Perusahaan", required=True, default=lambda self: self.env.company)
    category_id = fields.Many2one(
        'product.category', string="Kategori Part", required=True, tracking=True,
        domain="[('part_type', '!=', False)]",
        help="Setting harga berlaku untuk kategori ini dan sub-kategorinya.",
    )
    part_type = fields.Selection(related='category_id.part_type', string="Jenis Part")
    currency_id = fields.Many2one(
        'res.currency', string="Mata Uang Harga Principal", required=True, tracking=True,
        default=_default_currency_id,
    )
    company_currency_id = fields.Many2one(related='company_id.currency_id', string="Mata Uang Perusahaan")

    # Kurs
    rate_source = fields.Selection(
        [('manual', "Kurs Manual"), ('odoo', "Kurs Odoo (Currency Rate)")],
        string="Sumber Kurs", required=True, default='manual', tracking=True,
        help="Kurs Manual: memakai kurs yang diisi di setting ini.\n"
             "Kurs Odoo: memakai kurs terakhir di menu Currency Odoo.",
    )
    manual_rate = fields.Float(string="Kurs Manual", digits=(16, 4), tracking=True)
    current_rate = fields.Float(string="Kurs Berlaku", digits=(16, 4), compute='_compute_current_rate')
    rate_history_ids = fields.One2many('mina.part.rate.history', 'rule_id', string="Riwayat Kurs")

    # Parameter perhitungan (persen)
    discount_pct = fields.Float(string="Diskon Supplier (%)", tracking=True)
    freight_pct = fields.Float(string="Ongkos Kirim / Freight (%)", tracking=True)
    duty_pct = fields.Float(string="Bea Masuk (%)", tracking=True)
    other_pct = fields.Float(string="Biaya Lain (%)", tracking=True)
    reseller_margin_pct = fields.Float(string="Margin Reseller (%)", tracking=True)
    margin_pct = fields.Float(string="Margin End User (%)", tracking=True)
    cost_formula = fields.Char(
        string="Formula Harga Pokok", required=True, default=DEFAULT_COST_FORMULA, tracking=True,
    )
    reseller_formula = fields.Char(
        string="Formula Harga Reseller", required=True, default=DEFAULT_RESELLER_FORMULA, tracking=True,
    )
    sale_formula = fields.Char(
        string="Formula Harga End User", required=True, default=DEFAULT_SALE_FORMULA, tracking=True,
        help="Hasilnya disimpan sebagai Sales Price produk.",
    )
    reseller_pricelist_id = fields.Many2one(
        'product.pricelist', string="Pricelist Reseller", check_company=True, tracking=True,
        help="Jika diisi, harga reseller juga disimpan sebagai harga tetap (fixed price) di pricelist ini, "
             "sehingga bisa dipakai di penawaran/order penjualan untuk customer reseller.",
    )
    vendor_id = fields.Many2one(
        'res.partner', string="Vendor Principal", check_company=True, tracking=True,
        help="Jika diisi, Harga Principal, mata uang dan lead time setiap part disimpan sebagai harga vendor "
             "(tab Purchase pada produk), sehingga Purchase Order ke principal otomatis memakai harga tersebut.",
    )
    price_rounding = fields.Float(
        string="Pembulatan Harga Jual", default=1.0, tracking=True,
        help="Harga jual dibulatkan ke kelipatan nilai ini, misalnya 100 atau 1000. Isi 0 untuk tanpa pembulatan.",
    )
    rounding_method = fields.Selection(
        [('UP', "Ke Atas"), ('HALF-UP', "Terdekat"), ('DOWN', "Ke Bawah")],
        string="Metode Pembulatan", required=True, default='UP',
    )
    update_cost = fields.Boolean(
        string="Update Harga Pokok (Cost)", default=True,
        help="Jika aktif, hasil formula harga pokok disimpan ke field Cost produk. Untuk kategori dengan "
             "metode Average Cost (AVCO)/FIFO, cost hanya diisi selama stok part kosong; setelah ada stok, "
             "cost mengikuti harga beli sebenarnya dari penerimaan barang.",
    )
    apply_on_import = fields.Boolean(
        string="Hitung Harga Saat Import", default=True,
        help="Hitung harga jual otomatis untuk part baru/berubah harga saat import data part.",
    )

    # Simulasi
    preview_price = fields.Float(string="Contoh Harga Principal", default=1000.0)
    preview_cost = fields.Float(string="Hasil Harga Pokok", compute='_compute_preview')
    preview_reseller = fields.Float(string="Hasil Harga Reseller", compute='_compute_preview')
    preview_sale = fields.Float(string="Hasil Harga End User", compute='_compute_preview')
    preview_error = fields.Char(compute='_compute_preview')

    product_count = fields.Integer(string="Jumlah Produk", compute='_compute_product_count')

    # Background recompute of the prices (big catalogs)
    recompute_state = fields.Selection(
        [('pending', "Sedang Dihitung"), ('done', "Selesai")], string="Status Hitung Ulang", readonly=True, copy=False,
    )
    recompute_source = fields.Char(readonly=True, copy=False)
    recompute_last_id = fields.Integer(readonly=True, copy=False)
    recompute_user_id = fields.Many2one('res.users', readonly=True, copy=False)
    recompute_history_id = fields.Many2one('mina.part.rate.history', readonly=True, copy=False)
    # Background sync of the vendor prices (big catalogs)
    vendor_sync_state = fields.Selection(
        [('pending', "Sedang Disinkronkan"), ('done', "Selesai")], string="Status Sinkron Harga Vendor",
        readonly=True, copy=False,
    )
    vendor_sync_last_id = fields.Integer(readonly=True, copy=False)

    _category_company_uniq = models.Constraint(
        'UNIQUE(category_id, company_id)',
        "Setting harga untuk kategori ini sudah ada.",
    )
    _manual_rate_positive = models.Constraint(
        'CHECK (manual_rate >= 0)',
        "Kurs manual tidak boleh negatif.",
    )
    _price_rounding_positive = models.Constraint(
        'CHECK (price_rounding >= 0)',
        "Pembulatan harga tidak boleh negatif.",
    )

    @api.depends('rate_source', 'manual_rate', 'currency_id', 'company_id')
    def _compute_current_rate(self):
        for rule in self:
            try:
                rule.current_rate = rule._get_rate()
            except UserError:
                rule.current_rate = 0.0

    @api.depends(
        'preview_price', 'rate_source', 'manual_rate', 'currency_id', 'discount_pct', 'freight_pct',
        'duty_pct', 'other_pct', 'reseller_margin_pct', 'margin_pct', 'cost_formula', 'reseller_formula',
        'sale_formula', 'price_rounding', 'rounding_method',
    )
    def _compute_preview(self):
        for rule in self:
            rule.preview_error = False
            rule.preview_cost = rule.preview_reseller = rule.preview_sale = 0.0
            try:
                rule.preview_cost, rule.preview_reseller, rule.preview_sale = rule._compute_prices(
                    rule.preview_price, rule._get_rate(),
                )
            except UserError as error:
                rule.preview_error = str(error)

    def _compute_product_count(self):
        for rule in self:
            rule.product_count = self.env['product.template'].search_count(rule._get_products_domain())

    @api.constrains('cost_formula', 'reseller_formula', 'sale_formula')
    def _check_formulas(self):
        for rule in self:
            try:
                rule._compute_prices(100.0, 1.0)
            except UserError as error:
                raise ValidationError(str(error)) from error

    @api.constrains('reseller_pricelist_id', 'company_id')
    def _check_reseller_pricelist_currency(self):
        for rule in self:
            if rule.reseller_pricelist_id and rule.reseller_pricelist_id.currency_id != rule.company_id.currency_id:
                raise ValidationError(self.env._("Mata uang Pricelist Reseller harus sama dengan mata uang perusahaan."))

    @api.constrains('discount_pct')
    def _check_discount_pct(self):
        for rule in self:
            if not 0 <= rule.discount_pct < 100:
                raise ValidationError(self.env._("Diskon supplier harus antara 0 dan 100%."))

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    @api.model_create_multi
    def create(self, vals_list):
        rules = super().create(vals_list)
        rules.filtered('vendor_id')._start_vendor_sync()
        return rules

    def write(self, vals):
        res = super().write(vals)
        if vals.get('vendor_id'):
            self._start_vendor_sync()
        return res

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def action_sync_vendor_prices(self):
        self.ensure_one()
        if not self.vendor_id:
            raise UserError(self.env._("Isi Vendor Principal terlebih dahulu."))
        synced, background = self._start_vendor_sync()
        if background:
            message = self.env._("Harga vendor sedang disinkronkan di background karena jumlah part besar.")
        else:
            message = self.env._("%s harga vendor dibuat atau diupdate.", synced)
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'type': 'success',
                'title': self.env._("Sinkron harga vendor"),
                'message': message,
                'next': {'type': 'ir.actions.client', 'tag': 'soft_reload'},
            },
        }

    def action_apply_prices(self):
        self.ensure_one()
        return self._start_recompute('rule')

    def action_open_rate_update(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': self.env._("Update Kurs"),
            'res_model': 'mina.part.rate.update',
            'view_mode': 'form',
            'target': 'new',
            'context': {'default_rule_id': self.id},
        }

    def action_view_products(self):
        self.ensure_one()
        action = self.env['ir.actions.act_window']._for_xml_id('mina_spare_part.mina_part_product_action')
        action['domain'] = self._get_products_domain()
        return action

    # ------------------------------------------------------------------
    # Business methods
    # ------------------------------------------------------------------

    @api.model
    def _get_rules_by_category(self):
        rules = self.search([('company_id', '=', self.env.company.id)])
        return {rule.category_id.id: rule for rule in rules}

    def _get_products_domain(self):
        """Spare parts priced by this rule: its category and the sub-categories without their own rule."""
        self.ensure_one()
        domain = Domain('part_type', '!=', False) & Domain('categ_id', 'child_of', self.category_id.id)
        sub_rules = self.search([
            ('id', '!=', self.id),
            ('company_id', '=', self.company_id.id),
            ('category_id', 'child_of', self.category_id.id),
        ])
        if sub_rules:
            domain &= ~Domain('categ_id', 'child_of', sub_rules.category_id.ids)
        return list(domain)

    def _get_products(self):
        self.ensure_one()
        return self.env['product.template'].with_company(self.company_id).search(self._get_products_domain())

    def _start_recompute(self, source, history=None):
        """Recompute the prices of all the parts of the rules: right away for a small catalog,
        by the background job otherwise.

        :param source: change log source ('rule' or 'rate')
        :param history: mina.part.rate.history record(s) to report the number of updated parts on
        :return: notification action
        """
        history = history or self.env['mina.part.rate.history']
        result = {'updated': 0, 'unchanged': 0, 'skipped': 0}
        background = self.browse()
        for rule in self:
            if self.env['product.template'].search_count(rule._get_products_domain()) > RECOMPUTE_SYNC_LIMIT:
                background |= rule
                continue
            rule_result = rule.with_context(mina_part_log_source=source)._apply_prices(rule._get_products())
            for key, value in rule_result.items():
                result[key] += value
            history.filtered(lambda h: h.rule_id == rule).products_updated = rule_result['updated']
        if background:
            for rule in background:
                rule.write({
                    'recompute_state': 'pending',
                    'recompute_source': source,
                    'recompute_last_id': 0,
                    'recompute_user_id': self.env.uid,
                    'recompute_history_id': history.filtered(lambda h: h.rule_id == rule)[:1].id,
                })
            self.env.ref('mina_spare_part.ir_cron_mina_part_recompute')._trigger()
        message = self.env._(
            "%(updated)s produk berubah harga, %(unchanged)s tetap, %(skipped)s dilewati (harga principal kosong).",
            **result,
        )
        if background:
            message = self.env._(
                "%(message)s Harga part pada %(rules)s dihitung di background karena jumlah part besar.",
                message=message, rules=background.mapped('name'),
            )
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'type': 'success',
                'title': self.env._("Hitung ulang harga"),
                'message': message,
                'next': {'type': 'ir.actions.client', 'tag': 'soft_reload'},
            },
        }

    @api.model
    def _cron_recompute_prices(self):
        Cron = self.env['ir.cron']
        for rule in self.search([('recompute_state', '=', 'pending')]):
            rule = rule.with_user(rule.recompute_user_id or self.env.user).with_company(rule.company_id).with_context(
                mina_part_log_source=rule.recompute_source or 'rule',
            )
            while rule.recompute_state == 'pending':
                domain = Domain(rule._get_products_domain()) & Domain('id', '>', rule.recompute_last_id)
                products = self.env['product.template'].with_company(rule.company_id).search(
                    list(domain), order='id', limit=RECOMPUTE_BATCH_SIZE,
                )
                if products:
                    updated = rule._apply_prices(products)['updated']
                    rule.recompute_last_id = products[-1].id
                    if rule.recompute_history_id:
                        rule.recompute_history_id.products_updated += updated
                else:
                    rule.recompute_state = 'done'
                remaining = self.env['product.template'].search_count(
                    list(Domain(rule._get_products_domain()) & Domain('id', '>', rule.recompute_last_id)),
                )
                if not Cron._commit_progress(len(products), remaining=remaining + (rule.recompute_state == 'pending')):
                    break
        self._cron_sync_vendor_prices()
        if self.search_count(['|', ('recompute_state', '=', 'pending'), ('vendor_sync_state', '=', 'pending')], limit=1):
            self.env.ref('mina_spare_part.ir_cron_mina_part_recompute')._trigger()

    def _start_vendor_sync(self):
        """Sync the vendor prices of all the parts of the rules: right away for a small catalog,
        by the background job otherwise.

        :return: (number of vendor prices created or updated, rules synced in background)
        """
        synced = 0
        background = self.browse()
        for rule in self.filtered('vendor_id'):
            if self.env['product.template'].search_count(rule._get_products_domain()) > RECOMPUTE_SYNC_LIMIT:
                background |= rule
                continue
            synced += rule._sync_vendor_prices(rule._get_products())
            rule.vendor_sync_state = 'done'
        if background:
            background.write({'vendor_sync_state': 'pending', 'vendor_sync_last_id': 0})
            self.env.ref('mina_spare_part.ir_cron_mina_part_recompute')._trigger()
        return synced, background

    @api.model
    def _cron_sync_vendor_prices(self):
        Cron = self.env['ir.cron']
        for rule in self.search([('vendor_sync_state', '=', 'pending')]):
            rule = rule.with_company(rule.company_id)
            while rule.vendor_sync_state == 'pending':
                domain = Domain(rule._get_products_domain()) & Domain('id', '>', rule.vendor_sync_last_id)
                products = self.env['product.template'].with_company(rule.company_id).search(
                    list(domain), order='id', limit=RECOMPUTE_BATCH_SIZE,
                )
                if products and rule.vendor_id:
                    rule._sync_vendor_prices(products)
                    rule.vendor_sync_last_id = products[-1].id
                else:
                    rule.vendor_sync_state = 'done'
                remaining = self.env['product.template'].search_count(
                    list(Domain(rule._get_products_domain()) & Domain('id', '>', rule.vendor_sync_last_id)),
                )
                if not Cron._commit_progress(len(products), remaining=remaining + (rule.vendor_sync_state == 'pending')):
                    return

    def _sync_vendor_prices(self, templates):
        """Create or update the vendor price of the principal vendor for the given parts.

        Only the vendor prices without variant, validity dates and of the rule company (or shared)
        are managed; other vendor prices of the principal are left untouched.

        :return: number of vendor prices created or updated
        """
        self.ensure_one()
        vendor = self.vendor_id
        templates = templates.filtered(lambda t: t.part_list_price > 0)
        if not vendor or not templates:
            return 0
        Supplierinfo = self.env['product.supplierinfo'].with_company(self.company_id)
        infos = Supplierinfo.search([
            ('partner_id', '=', vendor.id),
            ('product_tmpl_id', 'in', templates.ids),
            ('product_id', '=', False),
            ('company_id', 'in', [self.company_id.id, False]),
            ('date_start', '=', False),
            ('date_end', '=', False),
        ])
        info_by_template = {}
        for info in infos:
            info_by_template.setdefault(info.product_tmpl_id.id, info)
        to_create = []
        count = 0
        for template in templates:
            currency = template.part_currency_id or self.currency_id
            info = info_by_template.get(template.id)
            if not info:
                to_create.append({
                    'partner_id': vendor.id,
                    'product_tmpl_id': template.id,
                    'company_id': self.company_id.id,
                    'price': template.part_list_price,
                    'currency_id': currency.id,
                    'product_code': template.default_code or False,
                    'delay': template.part_lead_time if template.part_lead_time > 0 else 1,
                })
                continue
            vals = {}
            if float_compare(info.price, template.part_list_price, precision_digits=4):
                vals['price'] = template.part_list_price
            if info.currency_id != currency:
                vals['currency_id'] = currency.id
            if (info.product_code or False) != (template.default_code or False):
                vals['product_code'] = template.default_code or False
            if template.part_lead_time > 0 and info.delay != template.part_lead_time:
                vals['delay'] = template.part_lead_time
            if vals:
                info.write(vals)
                count += 1
        if to_create:
            Supplierinfo.create(to_create)
        return count + len(to_create)

    def _get_rate(self, currency=None, date=None):
        """Kurs: value of 1 unit of the list price currency in company currency."""
        self.ensure_one()
        currency = currency or self.currency_id
        company_currency = self.company_id.currency_id
        if not currency or currency == company_currency:
            return 1.0
        if currency == self.currency_id and self.rate_source == 'manual':
            if self.manual_rate <= 0:
                raise UserError(self.env._("Kurs manual pada setting harga '%s' belum diisi.", self.name))
            return self.manual_rate
        date = date or fields.Date.context_today(self)
        has_rate = self.env['res.currency.rate'].sudo().search_count([
            ('currency_id', '=', currency.id),
            ('company_id', 'in', [self.company_id.root_id.id, False]),
            ('name', '<=', date),
        ], limit=1)
        if not has_rate:
            raise UserError(self.env._("Kurs %s belum tersedia di Odoo.", currency.name))
        return self.env['res.currency']._get_conversion_rate(currency, company_currency, self.company_id, date)

    def _get_formula_context(self, price, kurs):
        self.ensure_one()
        return {
            'price': price,
            'kurs': kurs,
            'discount': self.discount_pct,
            'freight': self.freight_pct,
            'duty': self.duty_pct,
            'other': self.other_pct,
            'margin_reseller': self.reseller_margin_pct,
            'margin': self.margin_pct,
            'ceil': math.ceil,
            'floor': math.floor,
        }

    def _eval_formula(self, formula, eval_context, label):
        try:
            return float(safe_eval(formula, dict(eval_context)))
        except FORMULA_ERRORS as error:
            raise UserError(self.env._("Formula %(label)s tidak valid: %(error)s", label=label, error=error)) from error

    def _round_price(self, amount):
        if self.price_rounding:
            return float_round(amount, precision_rounding=self.price_rounding, rounding_method=self.rounding_method)
        return (self.company_id.currency_id or self.env.company.currency_id).round(amount)

    def _compute_prices(self, price, kurs):
        """Prices in company currency for a principal price in the rule currency.

        :return: (cost, reseller price, end user price)
        """
        self.ensure_one()
        eval_context = self._get_formula_context(price, kurs)
        cost = self._eval_formula(self.cost_formula, eval_context, self.env._("harga pokok"))
        eval_context['cost'] = cost
        reseller = self._eval_formula(self.reseller_formula, eval_context, self.env._("harga reseller"))
        eval_context['reseller'] = reseller
        sale = self._eval_formula(self.sale_formula, eval_context, self.env._("harga end user"))
        company_currency = self.company_id.currency_id or self.env.company.currency_id
        return company_currency.round(cost), self._round_price(reseller), self._round_price(sale)

    def _apply_prices(self, products):
        """Compute and write the reseller price, end user price (and cost) of the given spare parts.

        :return: dict with the number of ``updated``, ``unchanged`` and ``skipped`` products
        """
        self.ensure_one()
        result = {'updated': 0, 'unchanged': 0, 'skipped': 0}
        if not products:
            return result
        rates = {}
        now = fields.Datetime.now()
        # The rule's context carries the change log source (import, rate update, ...).
        products = products.with_env(self.env).with_company(self.company_id).with_context(
            tracking_disable=True, **{LOG_RULE_KEY: self.id},
        )
        reseller_prices = {}
        for product in products:
            if not product.part_list_price:
                result['skipped'] += 1
                continue
            currency = product.part_currency_id or self.currency_id
            if currency not in rates:
                rates[currency] = self._get_rate(currency)
            cost, reseller, sale = self._compute_prices(product.part_list_price, rates[currency])
            reseller_prices[product] = reseller
            vals = {}
            if float_compare(product.list_price, sale, precision_digits=6):
                vals['list_price'] = sale
            if float_compare(product.part_reseller_price, reseller, precision_digits=6):
                vals['part_reseller_price'] = reseller
            if (
                self.update_cost
                and float_compare(product.standard_price, cost, precision_digits=6)
                and product._mina_can_update_cost()
            ):
                vals['standard_price'] = cost
            if vals:
                vals['part_price_date'] = now
                if 'standard_price' in vals and not product._mina_has_stock():
                    # Nothing to revalue without stock: skip the (slow, per product) inventory revaluation.
                    product = product.with_context(disable_auto_revaluation=True)
                product.write(vals)
                result['updated'] += 1
            else:
                result['unchanged'] += 1
        if self.reseller_pricelist_id and reseller_prices:
            self._sync_reseller_pricelist(reseller_prices)
        return result

    def _sync_reseller_pricelist(self, reseller_prices):
        """Store the reseller prices as fixed prices of the reseller pricelist.

        :param reseller_prices: {product.template: reseller price}
        """
        Item = self.env['product.pricelist.item'].with_context(active_test=False)
        templates = self.env['product.template'].browse([template.id for template in reseller_prices])
        items = Item.search([
            ('pricelist_id', '=', self.reseller_pricelist_id.id),
            ('applied_on', '=', '1_product'),
            ('product_tmpl_id', 'in', templates.ids),
            ('min_quantity', '<=', 1),
            ('date_start', '=', False),
            ('date_end', '=', False),
        ])
        item_by_template = {item.product_tmpl_id: item for item in items}
        to_create = []
        for template, price in reseller_prices.items():
            item = item_by_template.get(template)
            if not item:
                to_create.append({
                    'applied_on': '1_product',
                    'product_tmpl_id': template.id,
                    'compute_price': 'fixed',
                    'fixed_price': price,
                })
            elif item.compute_price != 'fixed' or float_compare(item.fixed_price, price, precision_digits=6):
                item.write({'compute_price': 'fixed', 'fixed_price': price})
        if to_create:
            self.reseller_pricelist_id.item_ids = [Command.create(vals) for vals in to_create]
