import re

from odoo import api, fields, models
from odoo.exceptions import UserError
from odoo.tools import float_compare, format_amount

SUBSTITUTE_SEPARATOR_RE = re.compile(r'[,;|\n\r]+')

# Context keys used to describe the origin of a change in the change log.
LOG_SOURCE_KEY = 'mina_part_log_source'
LOG_IMPORT_KEY = 'mina_part_import_id'
LOG_RULE_KEY = 'mina_part_rule_id'
NO_LOG_KEY = 'mina_part_no_log'
SKIP_SUBSTITUTE_KEY = 'mina_part_skip_substitute'
SKIP_VENDOR_SYNC_KEY = 'mina_part_skip_vendor_sync'
# Set by the price rules: the only way to change the selling prices of a spare part.
ALLOW_PRICE_WRITE_KEY = 'mina_part_price_write'
# Selling prices of a spare part, computed by its price rule and locked for manual edits.
LOCKED_PRICE_FIELDS = ('list_price', 'part_reseller_price')

# Fields copied to the vendor price (product.supplierinfo) of the principal.
VENDOR_SYNC_FIELDS = {'part_list_price', 'part_currency_id', 'part_lead_time', 'categ_id', 'default_code'}
# Depth limit when following substitutes of substitutes (A -> B -> C).
SUBSTITUTE_MAX_DEPTH = 10
# Initial values written in the change log when a part is created (empty values are left out).
CREATE_LOG_FIELDS = [
    'name', 'categ_id', 'part_list_price', 'part_currency_id', 'part_lead_time', 'part_stock_jpn',
    'part_availability', 'substitute_part_numbers', 'weight', 'uom_id', 'part_application', 'part_remarks',
    'list_price', 'part_reseller_price', 'standard_price',
]
CREATE_LOG_SEPARATOR = ' | '


def split_part_numbers(value):
    """Split a free-text list of part numbers ("A, B; C") into unique codes."""
    codes = []
    for code in SUBSTITUTE_SEPARATOR_RE.split(value or ''):
        code = code.strip()
        if code and code not in codes:
            codes.append(code)
    return codes


