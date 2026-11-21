import io
from unittest.mock import patch

import openpyxl

from odoo.exceptions import UserError, ValidationError
from odoo.fields import Command
from odoo.tools import BinaryBytes
from odoo.tests import TransactionCase, tagged

from odoo.addons.mina_spare_part.models import part_import as part_import_module
from odoo.addons.mina_spare_part.models import part_price_rule as part_price_rule_module
from odoo.addons.mina_spare_part.models.part_import import parse_number, parse_price


def make_xlsx(rows):
    workbook = openpyxl.Workbook()
    for row in rows:
        workbook.active.append(row)
    content = io.BytesIO()
    workbook.save(content)
    return BinaryBytes(content.getvalue())


@tagged('post_install', '-at_install')
class TestSparePart(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.usd = cls.env.ref('base.USD')
        cls.usd.active = True
        if cls.company.currency_id == cls.usd:
            cls.company.currency_id = cls.env.ref('base.IDR')
        cls.engine_categ = cls.env.ref('mina_spare_part.product_category_engine')
        cls.airend_categ = cls.env.ref('mina_spare_part.product_category_airend')
        cls.engine_rule = cls.env.ref('mina_spare_part.part_price_rule_engine')
        cls.airend_rule = cls.env.ref('mina_spare_part.part_price_rule_airend')
        (cls.engine_rule | cls.airend_rule).write({
            'company_id': cls.company.id,
            'currency_id': cls.usd.id,
            'rate_source': 'manual',
            'manual_rate': 16000.0,
            'freight_pct': 10.0,
            'duty_pct': 5.0,
            'margin_pct': 25.0,
            'reseller_margin_pct': 15.0,
            'price_rounding': 100.0,
            'rounding_method': 'UP',
        })
        cls.header = ["Part Number", "Description", "Substitute Part", "Price"]

    def _import(self, rows, part_type='engine', file_name='parts.xlsx', file=None):
        part_import = self.env['mina.part.import'].create({
            'part_type': part_type,
            'categ_id': (self.engine_categ if part_type == 'engine' else self.airend_categ).id,
            'file': file or make_xlsx(rows),
            'file_name': file_name,
        })
        part_import.action_import()
        return part_import

    def _part(self, code, part_type='engine'):
        return self.env['product.template'].search([('default_code', '=', code), ('part_type', '=', part_type)])

    def test_parse_price(self):
        self.assertEqual(parse_price("1,234.50"), 1234.5)
        self.assertEqual(parse_price("1.234,50"), 1234.5)
        self.assertEqual(parse_price("12,5"), 12.5)
        self.assertEqual(parse_price("1,234"), 1234.0)
        self.assertEqual(parse_price("USD 99.90"), 99.9)
        self.assertEqual(parse_price(10), 10.0)
        self.assertIsNone(parse_price("-"))
        self.assertIsNone(parse_price(None))

    def test_import_create_with_substitute_and_price(self):
        part_import = self._import([
            ["Daftar Harga Part Engine"],
            [],
            self.header,
            ["P-001", "Oil Filter", "P-002", 10],
            ["P-002", "Oil Filter New", "", "20.00"],
            ["", "", "", ""],
        ])
        self.assertEqual(part_import.state, 'done')
        self.assertEqual(part_import.created_count, 2)
        self.assertEqual(part_import.error_count, 0)
        old, new = self._part('P-001'), self._part('P-002')
        self.assertEqual(old.categ_id, self.engine_categ)
        self.assertEqual(old.part_currency_id, self.usd)
        self.assertEqual(old.substitute_product_ids, new)
        self.assertEqual(new.substitute_for_ids, old)
        # 10 * 16000 * 1.10 * 1.05 = 184800 ; * 1.25 = 231000
        self.assertAlmostEqual(old.standard_price, 184800.0)
        self.assertAlmostEqual(old.list_price, 231000.0)
        # 184800 * 1.15 = 212520 -> rounded up to 212600
        self.assertAlmostEqual(old.part_reseller_price, 212600.0)
        self.assertEqual(part_import.priced_count, 2)
        create_logs = part_import.change_log_ids.filtered(lambda log: log.change_type == 'create')
        self.assertEqual(len(create_logs), 2)
        # The creation log holds the initial data of the part (empty values are left out).
        old_log = create_logs.filtered(lambda log: log.product_tmpl_id == old)
        details = old_log.new_value.split(' | ')
        self.assertIn(f"{old._fields['name']._description_string(self.env)}: Oil Filter", details)
        self.assertIn("Mata Uang Harga Principal: USD", details)
        self.assertIn("No. Part Substitusi: P-002", details)
        self.assertTrue(any(item.startswith("Harga Principal") and item.endswith(": 10") for item in details), details)
        self.assertFalse(any(item.startswith("Remarks") for item in details), details)
        self.assertEqual(old_log.source, 'import')

    def test_import_update_only_changes_and_logs(self):
        self._import([self.header, ["P-001", "Oil Filter", "", 10], ["P-002", "Fuel Filter", "", 20]])
        second = self._import([
            self.header,
            ["P-001", "Oil Filter Element", "P-003", 12],
            ["P-002", "Fuel Filter", "", 20],
        ])
        self.assertEqual(second.created_count, 0)
        self.assertEqual(second.updated_count, 1)
        self.assertEqual(second.unchanged_count, 1)
        part = self._part('P-001')
        self.assertEqual(part.name, "Oil Filter Element")
        logs = second.change_log_ids
        by_field = {log.field_name: log for log in logs}
        self.assertEqual(set(by_field), {
            'name', 'part_list_price', 'substitute_part_numbers', 'list_price', 'standard_price', 'part_reseller_price',
        })
        self.assertEqual(by_field['part_list_price'].old_value, '10')
        self.assertEqual(by_field['part_list_price'].new_value, '12')
        self.assertEqual(by_field['name'].old_value, 'Oil Filter')
        self.assertTrue(all(log.source == 'import' for log in logs))
        line = second.line_ids.filtered(lambda line: line.default_code == 'P-001')
        self.assertIn("P-003", line.message, "Unknown substitute must be reported")
        self.assertFalse(self._part('P-002').part_change_log_ids.filtered(lambda log: log.import_id == second))

        # The substitute arrives later from another file: the older part gets linked.
        self._import([self.header, ["P-003", "Oil Filter Element v2", "", 13]])
        self.assertEqual(part.substitute_product_ids, self._part('P-003'))

    def test_engine_and_airend_are_separated(self):
        self._import([self.header, ["X-100", "Engine Part", "", 5]])
        airend_import = self._import([self.header, ["X-100", "Air End Part", "", 7]], part_type='airend')
        self.assertEqual(airend_import.created_count, 1)
        self.assertEqual(self._part('X-100').name, "Engine Part")
        airend_part = self._part('X-100', 'airend')
        self.assertEqual(airend_part.categ_id, self.airend_categ)
        self.assertEqual(airend_part.part_price_rule_id, self.airend_rule)

    def test_rows_with_errors(self):
        part_import = self._import([
            self.header,
            ["E-1", "Bad price", "", "1-2"],
            ["", "No code", "", 1],
            ["E-2", "Good", "", 1],
            ["E-2", "Duplicate", "", 2],
        ])
        statuses = dict(zip(part_import.line_ids.mapped('row_number'), part_import.line_ids.mapped('status')))
        self.assertEqual(statuses, {2: 'error', 3: 'error', 4: 'created', 5: 'skipped'})
        self.assertEqual(self._part('E-2').part_list_price, 1)

    def test_csv_semicolon(self):
        content = "Part No;Nama Part;Pengganti;Harga\nC-1;Seal Kit;C-2, C-3;1.234,50\n".encode()
        part_import = self._import(None, file_name='airend.csv', file=BinaryBytes(content), part_type='airend')
        part = self._part('C-1', 'airend')
        self.assertEqual(part_import.created_count, 1)
        self.assertEqual(part.part_list_price, 1234.5)
        self.assertEqual(part.substitute_part_numbers, "C-2, C-3")

    def test_rate_update_recomputes_prices(self):
        self._import([self.header, ["R-1", "Bearing", "", 10]])
        self._import([self.header, ["R-2", "Rotor Bearing", "", 10]], part_type='airend')
        wizard = self.env['mina.part.rate.update'].create({
            'rule_id': self.engine_rule.id,
            'new_rate': 17000.0,
        })
        self.assertEqual(wizard.rule_ids, self.engine_rule | self.airend_rule)
        wizard.action_confirm()
        # 10 * 17000 * 1.155 = 196350 ; * 1.25 = 245437.5 -> rounded up to 245500
        for part in self._part('R-1') | self._part('R-2', 'airend'):
            self.assertAlmostEqual(part.list_price, 245500.0)
            log = part.part_change_log_ids.filtered(lambda log: log.field_name == 'list_price' and log.source == 'rate')
            self.assertEqual((log.old_value, log.new_value), ('231,000', '245,500'))
            self.assertEqual(log.rule_id, self.engine_rule if part.part_type == 'engine' else self.airend_rule)
        history = self.engine_rule.rate_history_ids
        self.assertEqual((history.old_rate, history.new_rate), (16000.0, 17000.0))

    def test_import_without_rate_saves_parts_unpriced(self):
        """A missing rate does not stop the import: the parts are saved without selling price,
        which is computed by the rate update."""
        self.engine_rule.manual_rate = 0.0
        part_import = self._import([self.header, ["N-1", "Bearing", "", 10], ["N-2", "Seal", "", 20]])
        self._import([self.header, ["N-3", "Rotor", "", 10]], part_type='airend')
        self.assertEqual(part_import.state, 'done')
        self.assertEqual((part_import.created_count, part_import.priced_count, part_import.no_rate_count), (2, 0, 2))
        self.assertIn("2 part belum dihitung harga jualnya: Kurs manual pada setting harga", part_import.summary)
        self.assertIn("Update Kurs", part_import.summary)
        self.assertEqual(
            part_import.line_ids.mapped('message'),
            ["Harga jual belum dihitung: kurs belum diisi."] * 2,
        )
        bearing = self._part('N-1')
        self.assertEqual((bearing.part_list_price, bearing.list_price), (10.0, 0.0))
        # The Air End rule has a rate: its parts are priced.
        self.assertAlmostEqual(self._part('N-3', 'airend').list_price, 231000.0)

        self.env['mina.part.rate.update'].create({
            'rule_id': self.engine_rule.id, 'new_rate': 16000.0, 'apply_same_currency': False,
        }).action_confirm()
        self.assertAlmostEqual(bearing.list_price, 231000.0)
        self.assertAlmostEqual(self._part('N-2').list_price, 462000.0)

    def test_rate_update_by_spare_part_manager(self):
        """A Spare Part Administrator without other rights can update the rate, also when the
        prices are recomputed by the background job (which runs as that user)."""
        pricelist = self.env['product.pricelist'].create({
            'name': "Reseller", 'currency_id': self.company.currency_id.id,
        })
        self.engine_rule.reseller_pricelist_id = pricelist
        self._import([self.header, ["M-1", "Bearing", "", 10], ["M-2", "Seal", "", 20]])
        self._import([self.header, ["M-3", "Rotor", "", 10]], part_type='airend')
        manager = self.env['res.users'].create({
            'name': "Spare Part Manager",
            'login': 'mina_part_manager',
            'company_id': self.company.id,
            'company_ids': [Command.set(self.company.ids)],
            'group_ids': [Command.set([
                self.env.ref('base.group_user').id,
                self.env.ref('mina_spare_part.mina_spare_part_group_manager').id,
            ])],
        })
        self.engine_rule.rate_source = 'odoo'
        Wizard = self.env['mina.part.rate.update'].with_user(manager)
        Wizard.create({'rule_id': self.engine_rule.id, 'new_rate': 17000.0}).action_confirm()
        for code, part_type in (('M-1', 'engine'), ('M-3', 'airend')):
            self.assertAlmostEqual(self._part(code, part_type).list_price, 245500.0)
        self.assertEqual(self.engine_rule.rate_history_ids.products_updated, 2)
        self.assertEqual(self.airend_rule.rate_history_ids.products_updated, 1)
        self.assertEqual(self.engine_rule.rate_history_ids.user_id, manager)

        with patch.object(part_price_rule_module, 'RECOMPUTE_SYNC_LIMIT', 1):
            Wizard.create({'rule_id': self.engine_rule.id, 'new_rate': 18000.0}).action_confirm()
        self.assertEqual(self.engine_rule.recompute_state, 'pending')
        self.assertEqual(self.engine_rule.recompute_user_id, manager)
        with self._commit_patch():
            self.env['mina.part.price.rule']._cron_recompute_prices()
        self.assertEqual(self.engine_rule.recompute_state, 'done')
        latest = self.engine_rule.rate_history_ids.sorted('id')[-1:]
        self.assertEqual((latest.new_rate, latest.products_updated), (18000.0, 2))
        # 10 * 18000 * 1.155 = 207900 ; * 1.25 = 259875 -> 259900
        for code, part_type in (('M-1', 'engine'), ('M-3', 'airend')):
            self.assertAlmostEqual(self._part(code, part_type).list_price, 259900.0)
        self.assertEqual(len(pricelist.item_ids), 2)

    def test_odoo_rate_source(self):
        self.engine_rule.rate_source = 'odoo'
        self.env['mina.part.rate.update'].create({
            'rule_id': self.engine_rule.id,
            'new_rate': 15500.0,
            'apply_same_currency': False,
        }).action_confirm()
        self.assertAlmostEqual(self.engine_rule.current_rate, 15500.0, places=2)
        rate = self.env['res.currency.rate'].search([('currency_id', '=', self.usd.id), ('name', '=', self.engine_rule.rate_history_ids.date)])
        self.assertAlmostEqual(rate.inverse_company_rate, 15500.0, places=2)

    def test_manual_edit_is_logged(self):
        self._import([self.header, ["M-1", "Gasket", "", 3]])
        part = self._part('M-1')
        part.substitute_part_numbers = "M-1, M-9"
        log = part.part_change_log_ids.filtered(lambda log: log.field_name == 'substitute_part_numbers')
        self.assertEqual(log.source, 'manual')
        self.assertFalse(part.substitute_product_ids, "A part is never its own substitute")

    def test_invalid_formula(self):
        with self.assertRaises(ValidationError):
            self.engine_rule.sale_formula = "cost * unknown_variable"
        self.engine_rule.sale_formula = "max(cost * (1 + margin / 100), cost + 5000)"
        # cost = 18480 ; max(23100, 23480) = 23480, rounded up to 23500
        self.assertAlmostEqual(self.engine_rule._compute_prices(1, 16000)[2], 23500.0)

    def test_subcategory_uses_parent_rule(self):
        sub = self.env['product.category'].create({'name': "Filter", 'parent_id': self.engine_categ.id})
        self.assertEqual(sub.part_type, 'engine')
        product = self.env['product.template'].create({
            'name': "Filter X", 'default_code': 'F-X', 'categ_id': sub.id, 'part_list_price': 1,
            'part_currency_id': self.usd.id,
        })
        self.assertEqual(product.part_price_rule_id, self.engine_rule)
        product.action_mina_compute_part_price()
        self.assertAlmostEqual(product.list_price, 23100.0)

    def test_non_part_products_not_logged(self):
        product = self.env['product.template'].create({'name': "Office Chair"})
        product.list_price = 99
        self.assertFalse(product.part_change_log_ids)
        product.categ_id = self.engine_categ
        self.assertTrue(product.part_change_log_ids.filtered(lambda log: log.field_name == 'categ_id'))

    def test_download_template_and_views(self):
        part_import = self.env['mina.part.import'].with_context(default_part_type='airend').create({
            'part_type': 'airend',
            'file': make_xlsx([self.header]),
            'file_name': 'empty.xlsx',
        })
        self.assertEqual(part_import.categ_id, self.airend_categ)
        action = part_import.action_download_template()
        attachment = self.env['ir.attachment'].browse(int(action['url'].split('/')[3].split('?')[0]))
        rows = list(openpyxl.load_workbook(io.BytesIO(attachment.raw)).active.iter_rows(values_only=True))
        self.assertEqual(rows[0][0], "Part No.")
        for model in ('mina.part.import', 'mina.part.price.rule', 'mina.part.change.log', 'product.template'):
            self.env[model].get_views([(False, 'list'), (False, 'form'), (False, 'search')])


    def _commit_patch(self):
        """The background jobs commit between batches: not allowed in tests."""
        return patch.object(type(self.env['ir.cron']), '_commit_progress', lambda self, *args, **kwargs: float('inf'))

    def test_parse_number_decimal_comma(self):
        self.assertEqual(parse_number("0,37", decimal_comma=True), 0.37)
        self.assertEqual(parse_number("1.234,5", decimal_comma=True), 1234.5)
        self.assertEqual(parse_number("12", decimal_comma=True), 12.0)

    def test_principal_catalog_xlsx(self):
        """Catalog with an information sheet first and Japanese headers on the data sheet."""
        workbook = openpyxl.Workbook()
        info = workbook.active
        info.title = "Product Info"
        info.append(["Updated date"])
        info.append(["Part No.", "Substitution Parts Number", "Parts name", "List Price (YEN)"])
        info.append(["3214300800", None, "ELEMENT", 12400])
        data = workbook.create_sheet("Data")
        data.append(['部品番号', '代替部品番号', '部品名', 'LT', '在庫数', '海外定価', '重量', '備考4', 'コメント'])
        data.append(['F12994842240', None, 'V-BELT', 29, 5, 7650, 0.18, '在庫限定部品', None])
        data.append(['LZ1136501333', 'LZ1136501334', 'FILTER', 126, 0, 0, None, None, 'Substitution Parts Number'])
        data.append(['LZ1136501334', None, 'FILTER NEW', 42, 3, 2100, 0.5, None, None])
        data.append(['K1747264010', None, 'GASKET', 7, 0, 0, None, None, 'discontinued'])
        content = io.BytesIO()
        workbook.save(content)
        part_import = self._import(None, file=BinaryBytes(content.getvalue()), file_name='e_engine_catalog.xlsx')
        self.assertEqual(part_import.created_count, 4)
        belt = self._part('F12994842240')
        self.assertEqual(
            (belt.part_lead_time, belt.part_stock_jpn, belt.weight, belt.part_remarks, belt.part_availability),
            (29, 5.0, 0.18, '在庫限定部品', 'available'),
        )
        self.assertEqual(belt.part_list_price, 7650)
        old_filter = self._part('LZ1136501333')
        self.assertEqual(old_filter.part_availability, 'substituted')
        self.assertEqual(old_filter.substitute_product_ids, self._part('LZ1136501334'))
        self.assertFalse(old_filter.list_price, "No principal price: no sale price")
        self.assertEqual(self._part('K1747264010').part_availability, 'discontinued')

    def test_principal_csv_with_lost_header(self):
        """CSV export where the Japanese header became '????': read by column order."""
        content = (
            "????;??????;???;LT;???;????;??;??;????;Product Category\r\n"
            "006208UUC3;2147211200;MOTOR BEARING;126;0;0;;;Substitution Parts Number;PART\r\n"
            "006210VVC3;;MOTOR BEARING;95;20;12200;0,45;????;;PART\r\n"
            "GE36003400;GE36003401;DUMPER;7;0;0;;;discontinued;PART\r\n"
        ).encode()
        part_import = self._import(None, file=BinaryBytes(content), file_name='Principle Data.csv', part_type='airend')
        self.assertEqual(part_import.created_count, 3)
        self.assertIn("Remarks", part_import.summary)
        bearing = self._part('006210VVC3', 'airend')
        self.assertEqual(bearing.categ_id.name, 'PART')
        self.assertEqual(bearing.categ_id.parent_id, self.airend_categ)
        self.assertEqual(bearing.part_price_rule_id, self.airend_rule, "Sub-category uses the parent's price rule")
        self.assertEqual((bearing.weight, bearing.part_stock_jpn, bearing.part_lead_time), (0.45, 20.0, 95))
        self.assertFalse(bearing.part_remarks)
        self.assertEqual(self._part('006208UUC3', 'airend').substitute_part_numbers, '2147211200')

    def test_import_in_batches(self):
        rows = [self.header] + [[f"B-{i}", f"Part {i}", "", i] for i in range(1, 6)] + [["B-2", "Duplicate", "", 9]]
        part_import = self.env['mina.part.import'].create({
            'part_type': 'engine', 'categ_id': self.engine_categ.id,
            'file': make_xlsx(rows), 'file_name': 'big.xlsx',
        })
        with patch.object(part_import_module, 'SYNC_ROW_LIMIT', 2):
            action = part_import.action_import()
        self.assertEqual(action['tag'], 'display_notification')
        self.assertEqual((part_import.state, part_import.data_rows), ('queued', 6))
        with self._commit_patch(), patch.object(part_import_module, 'BATCH_SIZE', 2):
            self.env['mina.part.import']._cron_process_imports()
        self.assertEqual(part_import.state, 'done')
        self.assertEqual((part_import.created_count, part_import.skipped_count), (5, 1))
        self.assertEqual(self._part('B-2').name, "Part 2")
        self.assertEqual(part_import.progress, 100)

    def test_failed_import_can_resume(self):
        part_import = self.env['mina.part.import'].create({
            'part_type': 'engine', 'categ_id': self.engine_categ.id,
            'file': make_xlsx([self.header, ["F-1", "A", "", 1], ["F-2", "B", "", 2]]), 'file_name': 'f.xlsx',
        })
        part_import.write({'state': 'queued', 'user_id': self.env.uid})
        with self._commit_patch(), \
                patch.object(type(part_import), '_process_rows', side_effect=ValueError("boom")), \
                patch.object(type(self.env['ir.cron']), '_rollback_progress', lambda self: None):
            self.env['mina.part.import']._cron_process_imports()
        self.assertEqual(part_import.state, 'failed')
        self.assertIn("boom", part_import.summary)
        part_import.action_resume()
        with self._commit_patch():
            self.env['mina.part.import']._cron_process_imports()
        self.assertEqual((part_import.state, part_import.created_count), ('done', 2))

    def test_part_selling_price_locked(self):
        self._import([self.header, ["L-1", "Gasket", "", 10]])
        part = self._part('L-1')
        self.assertAlmostEqual(part.list_price, 231000.0)
        with self.assertRaises(UserError):
            part.list_price = 100.0
        with self.assertRaises(UserError):
            part.product_variant_id.lst_price = 100.0
        with self.assertRaises(UserError):
            part.part_reseller_price = 100.0
        # Writing the same value (e.g. a form saving all fields) and editing other fields is allowed.
        part.write({'list_price': 231000.0, 'description_sale': "Gasket kit"})
        # Products that are not spare parts keep a free sales price.
        other = self.env['product.template'].create({'name': "Service", 'list_price': 10.0})
        other.list_price = 20.0
        self.assertEqual(other.list_price, 20.0)
        # The price rule still updates the price.
        self.engine_rule.margin_pct = 50.0
        self.engine_rule.action_apply_prices()
        self.assertAlmostEqual(part.list_price, 277200.0)

    def test_change_reseller_pricelist_moves_prices(self):
        Pricelist = self.env['product.pricelist']
        old_pricelist = Pricelist.create({'name': "Umum", 'currency_id': self.company.currency_id.id})
        new_pricelist = Pricelist.create({'name': "Reseller", 'currency_id': self.company.currency_id.id})
        self.engine_rule.reseller_pricelist_id = old_pricelist
        self._import([self.header, ["H-1", "Ring", "", 10], ["H-2", "Seal", "", 20]])
        ring = self._part('H-1')
        # A price added by hand in the old pricelist is not managed by the rule and stays.
        manual = self.env['product.pricelist.item'].create({
            'pricelist_id': old_pricelist.id, 'applied_on': '0_product_variant',
            'product_id': self.env['product.product'].create({'name': "Other"}).id,
            'compute_price': 'fixed', 'fixed_price': 5.0,
        })
        self.assertEqual(len(old_pricelist.item_ids), 3)
        self.assertEqual(old_pricelist.item_ids.mina_part_rule_id, self.engine_rule)

        self.engine_rule.reseller_pricelist_id = new_pricelist
        self.assertEqual(old_pricelist.item_ids, manual)
        self.assertEqual(len(new_pricelist.item_ids), 2)
        item = new_pricelist.item_ids.filtered(lambda i: i.product_tmpl_id == ring)
        self.assertAlmostEqual(item.fixed_price, ring.part_reseller_price)
        self.assertEqual(item.mina_part_rule_id, self.engine_rule)

        self.engine_rule.reseller_pricelist_id = False
        self.assertFalse(new_pricelist.item_ids)

    def test_recompute_in_background_and_reseller_pricelist(self):
        pricelist = self.env['product.pricelist'].create({
            'name': "Reseller", 'currency_id': self.company.currency_id.id,
        })
        self.engine_rule.reseller_pricelist_id = pricelist
        self._import([self.header, ["G-1", "Ring", "", 10], ["G-2", "Seal", "", 20]])
        self.assertEqual(len(pricelist.item_ids), 2)
        self.engine_rule.margin_pct = 50.0
        self.engine_rule.reseller_margin_pct = 20.0
        with patch.object(part_price_rule_module, 'RECOMPUTE_SYNC_LIMIT', 1):
            self.engine_rule.action_apply_prices()
        self.assertEqual(self.engine_rule.recompute_state, 'pending')
        with self._commit_patch(), patch.object(part_price_rule_module, 'RECOMPUTE_BATCH_SIZE', 1):
            self.env['mina.part.price.rule']._cron_recompute_prices()
        self.assertEqual(self.engine_rule.recompute_state, 'done')
        ring = self._part('G-1')
        # cost 184800 ; end user * 1.5 = 277200 ; reseller * 1.2 = 221760 -> 221800
        self.assertAlmostEqual(ring.list_price, 277200.0)
        self.assertAlmostEqual(ring.part_reseller_price, 221800.0)
        item = pricelist.item_ids.filtered(lambda i: i.product_tmpl_id == ring)
        self.assertAlmostEqual(item.fixed_price, 221800.0)
        self.assertEqual(len(pricelist.item_ids), 2)
        self.assertEqual(
            ring.part_change_log_ids.filtered(lambda log: log.field_name == 'list_price' and log.source == 'rule').new_value,
            '277,200',
        )

    # ------------------------------------------------------------------
    # Integration with Inventory, Purchase and Sales
    # ------------------------------------------------------------------

    def test_imported_parts_track_inventory(self):
        self._import([self.header, ["S-1", "Gasket", "", 10]])
        part = self._part('S-1')
        self.assertTrue(part.is_storable)
        self.assertEqual(part.type, 'consu')

    def test_vendor_price_sync(self):
        vendor = self.env['res.partner'].create({'name': "Principal Jepang"})
        self.engine_rule.vendor_id = vendor
        header = self.header + ["Lead Time"]
        self._import([header, ["V-1", "Bearing", "", 10, 30], ["V-2", "No Price", "", "", 5]])
        bearing = self._part('V-1')
        info = bearing.seller_ids
        self.assertEqual(len(info), 1)
        self.assertEqual(info.partner_id, vendor)
        self.assertAlmostEqual(info.price, 10.0)
        self.assertEqual(info.currency_id, self.usd)
        self.assertEqual(info.delay, 30)
        self.assertEqual(info.product_code, 'V-1')
        self.assertFalse(self._part('V-2').seller_ids, "No vendor price without principal price")

        # Re-import with a new price updates the same vendor price
        self._import([header, ["V-1", "Bearing", "", 12.5, 45]])
        self.assertEqual(bearing.seller_ids, info)
        self.assertAlmostEqual(info.price, 12.5)
        self.assertEqual(info.delay, 45)

        # Manual change of the principal price
        bearing.part_list_price = 13.0
        self.assertAlmostEqual(info.price, 13.0)

        # Purchase order to the principal uses the vendor price
        order = self.env['purchase.order'].create({
            'partner_id': vendor.id,
            'currency_id': self.usd.id,
            'order_line': [Command.create({'product_id': bearing.product_variant_id.id, 'product_qty': 2})],
        })
        self.assertAlmostEqual(order.order_line.price_unit, 13.0)

    def test_vendor_price_sync_existing_parts_in_background(self):
        self._import([self.header, ["W-1", "Valve", "", 10], ["W-2", "Spring", "", 20]], part_type='airend')
        vendor = self.env['res.partner'].create({'name': "Principal Air End"})
        with patch.object(part_price_rule_module, 'RECOMPUTE_SYNC_LIMIT', 1):
            self.airend_rule.vendor_id = vendor
        self.assertEqual(self.airend_rule.vendor_sync_state, 'pending')
        with self._commit_patch(), patch.object(part_price_rule_module, 'RECOMPUTE_BATCH_SIZE', 1):
            self.env['mina.part.price.rule']._cron_recompute_prices()
        self.assertEqual(self.airend_rule.vendor_sync_state, 'done')
        spring = self._part('W-2', 'airend')
        self.assertEqual(spring.seller_ids.partner_id, vendor)
        self.assertAlmostEqual(spring.seller_ids.price, 20.0)
        # Running the sync again does not duplicate the vendor prices
        self.airend_rule.action_sync_vendor_prices()
        self.assertEqual(len(spring.seller_ids), 1)

    def _substitute_chain(self):
        self._import([
            self.header,
            ["C-OLD", "Filter Lama", "C-NEW", 10],
            ["C-NEW", "Filter Baru", "C-NEWER", 11],
            ["C-NEWER", "Filter Terbaru", "", 12],
        ])
        old, new, newer = self._part('C-OLD'), self._part('C-NEW'), self._part('C-NEWER')
        old.part_availability = 'substituted'
        return old, new, newer

    def test_sale_order_substitute(self):
        old, new, newer = self._substitute_chain()
        customer = self.env['res.partner'].create({'name': "Customer Part"})
        order = self.env['sale.order'].create({
            'partner_id': customer.id,
            'order_line': [Command.create({'product_id': old.product_variant_id.id, 'product_uom_qty': 3})],
        })
        line = order.order_line
        self.assertEqual(line.mina_substitute_count, 1)
        self.assertEqual(line.mina_part_availability, 'substituted')
        old.write({'part_stock_jpn': 7, 'part_lead_time': 21})
        self.assertEqual(line.mina_part_type, 'engine')
        self.assertEqual(line.mina_part_stock_jpn, 7)
        self.assertEqual(line.mina_part_lead_time, 21)
        warning = line._mina_onchange_product_substitute()
        self.assertIn('C-NEW', warning['warning']['message'])
        self.assertIn('C-NEWER', warning['warning']['message'])

        action = line.action_mina_open_substitute()
        wizard = self.env['mina.part.substitute.wizard'].browse(action['res_id'])
        self.assertEqual(wizard.line_ids.mapped('product_tmpl_id'), new | newer)
        self.assertEqual(wizard.line_ids.mapped('level'), [1, 2])
        self.assertEqual(wizard.line_ids.filtered('is_recommended').product_tmpl_id, new)
        wizard.line_ids.filtered(lambda l: l.product_tmpl_id == newer).action_select()

        self.assertEqual(line.product_id, newer.product_variant_id)
        self.assertEqual(line.product_uom_qty, 3)
        reference = self.env['sale.order.line'].create({
            'order_id': order.id, 'product_id': newer.product_variant_id.id, 'product_uom_qty': 1,
        })
        self.assertAlmostEqual(line.price_unit, reference.price_unit)
        self.assertIn('C-NEWER', order.message_ids[0].body)

        order.action_confirm()
        with self.assertRaises(UserError):
            line.action_mina_open_substitute()

    def test_sale_substitute_chain_earliest_and_recommended(self):
        # S-1 and S-1B were replaced by S-2, then S-3, then S-4: only S-4 has a principal price.
        self._import([
            self.header,
            ["S-1", "Filter Generasi 1", "S-2", ""],
            ["S-1B", "Filter Varian", "S-2", ""],
            ["S-2", "Filter Generasi 2", "S-3", ""],
            ["S-3", "Filter Generasi 3", "S-4", ""],
            ["S-4", "Filter Generasi 4", "", 12],
        ])
        g1, g1b, g2, g3, g4 = (self._part(code) for code in ('S-1', 'S-1B', 'S-2', 'S-3', 'S-4'))
        g2.part_availability = 'substituted'
        self.assertFalse(g3.list_price)
        self.assertTrue(g4.list_price)
        self.assertEqual(g2._mina_get_earliest_parts(), g1 | g1b)
        self.assertEqual(g4._mina_get_earliest_parts(), g1 | g1b, "Earliest parts of the whole chain")
        self.assertFalse(g1._mina_get_earliest_parts())

        customer = self.env['res.partner'].create({'name': "Customer Chain"})
        order = self.env['sale.order'].create({
            'partner_id': customer.id,
            'order_line': [Command.create({'product_id': g2.product_variant_id.id, 'product_uom_qty': 2})],
        })
        line = order.order_line
        self.assertEqual(line.mina_part_replacement_id, g4, "Closest substitute with a selling price")
        self.assertAlmostEqual(line.mina_part_replacement_price, g4.list_price)
        self.assertEqual(line.mina_part_earliest_ids, g1 | g1b)

        message = line._mina_onchange_product_substitute()['warning']['message']
        self.assertIn("Part paling awal di rangkaian ini: [S-1] Filter Generasi 1, [S-1B] Filter Varian", message)
        self.assertIn("- Level 1: [S-3] Filter Generasi 3, harga jual belum ada", message)
        self.assertIn("- Level 2: [S-4] Filter Generasi 4, harga ", message)
        self.assertIn("Rekomendasi pengganti: [S-4] Filter Generasi 4", message)

        action = line.action_mina_open_substitute()
        wizard = self.env['mina.part.substitute.wizard'].browse(action['res_id'])
        self.assertEqual(
            [(l.product_tmpl_id, l.role, l.level) for l in wizard.line_ids],
            [(g1, 'earliest', -1), (g1b, 'earliest', -1), (g3, 'substitute', 1), (g4, 'substitute', 2)],
        )
        recommended = wizard.line_ids.filtered('is_recommended')
        self.assertEqual(recommended.product_tmpl_id, g4)
        self.assertAlmostEqual(recommended.order_price, g4.list_price)
        with self.assertRaises(UserError):
            wizard.line_ids[0].action_select()
        recommended.action_select()
        self.assertEqual(line.product_id, g4.product_variant_id)
        self.assertEqual(line.product_uom_qty, 2)
        self.assertAlmostEqual(line.price_unit, g4.list_price)
        self.assertFalse(line.mina_part_replacement_id)
        self.assertEqual(line.mina_part_earliest_ids, g1 | g1b)

    def test_sale_warning_out_of_stock_with_substitute(self):
        old, new, newer = self._substitute_chain()
        # Available part, out of stock, with a substitute: warn on sales only
        self.assertTrue(new._mina_substitute_warning(check_stock=True))
        self.assertFalse(new._mina_substitute_warning(check_stock=False))
        # No substitute and available: no warning
        self.assertFalse(newer._mina_substitute_warning(check_stock=True))
        newer.part_availability = 'discontinued'
        self.assertIn("Tidak ada part substitusi", newer._mina_substitute_warning())

    def test_purchase_order_substitute(self):
        old, new, newer = self._substitute_chain()
        vendor = self.env['res.partner'].create({'name': "Principal"})
        self.engine_rule.vendor_id = vendor
        order = self.env['purchase.order'].create({
            'partner_id': vendor.id,
            'currency_id': self.usd.id,
            'order_line': [Command.create({'product_id': old.product_variant_id.id, 'product_qty': 4})],
        })
        line = order.order_line
        self.assertAlmostEqual(line.price_unit, 10.0)
        self.assertTrue(line._mina_onchange_product_substitute())
        action = line.action_mina_open_substitute()
        wizard = self.env['mina.part.substitute.wizard'].browse(action['res_id'])
        wizard.line_ids.filtered(lambda l: l.product_tmpl_id == new).action_select()
        self.assertEqual(line.product_id, new.product_variant_id)
        self.assertEqual(line.product_qty, 4)
        self.assertAlmostEqual(line.price_unit, 11.0)
        reference = self.env['purchase.order.line'].create({
            'order_id': order.id, 'product_id': new.product_variant_id.id, 'product_qty': 1,
        })
        self.assertEqual(line.name, reference.name)
        self.assertEqual(line.uom_id, reference.uom_id)

    # ------------------------------------------------------------------
    # Inventory valuation
    # ------------------------------------------------------------------

    def _add_stock(self, template, quantity):
        warehouse = self.env['stock.warehouse'].search([('company_id', '=', self.company.id)], limit=1)
        self.env['stock.quant']._update_available_quantity(template.product_variant_id, warehouse.lot_stock_id, quantity)
        self.env.invalidate_all()

    def test_part_categories_use_average_cost(self):
        for categ in (self.engine_categ, self.airend_categ, self.engine_categ.parent_id):
            self.assertEqual(categ.property_cost_method, 'average')
        sub_categ = self.env['product.category'].create({'name': "Filter", 'parent_id': self.engine_categ.id})
        self.assertEqual(sub_categ.property_cost_method, 'average')
        explicit = self.env['product.category'].create({
            'name': "Gasket", 'parent_id': self.engine_categ.id, 'property_cost_method': 'standard',
        })
        self.assertEqual(explicit.property_cost_method, 'standard')

    def test_average_cost_not_overwritten_when_in_stock(self):
        self._import([self.header, ["AV-1", "Piston", "", 10], ["AV-2", "Liner", "", 10]])
        in_stock, no_stock = self._part('AV-1'), self._part('AV-2')
        initial_cost = in_stock.standard_price
        # 10 USD * 16000 * 1.1 * 1.05
        self.assertAlmostEqual(initial_cost, 184800.0)
        self._add_stock(in_stock, 5)
        self.assertEqual(in_stock.qty_available, 5)

        self.engine_rule.write({'manual_rate': 17000.0})
        self.engine_rule.action_apply_prices()
        # Part in stock: AVCO cost kept, selling prices follow the new rate
        self.assertAlmostEqual(in_stock.standard_price, initial_cost)
        self.assertAlmostEqual(in_stock.list_price, 245500.0)  # 196350 * 1.25 = 245437.5 -> 245500
        # Part without stock: the formula still provides the initial cost
        self.assertAlmostEqual(no_stock.standard_price, 196350.0)
        # ... without an (empty) inventory revaluation
        self.assertFalse(self.env['product.value'].search([
            ('product_id', '=', no_stock.product_variant_id.id), ('new_cost', '=', 196350.0),
        ]))

    def test_standard_cost_follows_formula(self):
        self.engine_categ.property_cost_method = 'standard'
        self._import([self.header, ["ST-1", "Crank", "", 10]])
        part = self._part('ST-1')
        self._add_stock(part, 5)
        self.engine_rule.write({'manual_rate': 17000.0})
        self.engine_rule.action_apply_prices()
        self.assertAlmostEqual(part.standard_price, 196350.0)
        # Standard price with stock: the stock is revalued
        revaluation = self.env['product.value'].search([
            ('product_id', '=', part.product_variant_id.id), ('new_cost', '=', 196350.0),
        ])
        self.assertEqual(revaluation.quantity, 5)