class ProductTemplate(models.Model):
    _inherit = 'product.template'

    part_type = fields.Selection(related='categ_id.part_type', store=True, string="Jenis Part")
    part_list_price = fields.Float(
        string="Harga Principal",
        digits=(16, 4),
        help="Harga list dari principal (Price Yen pada file import). "
             "Menjadi dasar perhitungan harga reseller dan end user.",
    )
    part_currency_id = fields.Many2one('res.currency', string="Mata Uang Harga Principal")
    part_reseller_price = fields.Monetary(
        string="Harga Reseller", currency_field='currency_id', readonly=True, copy=False,
        help="Dihitung dari Harga Principal x Kurs x formula reseller pada Setting Harga.",
    )
    part_lead_time = fields.Integer(string="Lead Time (Hari)", help="Waktu indent dari principal.")
    part_stock_jpn = fields.Float(string="Stock JPN", digits=(16, 2), help="Stok di principal (Jepang) saat file diupload.")
    part_remarks = fields.Char(string="Remarks")
    part_availability = fields.Selection(
        [('available', "Tersedia"), ('substituted', "Diganti Substitusi"), ('discontinued', "Discontinued")],
        string="Product Availability",
    )
    part_application = fields.Char(string="Aplikasi / Model Unit")
    substitute_part_numbers = fields.Char(
        string="No. Part Substitusi",
        help="Nomor part pengganti, pisahkan dengan koma atau titik koma bila lebih dari satu.",
    )
    substitute_product_ids = fields.Many2many(
        'product.template', 'mina_part_substitute_rel', 'product_tmpl_id', 'substitute_tmpl_id',
        string="Part Substitusi",
        help="Produk substitusi yang ditemukan di master data berdasarkan No. Part Substitusi.",
    )
    substitute_for_ids = fields.Many2many(
        'product.template', 'mina_part_substitute_rel', 'substitute_tmpl_id', 'product_tmpl_id',
        string="Substitusi Untuk", readonly=True,
    )
    part_price_rule_id = fields.Many2one(
        'mina.part.price.rule', string="Setting Harga", compute='_compute_part_price_rule_id',
    )
    part_price_date = fields.Datetime(string="Harga Terakhir Berubah", readonly=True, copy=False)
    part_import_id = fields.Many2one(
        'mina.part.import', string="Import Terakhir", readonly=True, copy=False, ondelete='set null',
    )
    part_change_log_ids = fields.One2many('mina.part.change.log', 'product_tmpl_id', string="Log Perubahan")
    part_change_log_count = fields.Integer(compute='_compute_part_change_log_count')
    mina_stock_quant_ids = fields.Many2many(
        'stock.quant', string="Stok per Lokasi", compute='_compute_mina_stock_quant_ids',
        help="Stok Mina di setiap lokasi gudang.",
    )
    mina_incoming_move_line_ids = fields.Many2many(
        'stock.move.line', string="Riwayat Barang Masuk", compute='_compute_mina_incoming_move_line_ids',
        help="Barang yang masuk ke lokasi gudang: penerimaan dari vendor, penyesuaian stok, retur.",
    )

    @api.depends('categ_id', 'part_type')
    @api.depends_context('company')
    def _compute_part_price_rule_id(self):
        rules = self.env['mina.part.price.rule']._get_rules_by_category()
        for template in self:
            template.part_price_rule_id = template._mina_find_price_rule(rules)

    def _compute_mina_stock_quant_ids(self):
        Quant = self.env['stock.quant']
        for template in self:
            variants = template._origin.product_variant_ids
            template.mina_stock_quant_ids = Quant.search([
                ('product_id', 'in', variants.ids), ('location_id.usage', '=', 'internal'),
            ]) if variants and template.is_storable else Quant

    def _compute_mina_incoming_move_line_ids(self):
        MoveLine = self.env['stock.move.line']
        for template in self:
            variants = template._origin.product_variant_ids
            template.mina_incoming_move_line_ids = MoveLine.search(
                MoveLine._mina_incoming_domain() + [('product_id', 'in', variants.ids)],
            ) if variants and template.is_storable else MoveLine

    def _compute_part_change_log_count(self):
        counts = dict(self.env['mina.part.change.log']._read_group(
            [('product_tmpl_id', 'in', self.ids)], ['product_tmpl_id'], ['__count'],
        ))
        for template in self:
            template.part_change_log_count = counts.get(template, 0)

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    @api.model_create_multi
    def create(self, vals_list):
        templates = super().create(vals_list)
        parts = templates.filtered('part_type')
        if parts and not self.env.context.get(NO_LOG_KEY):
            self.env['mina.part.change.log'].sudo().create([
                template._mina_prepare_log_vals('create', new_value=template._mina_initial_values_summary())
                for template in parts
            ])
        if parts and not self.env.context.get(SKIP_SUBSTITUTE_KEY):
            parts.filtered('substitute_part_numbers')._mina_resolve_substitutes()
        if parts and not self.env.context.get(SKIP_VENDOR_SYNC_KEY):
            parts._mina_sync_vendor_prices()
        return templates

    def write(self, vals):
        self._mina_check_locked_prices(vals)
        fnames = [fname for fname in self._mina_logged_fields() if fname in vals]
        if not fnames or self.env.context.get(NO_LOG_KEY):
            res = super().write(vals)
        else:
            # Only spare parts are logged; when the category changes a product may become one.
            watched = self if 'categ_id' in vals else self.filtered('part_type')
            before = {
                template.id: {fname: template._mina_log_value(fname) for fname in fnames}
                for template in watched
            }
            res = super().write(vals)
            log_vals = []
            for template in watched:
                if not template.part_type:
                    continue
                for fname in fnames:
                    old_value = before[template.id][fname]
                    new_value = template._mina_log_value(fname)
                    if old_value != new_value:
                        log_vals.append(template._mina_prepare_log_vals(
                            'update', fname=fname, old_value=old_value, new_value=new_value,
                        ))
            if log_vals:
                self.env['mina.part.change.log'].sudo().create(log_vals)
        if 'substitute_part_numbers' in vals and not self.env.context.get(SKIP_SUBSTITUTE_KEY):
            self._mina_resolve_substitutes()
        if VENDOR_SYNC_FIELDS & vals.keys() and not self.env.context.get(SKIP_VENDOR_SYNC_KEY):
            self._mina_sync_vendor_prices()
        return res

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def action_mina_compute_part_price(self):
        templates = self.filtered('part_type')
        if not templates:
            raise UserError(self.env._("Produk yang dipilih bukan spare part."))
        missing = templates.filtered(lambda t: not t.part_price_rule_id)
        if missing:
            raise UserError(self.env._(
                "Belum ada Setting Harga untuk kategori: %s",
                missing.categ_id.mapped('display_name'),
            ))
        updated = 0
        for rule, products in templates.grouped('part_price_rule_id').items():
            updated += rule.with_context(**{LOG_SOURCE_KEY: 'rule'})._apply_prices(products)['updated']
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'type': 'success',
                'message': self.env._("Harga jual dihitung ulang untuk %s produk.", updated),
                'next': {'type': 'ir.actions.client', 'tag': 'soft_reload'},
            },
        }

    def action_mina_view_change_log(self):
        self.ensure_one()
        action = self.env['ir.actions.act_window']._for_xml_id('mina_spare_part.mina_part_change_log_action')
        action['domain'] = [('product_tmpl_id', '=', self.id)]
        action['context'] = {'default_product_tmpl_id': self.id}
        return action

    def action_mina_view_stock(self):
        self.ensure_one()
        return self.product_variant_ids.action_mina_view_stock()

    def action_mina_view_incoming(self):
        self.ensure_one()
        action = self.env['ir.actions.act_window']._for_xml_id('mina_spare_part.mina_incoming_move_line_action')
        action['domain'] = self.env['stock.move.line']._mina_incoming_domain() + [
            ('product_id', 'in', self.product_variant_ids.ids),
        ]
        action['context'] = {}
        action['name'] = self.env._("Barang Masuk: %s", self.display_name)
        return action

    # ------------------------------------------------------------------
    # Business methods
    # ------------------------------------------------------------------

    def _mina_find_price_rule(self, rules_by_categ):
        """Return the price rule of the closest category (itself, then its parents)."""
        self.ensure_one()
        if not self.part_type or not self.categ_id:
            return self.env['mina.part.price.rule']
        categ_ids = [int(categ_id) for categ_id in (self.categ_id.parent_path or '').split('/') if categ_id]
        for categ_id in reversed(categ_ids or [self.categ_id.id]):
            if categ_id in rules_by_categ:
                return rules_by_categ[categ_id]
        return self.env['mina.part.price.rule']

    @api.model
    def _mina_logged_fields(self):
        """Fields whose changes are recorded in the spare part change log.

        Stock JPN is refreshed by every upload and is not logged to keep the log readable.
        """
        return [
            'default_code', 'name', 'categ_id', 'uom_id', 'part_list_price', 'part_currency_id',
            'part_application', 'substitute_part_numbers', 'part_lead_time', 'weight', 'part_remarks',
            'part_availability', 'part_reseller_price', 'list_price', 'standard_price', 'active',
        ]

    def _mina_log_value(self, fname):
        """Human readable value of a field, used to detect and display changes."""
        self.ensure_one()
        field = self._fields[fname]
        value = self[fname]
        if field.type == 'many2one':
            return value.display_name or ''
        if field.type in ('one2many', 'many2many'):
            return ', '.join(value.mapped('display_name'))
        if field.type in ('float', 'monetary'):
            return f"{value or 0.0:,.6f}".rstrip('0').rstrip('.')
        if field.type == 'integer':
            return str(value or 0)
        if field.type == 'boolean':
            return self.env._("Ya") if value else self.env._("Tidak")
        if field.type == 'selection':
            return dict(field._description_selection(self.env)).get(value, '')
        return value or ''

    def _mina_check_locked_prices(self, vals):
        """The selling prices of a spare part come from its price rule (import, rate update,
        recompute): a manual change would be overwritten by the next recompute."""
        if self.env.context.get(ALLOW_PRICE_WRITE_KEY):
            return
        fnames = [fname for fname in LOCKED_PRICE_FIELDS if fname in vals]
        if not fnames:
            return
        changed = self.filtered(lambda template: template.part_type and any(
            float_compare(template[fname], vals[fname] or 0.0, precision_digits=6) for fname in fnames
        ))
        if changed:
            raise UserError(self.env._(
                "Harga jual spare part dikunci dan tidak bisa diubah manual (%(parts)s).\n"
                "Harga dihitung dari Setting Harga: ubah kurs, margin atau formula di Setting Harga, "
                "lalu hitung ulang harga.",
                parts=", ".join(changed[:5].mapped(lambda t: t.default_code or t.name))
                + (", ..." if len(changed) > 5 else ""),
            ))

    def _mina_initial_values_summary(self):
        """Data of a new part for the change log, e.g. "Harga Principal: 10 | Mata Uang: USD | ..."."""
        self.ensure_one()
        return CREATE_LOG_SEPARATOR.join(
            f"{self._fields[fname]._description_string(self.env)}: {self._mina_log_value(fname)}"
            for fname in CREATE_LOG_FIELDS
            if self[fname]
        )

    def _mina_prepare_log_vals(self, change_type, fname=None, old_value='', new_value=''):
        self.ensure_one()
        context = self.env.context
        if fname:
            field_label = self._fields[fname]._description_string(self.env)
        else:
            field_label = self.env._("Produk baru")
        return {
            'product_tmpl_id': self.id,
            'default_code': self.default_code,
            'change_type': change_type,
            'field_name': fname or False,
            'field_label': field_label,
            'old_value': old_value,
            'new_value': new_value,
            'source': context.get(LOG_SOURCE_KEY) or 'manual',
            'import_id': context.get(LOG_IMPORT_KEY) or False,
            'rule_id': context.get(LOG_RULE_KEY) or False,
            'user_id': self.env.uid,
        }

    def _mina_resolve_substitutes(self):
        """Link substitute products from the free-text substitute part numbers.

        :return: dict {template id: [part numbers not found in master data]}
        """
        codes_by_template = {template: split_part_numbers(template.substitute_part_numbers) for template in self}
        all_codes = {code for codes in codes_by_template.values() for code in codes}
        by_code = {}
        if all_codes:
            candidates = self.with_context(active_test=False).search_fetch(
                [('default_code', 'in', list(all_codes)), ('part_type', '!=', False)],
                ['default_code', 'part_type'],
            )
            for candidate in candidates:
                by_code[candidate.default_code] = by_code.get(candidate.default_code, self.browse()) | candidate
        missing = {}
        for template, codes in codes_by_template.items():
            substitutes = self.browse()
            for code in codes:
                matches = by_code.get(code, self.browse()) - template
                if not matches:
                    missing.setdefault(template.id, []).append(code)
                    continue
                # Prefer a part of the same type when a part number exists in both catalogs.
                substitutes |= matches.filtered(lambda p: p.part_type == template.part_type)[:1] or matches[:1]
            if substitutes != template.substitute_product_ids:
                template.substitute_product_ids = substitutes
        return missing

    def _mina_sync_vendor_prices(self):
        """Store the principal price as vendor price of the principal vendor of the price rule,
        so purchase orders to the principal use the principal price, currency and lead time.

        :return: number of vendor prices created or updated
        """
        parts = self.filtered('part_type')
        if not parts:
            return 0
        rules = self.env['mina.part.price.rule']._get_rules_by_category()
        count = 0
        for rule, templates in parts.grouped(lambda t: t._mina_find_price_rule(rules)).items():
            if rule.vendor_id:
                count += rule._sync_vendor_prices(templates)
        return count

    def _mina_can_update_cost(self):
        """With Average Cost (AVCO) or FIFO the cost of a part in stock comes from its receipts:
        the price formula only sets the initial cost of a part without stock.
        """
        self.ensure_one()
        return self.cost_method == 'standard' or not self._mina_has_stock()

    def _mina_has_stock(self):
        self.ensure_one()
        # Spare Part managers may have no Inventory rights: the stock check is internal to the pricing.
        return self.is_storable and self.sudo().qty_available > 0

    def _mina_get_substitutes(self):
        """Substitutes of the part, including the substitutes of the substitutes (A -> B -> C).

        :return: (product.template recordset ordered closest first, {template id: level})
        """
        self.ensure_one()
        substitutes = self.browse()
        levels = {}
        seen = self
        frontier = self.substitute_product_ids
        level = 1
        while frontier and level <= SUBSTITUTE_MAX_DEPTH:
            frontier -= seen
            for template in frontier:
                levels[template.id] = level
            substitutes |= frontier
            seen |= frontier
            frontier = frontier.substitute_product_ids
            level += 1
        return substitutes, levels

    def _mina_get_origins(self):
        """Parts replaced by this part, up the chain (A <- B <- C, several parts may be replaced by one).

        :return: (product.template recordset ordered earliest first, {template id: negative level})
        """
        self.ensure_one()
        origins = self.browse()
        levels = {}
        seen = self
        frontier = self.substitute_for_ids
        level = -1
        while frontier and -level <= SUBSTITUTE_MAX_DEPTH:
            frontier -= seen
            for template in frontier:
                levels[template.id] = level
            origins |= frontier
            seen |= frontier
            frontier = frontier.substitute_for_ids
            level -= 1
        return origins.sorted(lambda template: (levels[template.id], template.id)), levels

    def _mina_get_earliest_parts(self):
        """Earliest produced parts of the substitution chain of this part (not replacing any other part)."""
        self.ensure_one()
        return self._mina_get_origins()[0].filtered(lambda template: not template.substitute_for_ids)

    @api.model
    def _mina_get_recommended_substitute(self, substitutes):
        """Closest substitute that can be sold: with a sales price, available first, never discontinued
        unless nothing else has a price."""
        priced = substitutes.filtered(lambda template: template.sale_ok and template.list_price > 0)
        return (
            priced.filtered(lambda template: template.part_availability == 'available')
            or priced.filtered(lambda template: template.part_availability != 'discontinued')
            or priced
        )[:1]

    def _mina_substitute_warning(self, check_stock=False, price_func=None, currency=None):
        """Warning shown when the part is put on an order while it should be replaced.

        :param check_stock: also warn when the part is out of stock and has substitutes (sales)
        :param price_func: function(product.template) -> selling price for the order (sales)
        :param currency: currency of the prices returned by ``price_func``
        :return: message or False
        """
        self.ensure_one()
        if not self.part_type:
            return False
        substitutes, levels = self._mina_get_substitutes()
        if self.part_availability == 'substituted':
            reason = self.env._("Part %s berstatus Diganti Substitusi.", self.display_name)
        elif self.part_availability == 'discontinued':
            reason = self.env._("Part %s sudah Discontinued.", self.display_name)
        elif check_stock and substitutes and self.is_storable and self.qty_available <= 0:
            reason = self.env._("Stok part %s kosong.", self.display_name)
        else:
            return False
        lines = [reason]
        earliest = self._mina_get_earliest_parts()
        if earliest:
            lines.append(self.env._("Part paling awal di rangkaian ini: %s", ", ".join(earliest.mapped('display_name'))))
        if not substitutes:
            lines.append(self.env._("Tidak ada part substitusi di master data."))
            return "\n".join(lines)

        def price_text(template):
            if not price_func:
                return ""
            price = price_func(template)
            if not price:
                return self.env._("harga jual belum ada") + " | "
            return self.env._("harga %s", format_amount(self.env, price, currency or template.currency_id)) + " | "

        lines.append(self.env._("Part pengganti:"))
        lines += [
            self.env._(
                "- Level %(level)s: %(part)s, %(price)sstok %(qty)s | stock JPN %(jpn)s | lead time %(lead)s hari | %(availability)s",
                level=levels[substitute.id],
                part=substitute.display_name,
                price=price_text(substitute),
                qty=f"{substitute.qty_available:g}",
                jpn=f"{substitute.part_stock_jpn:g}",
                lead=substitute.part_lead_time,
                availability=substitute._mina_log_value('part_availability') or "-",
            )
            for substitute in substitutes
        ]
        recommended = self._mina_get_recommended_substitute(substitutes)
        if recommended:
            lines.append(self.env._("Rekomendasi pengganti: %s", recommended.display_name))
        else:
            lines.append(self.env._("Belum ada part pengganti yang punya harga jual."))
        lines.append(self.env._("Gunakan tombol Substitusi pada baris order untuk mengganti part."))
        return "\n".join(lines)
