import csv
import io
import logging
import re
import unicodedata

import openpyxl
import xlrd

from odoo import api, fields, models
from odoo.exceptions import UserError, ValidationError
from odoo.tools import float_compare, float_round

from .product_category import PART_TYPES
from .product_template import (
    LOG_IMPORT_KEY,
    LOG_SOURCE_KEY,
    SKIP_SUBSTITUTE_KEY,
    SKIP_VENDOR_SYNC_KEY,
    VENDOR_SYNC_FIELDS,
    split_part_numbers,
)

_logger = logging.getLogger(__name__)

HEADER_SCAN_ROWS = 30
PRICE_DIGITS = 4
# Files up to this number of rows are imported right away, bigger files by the background job.
SYNC_ROW_LIMIT = 1000
BATCH_SIZE = 1000

# Accepted column headers. Compared after Unicode normalization (NFKC), lowercase,
# without spaces/punctuation, so "Liｓｔ Price（YEN）" matches "List Price (YEN)".
COLUMN_ALIASES = {
    'default_code': [
        "Part Number", "Part No", "Part No.", "PN", "P/N", "No Part", "No. Part", "Nomor Part", "Kode Part",
        "Part Code", "Material", "Material Number", "Item Code", "Kode Barang", "Internal Reference",
        "HOKUETSU Part No.", "部品番号", "北越部品番号",
    ],
    'substitute_part_numbers': [
        "Substitution Parts Number", "Substitution Part Number", "Substitution", "Substitute",
        "Substitute Part", "Substitute Part Number", "Substitute Part No", "Subtitute", "Subtitute Part",
        "Part Substitusi", "Substitusi", "Superseded By", "Supersede", "Replaced By", "Replacement",
        "Pengganti", "Part Pengganti", "代替部品番号",
    ],
    'name': [
        "Parts Name", "Part Name", "Description", "Part Description", "Nama Spare Part", "Nama Part",
        "Nama Barang", "Deskripsi", "Name", "Nama", "Item Description", "部品名",
    ],
    'part_list_price': [
        "Price Yen", "YEN", "List Price (YEN)", "Harga Principle", "Harga Principal", "Price", "List Price",
        "Unit Price", "Price List", "Pricelist", "Harga", "Harga List", "Harga Satuan", "Price USD", "USD",
        "Net Price", "FOB", "海外定価", "定価",
    ],
    'lead_time': [
        "LT", "Lead Time", "Lead Times", "Lead Time (DAYS)", "Lead Time Days", "Waktu Indent", "リードタイム",
    ],
    'stock_jpn': [
        "Stock JPN", "Stock Japan", "Stock Principal", "Stock Principle", "The number of available stock",
        "在庫数", "有効在庫数",
    ],
    'weight': ["Weight", "Weight Kg", "Weight (kg)", "Berat", "Berat Kg", "重量"],
    'remarks': ["Remarks", "Remark", "Keterangan", "Catatan", "備考", "備考4"],
    'availability': [
        "Product Availability", "Product Availablity", "Availability", "No longer available information",
        "Comment", "供給可否", "供給不可情報", "コメント",
    ],
    'category': ["Product Category", "Kategori", "Kategori Produk"],
    'currency': ["Currency", "Curr", "Mata Uang", "Valuta"],
    'uom': ["UoM", "Unit", "Satuan", "Unit of Measure"],
    'part_application': [
        "Application", "Aplikasi", "Model", "Engine Model", "Compressor Model", "Model Unit", "Unit Model",
    ],
}

# Column order of the principal catalog ("Data" sheet of the Engine and Air End catalogs).
# Used when the header is unreadable, e.g. a CSV export that turned the Japanese header into "????".
PRINCIPAL_LAYOUT = [
    'default_code', 'substitute_part_numbers', 'name', 'lead_time', 'stock_jpn', 'part_list_price',
    'weight', 'remarks', 'availability',
]
GARBLED_RE = re.compile(r'^\?+$')


def normalize_header(value):
    text = unicodedata.normalize('NFKC', str(value or '')).lower()
    return re.sub(r'[\W_]+', '', text)


NORMALIZED_ALIASES = {
    key: {normalize_header(alias) for alias in aliases}
    for key, aliases in COLUMN_ALIASES.items()
}


def _cell_to_text(value):
    """Text of a spreadsheet cell; whole floats (1234.0) become integers (1234)."""
    if value is None:
        return ''
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return str(value).strip()


def parse_number(value, decimal_comma=False):
    """Parse a numeric cell; accepts numbers and texts like '1,234.50', '1.234,50' or 'USD 12.5'.

    :param decimal_comma: the file uses a comma as decimal separator ("0,3")
    :return: float, or None when the cell holds no number ('', '-', 'N/A')
    :raise ValueError: when the text is not a valid number
    """
    if value is None or value == '':
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = re.sub(r'[^0-9,.\-]', '', str(value))
    if not re.search(r'\d', text):
        return None
    if decimal_comma:
        text = text.replace('.', '').replace(',', '.')
    elif ',' in text and '.' in text:
        # The right-most separator is the decimal one.
        if text.rfind(',') > text.rfind('.'):
            text = text.replace('.', '').replace(',', '.')
        else:
            text = text.replace(',', '')
    elif ',' in text:
        text = text.replace(',', '') if re.fullmatch(r'-?\d{1,3}(,\d{3})+', text) else text.replace(',', '.')
    elif text.count('.') > 1:
        text = text.replace('.', '')
    return float(text)


def parse_price(value, decimal_comma=False):
    number = parse_number(value, decimal_comma)
    return None if number is None else round(number, PRICE_DIGITS)


def parse_availability(value):
    text = normalize_header(value)
    if not text:
        return 'available'
    if 'discontinu' in text or '供給不可' in text:
        return 'discontinued'
    if 'substitut' in text or '代替' in text:
        return 'substituted'
    return 'available'


class MinaPartImport(models.Model):
    _name = 'mina.part.import'
    _description = "Spare Part Data Import"
    _inherit = ['mail.thread']
    _order = 'id desc'
    _check_company_auto = True

    def _default_categ_id(self):
        part_type = self.env.context.get('default_part_type', 'engine')
        return self.env['product.category'].search([('part_type', '=', part_type)], order='parent_path', limit=1)

    name = fields.Char(string="No. Import", required=True, readonly=True, copy=False, default="/")
    part_type = fields.Selection(
        PART_TYPES, string="Jenis File", required=True, default='engine', tracking=True,
        help="Engine: file part engine. Air End: file part air end (compressor). "
             "Part dari kedua file dibedakan berdasarkan kategori produk.",
    )
    categ_id = fields.Many2one(
        'product.category', string="Kategori Tujuan", required=True, tracking=True,
        default=_default_categ_id,
        domain="[('part_type', '=', part_type)]",
        help="Kategori produk untuk part baru dari file ini. Jika file punya kolom Product Category, "
             "part ditempatkan di sub-kategori dengan nama tersebut.",
    )
    company_id = fields.Many2one('res.company', string="Perusahaan", required=True, default=lambda self: self.env.company)
    file = fields.Binary(string="File", required=True, attachment=True)
    file_name = fields.Char(string="Nama File")
    sheet_name = fields.Char(
        string="Nama Sheet",
        help="Kosongkan agar sheet data dicari otomatis (khusus Excel, mis. sheet 'Data' pada katalog principal).",
    )
    state = fields.Selection([
        ('draft', "Draft"),
        ('queued', "Diproses"),
        ('done', "Selesai"),
        ('failed', "Gagal"),
    ], default='draft', required=True, tracking=True)
    import_date = fields.Datetime(string="Tanggal Import", readonly=True, copy=False)
    user_id = fields.Many2one('res.users', string="Diimport Oleh", readonly=True, copy=False)

    create_new = fields.Boolean(string="Buat Part Baru", default=True)
    update_existing = fields.Boolean(string="Update Part yang Sudah Ada", default=True)
    apply_pricing = fields.Boolean(
        string="Hitung Harga Jual", default=True,
        help="Hitung harga reseller dan end user dengan Setting Harga untuk part baru dan part yang harganya berubah.",
    )

    line_ids = fields.One2many('mina.part.import.line', 'import_id', string="Hasil Import", readonly=True)
    change_log_ids = fields.One2many('mina.part.change.log', 'import_id', string="Log Perubahan", readonly=True)
    data_rows = fields.Integer(string="Baris Data di File", readonly=True, copy=False)
    next_row = fields.Integer(readonly=True, copy=False)
    progress = fields.Float(string="Progres", compute='_compute_progress')
    total_rows = fields.Integer(string="Total Baris", readonly=True, copy=False)
    created_count = fields.Integer(string="Part Baru", readonly=True, copy=False)
    updated_count = fields.Integer(string="Part Diupdate", readonly=True, copy=False)
    unchanged_count = fields.Integer(string="Tidak Berubah", readonly=True, copy=False)
    skipped_count = fields.Integer(string="Dilewati", readonly=True, copy=False)
    error_count = fields.Integer(string="Error", readonly=True, copy=False)
    priced_count = fields.Integer(string="Harga Jual Berubah", readonly=True, copy=False)
    unpriced_count = fields.Integer(readonly=True, copy=False)
    change_log_count = fields.Integer(compute='_compute_change_log_count')
    file_warning = fields.Text(readonly=True, copy=False)
    summary = fields.Text(string="Catatan", readonly=True, copy=False)

    @api.depends('next_row', 'data_rows', 'state')
    def _compute_progress(self):
        for part_import in self:
            if part_import.state == 'done':
                part_import.progress = 100.0
            elif part_import.data_rows:
                part_import.progress = part_import.next_row / part_import.data_rows * 100
            else:
                part_import.progress = 0.0

    def _compute_change_log_count(self):
        counts = dict(self.env['mina.part.change.log']._read_group(
            [('import_id', 'in', self.ids)], ['import_id'], ['__count'],
        ))
        for part_import in self:
            part_import.change_log_count = counts.get(part_import, 0)

    @api.onchange('part_type')
    def _onchange_part_type(self):
        if self.categ_id.part_type != self.part_type:
            self.categ_id = self.with_context(default_part_type=self.part_type)._default_categ_id()

    @api.constrains('part_type', 'categ_id')
    def _check_categ_part_type(self):
        for part_import in self:
            if part_import.categ_id.part_type != part_import.part_type:
                raise ValidationError(self.env._("Kategori tujuan harus kategori dengan jenis part yang sama dengan jenis file."))

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', '/') == '/':
                vals['name'] = self.env['ir.sequence'].next_by_code('mina.part.import') or '/'
        return super().create(vals_list)

    @api.ondelete(at_uninstall=False)
    def _unlink_except_processed(self):
        if any(part_import.state != 'draft' for part_import in self):
            raise UserError(self.env._("Hanya import berstatus Draft yang bisa dihapus."))

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def action_import(self):
        self.ensure_one()
        if self.state != 'draft':
            raise UserError(self.env._("Import ini sudah diproses."))
        table = self._load_table()
        data_rows = len(table['rows']) - table['header_index'] - 1
        self.write({
            'state': 'queued',
            'next_row': 0,
            'data_rows': data_rows,
            'file_warning': table['warning'],
            'import_date': fields.Datetime.now(),
            'user_id': self.env.uid,
        })
        if data_rows <= SYNC_ROW_LIMIT:
            self._run_import(table)
            return True
        self.env.ref('mina_spare_part.ir_cron_mina_part_import')._trigger()
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'type': 'info',
                'title': self.env._("Import berjalan di background"),
                'message': self.env._(
                    "%s baris sedang diproses per batch. Buka kembali dokumen ini untuk melihat progres.",
                    data_rows,
                ),
                'next': {'type': 'ir.actions.client', 'tag': 'soft_reload'},
            },
        }

    def action_resume(self):
        self.ensure_one()
        if self.state != 'failed':
            raise UserError(self.env._("Hanya import yang gagal yang bisa dilanjutkan."))
        self.write({'state': 'queued', 'summary': False})
        self.env.ref('mina_spare_part.ir_cron_mina_part_import')._trigger()
        return True

    def action_view_products(self):
        self.ensure_one()
        action = self.env['ir.actions.act_window']._for_xml_id('mina_spare_part.mina_part_product_action')
        action['domain'] = [('id', 'in', self.line_ids.product_tmpl_id.ids)]
        return action

    def action_view_change_log(self):
        self.ensure_one()
        action = self.env['ir.actions.act_window']._for_xml_id('mina_spare_part.mina_part_change_log_action')
        action['domain'] = [('import_id', '=', self.id)]
        return action

    def action_download_template(self):
        self.ensure_one()
        workbook = openpyxl.Workbook()
        sheet = workbook.active
        sheet.title = "Data"
        sheet.append([
            "Part No.", "Substitution Parts Number", "Parts name", "Lead Times", "Stock JPN", "YEN",
            "Weight", "Remarks", "Product Availablity",
        ])
        sheet.append(["006208UUC3", "2147211200", "MOTOR BEARING", 126, 0, 0, None, None, "Substitution Parts Number"])
        sheet.append(["006208VVC3", None, "MOTOR BEARING", 42, 62, 3770, 0.37, None, None])
        sheet.append(["006211VVC3", None, "MOTOR BEARING", 36, 0, 0, None, None, "discontinued"])
        content = io.BytesIO()
        workbook.save(content)
        attachment = self.env['ir.attachment'].create({
            'name': f"template_import_part_{self.part_type}.xlsx",
            'raw': content.getvalue(),
            'res_model': self._name,
            'res_id': self.id,
        })
        return {
            'type': 'ir.actions.act_url',
            'url': f'/web/content/{attachment.id}?download=true',
            'target': 'self',
        }

    # ------------------------------------------------------------------
    # Background job
    # ------------------------------------------------------------------

    @api.model
    def _cron_process_imports(self):
        Cron = self.env['ir.cron']
        for part_import in self.search([('state', '=', 'queued')], order='id'):
            try:
                finished = part_import.with_user(part_import.user_id).with_company(part_import.company_id)._run_import(
                    commit=True,
                )
            except Exception as error:  # noqa: BLE001 job boundary: the error is stored on the import
                Cron._rollback_progress()
                _logger.exception("Spare part import %s failed", part_import.name)
                part_import.write({'state': 'failed', 'summary': self.env._(
                    "Import berhenti di baris %(row)s: %(error)s\nData sebelum baris ini sudah tersimpan; "
                    "klik Lanjutkan setelah masalah diperbaiki.",
                    row=part_import.next_row + 1, error=error,
                )})
                Cron._commit_progress()
                continue
            if not finished:
                break
        if self.search_count([('state', '=', 'queued')], limit=1):
            self.env.ref('mina_spare_part.ir_cron_mina_part_import')._trigger()

    def _run_import(self, table=None, batch_size=BATCH_SIZE, commit=False):
        """Process the remaining rows by batch, then finalize the import.

        :param commit: commit after each batch (only from the background job)
        :return: True when the import is finished, False when the job ran out of time
        """
        self.ensure_one()
        table = table or self._load_table()
        rows = table['rows'][table['header_index'] + 1:]
        first_row_number = table['header_index'] + 2
        while self.state == 'queued':
            start = self.next_row
            if start < len(rows):
                end = min(start + batch_size, len(rows))
                self._process_rows(rows[start:end], first_row_number + start, table)
                self.next_row = end
            else:
                end = start
                self._finalize()
            if commit:
                remaining = len(rows) - self.next_row + (1 if self.state == 'queued' else 0)
                if not self.env['ir.cron']._commit_progress(end - start, remaining=remaining):
                    return self.state != 'queued'
        return True

    # ------------------------------------------------------------------
    # File reading
    # ------------------------------------------------------------------

    def _load_table(self):
        """Read the file and locate its data.

        :return: dict with ``rows`` (lists of raw cell values), ``header_index``, ``columns``
            (column key -> index), ``decimal_comma`` and ``warning``
        """
        self.ensure_one()
        content = self.file.content if self.file else b''
        if not content:
            raise UserError(self.env._("Silakan upload file terlebih dahulu."))
        file_name = (self.file_name or '').lower()
        decimal_comma = False
        if file_name.endswith(('.csv', '.txt')):
            rows, delimiter = self._read_csv(content)
            sheets = [('', rows)]
            decimal_comma = delimiter == ';'
        elif file_name.endswith('.xls'):
            sheets = self._read_xls(content)
        else:
            sheets = self._read_xlsx(content)
        if self.sheet_name:
            sheets = [sheet for sheet in sheets if sheet[0] == self.sheet_name]
            if not sheets:
                raise UserError(self.env._("Sheet '%s' tidak ditemukan.", self.sheet_name))

        # Catalogs have an information sheet before the data: take the sheet with the most data rows.
        best = None
        for _sheet_name, rows in sheets:
            detected = self._detect_columns(rows)
            if detected and (not best or len(rows) - detected[0] > len(best['rows']) - best['header_index']):
                best = {'rows': rows, 'header_index': detected[0], 'columns': detected[1], 'warning': False}
        if not best:
            best = self._detect_principal_layout(sheets[0][1] if sheets else [])
        if not best:
            raise UserError(self.env._(
                "Kolom nomor part tidak ditemukan. Pastikan ada header salah satu dari: %s",
                COLUMN_ALIASES['default_code'],
            ))
        best['decimal_comma'] = decimal_comma
        return best

    def _read_csv(self, content):
        for encoding in ('utf-8-sig', 'cp932'):
            try:
                text = content.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        else:
            text = content.decode('latin-1')
        try:
            dialect = csv.Sniffer().sniff(text[:4096], delimiters=',;\t|')
        except csv.Error:
            dialect = csv.excel
        return list(csv.reader(io.StringIO(text), dialect)), dialect.delimiter

    def _read_xls(self, content):
        try:
            book = xlrd.open_workbook(file_contents=content)
        except xlrd.XLRDError as error:
            raise UserError(self.env._("File Excel tidak bisa dibaca: %s", error)) from error
        return [
            (sheet.name, [sheet.row_values(index) for index in range(sheet.nrows)])
            for sheet in book.sheets()
        ]

    def _read_xlsx(self, content):
        try:
            book = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
        except Exception as error:  # noqa: BLE001 openpyxl raises many unrelated types on an unsupported file
            raise UserError(self.env._(
                "File tidak bisa dibaca. Gunakan format .xlsx, .xls atau .csv. (%s)", error,
            )) from error
        sheets = [(sheet.title, [list(row) for row in sheet.iter_rows(values_only=True)]) for sheet in book.worksheets]
        book.close()
        return sheets

    def _match_columns(self, row):
        columns = {}
        for col_index, cell in enumerate(row):
            header = normalize_header(cell)
            for key, aliases in NORMALIZED_ALIASES.items():
                if header in aliases and key not in columns:
                    columns[key] = col_index
                    break
        return columns

    def _detect_columns(self, rows):
        """Find the header row: (index, {column key: column index}) or None."""
        for index, row in enumerate(rows[:HEADER_SCAN_ROWS]):
            columns = self._match_columns(row)
            if 'default_code' in columns:
                return index, columns
        return None

    def _detect_principal_layout(self, rows):
        """Principal catalog whose header became '????' (non-Unicode CSV export): use the column order."""
        if not rows or len(rows[0]) < len(PRINCIPAL_LAYOUT) or not GARBLED_RE.match(_cell_to_text(rows[0][0])):
            return None
        columns = {key: index for index, key in enumerate(PRINCIPAL_LAYOUT)}
        for key, index in self._match_columns(rows[0]).items():
            columns.setdefault(key, index)
        # The Japanese remarks were lost too ("????"): keep the remarks already in Odoo.
        del columns['remarks']
        return {
            'rows': rows,
            'header_index': 0,
            'columns': columns,
            'warning': self.env._(
                "Header file tidak terbaca (karakter Jepang hilang saat disimpan sebagai CSV). "
                "Kolom dibaca berdasarkan urutan standar katalog principal dan kolom Remarks tidak diimport. "
                "Gunakan file Excel asli (.xls/.xlsx) agar Remarks ikut terimport.",
            ),
        }

    # ------------------------------------------------------------------
    # Processing
    # ------------------------------------------------------------------

    def _template_env(self):
        return self.env['product.template'].with_company(self.company_id).with_context(**{
            LOG_SOURCE_KEY: 'import',
            LOG_IMPORT_KEY: self.id,
            SKIP_SUBSTITUTE_KEY: True,
            SKIP_VENDOR_SYNC_KEY: True,
            # The spare part change log records the changes: no chatter message/follower per product.
            'tracking_disable': True,
            'mail_create_nolog': True,
            'mail_create_nosubscribe': True,
        })

    def _parse_rows(self, rows, first_row_number, table, seen):
        """Validate the file rows.

        :param seen: {part number: row number} of the rows already imported, updated in place
        :return: (list of parsed row dicts, list of import line vals for rejected rows)
        """
        columns = table['columns']
        decimal_comma = table['decimal_comma']
        currencies, uoms, categories = {}, {}, {}
        Currency = self.env['res.currency'].with_context(active_test=False)
        parsed, rejected = [], []

        def cell(row, key):
            col_index = columns.get(key)
            if col_index is None or col_index >= len(row):
                return None
            return row[col_index]

        def number(key, label, errors):
            raw = cell(row, key)
            try:
                return parse_number(raw, decimal_comma)
            except ValueError:
                errors.append(self.env._("%(column)s '%(value)s' bukan angka.", column=label, value=raw))
                return None

        for row_number, row in enumerate(rows, start=first_row_number):
            if not any(_cell_to_text(value) for value in row):
                continue
            code = _cell_to_text(cell(row, 'default_code'))
            name = _cell_to_text(cell(row, 'name'))
            line = {'row_number': row_number, 'default_code': code, 'name': name}
            if not code:
                rejected.append({**line, 'status': 'error', 'message': self.env._("No. part kosong.")})
                continue
            if code in seen:
                rejected.append({**line, 'status': 'skipped', 'message': self.env._(
                    "Duplikat No. part di baris %s, baris ini dilewati.", seen[code],
                )})
                continue
            seen[code] = row_number
            errors = []
            values = {'default_code': code, 'name': name}

            price = number('part_list_price', self.env._("Harga"), errors)
            values['part_list_price'] = None if price is None else round(price, PRICE_DIGITS)
            if price is not None and price < 0:
                errors.append(self.env._("Harga tidak boleh negatif."))
            if 'lead_time' in columns:
                lead_time = number('lead_time', self.env._("Lead time"), errors)
                values['part_lead_time'] = int(round(lead_time or 0))
            if 'stock_jpn' in columns:
                values['part_stock_jpn'] = number('stock_jpn', self.env._("Stock JPN"), errors) or 0.0
            if 'weight' in columns:
                values['weight'] = number('weight', self.env._("Weight"), errors) or 0.0

            currency_code = _cell_to_text(cell(row, 'currency')).upper()
            if currency_code:
                if currency_code not in currencies:
                    currencies[currency_code] = Currency.search([('name', '=', currency_code)], limit=1)
                if not currencies[currency_code]:
                    errors.append(self.env._("Mata uang '%s' tidak dikenal.", currency_code))
                values['currency'] = currencies[currency_code]

            uom_name = _cell_to_text(cell(row, 'uom'))
            if uom_name:
                if uom_name.lower() not in uoms:
                    uoms[uom_name.lower()] = self.env['uom.uom'].search([('name', '=ilike', uom_name)], limit=1)
                if not uoms[uom_name.lower()]:
                    errors.append(self.env._("Satuan '%s' tidak dikenal.", uom_name))
                values['uom'] = uoms[uom_name.lower()]

            category_name = _cell_to_text(cell(row, 'category'))
            if category_name:
                if category_name not in categories:
                    categories[category_name] = self._get_sub_category(category_name)
                values['categ'] = categories[category_name]

            if 'substitute_part_numbers' in columns:
                substitutes = [c for c in split_part_numbers(_cell_to_text(cell(row, 'substitute_part_numbers'))) if c != code]
                values['substitute_part_numbers'] = ', '.join(substitutes)
            if 'part_application' in columns:
                values['part_application'] = _cell_to_text(cell(row, 'part_application'))
            if 'remarks' in columns:
                values['part_remarks'] = _cell_to_text(cell(row, 'remarks'))
            if 'availability' in columns:
                values['part_availability'] = parse_availability(cell(row, 'availability'))

            if errors:
                rejected.append({**line, 'status': 'error', 'message': '\n'.join(errors)})
                continue
            parsed.append({'line': line, 'values': values})
        return parsed, rejected

    def _get_sub_category(self, name):
        """Sub-category of the target category for the file's 'Product Category' column (created if needed)."""
        Category = self.env['product.category']
        if name.lower() == (self.categ_id.name or '').lower():
            return self.categ_id
        category = Category.search([('parent_id', '=', self.categ_id.id), ('name', '=ilike', name)], limit=1)
        return category or Category.create({'name': name, 'parent_id': self.categ_id.id})

    def _prepare_update_vals(self, product, values, default_currency):
        """Only the values that differ from the product."""
        vals = {}
        if values['name'] and values['name'] != product.name:
            vals['name'] = values['name']
        price = values.get('part_list_price')
        if price is not None and float_compare(price, product.part_list_price, precision_digits=PRICE_DIGITS):
            vals['part_list_price'] = price
        currency = values.get('currency')
        if currency and currency != product.part_currency_id:
            vals['part_currency_id'] = currency.id
        elif not product.part_currency_id and default_currency:
            vals['part_currency_id'] = default_currency.id
        uom = values.get('uom')
        if uom and uom != product.uom_id:
            vals['uom_id'] = uom.id
        categ = values.get('categ')
        if categ and categ != product.categ_id:
            vals['categ_id'] = categ.id
        if 'part_lead_time' in values and values['part_lead_time'] != product.part_lead_time:
            vals['part_lead_time'] = values['part_lead_time']
        if 'part_stock_jpn' in values and float_compare(values['part_stock_jpn'], product.part_stock_jpn, precision_digits=4):
            vals['part_stock_jpn'] = values['part_stock_jpn']
        if 'weight' in values:
            digits = self.env['decimal.precision'].precision_get('Stock Weight')
            weight = float_round(values['weight'], precision_digits=digits)
            if float_compare(weight, product.weight, precision_digits=digits):
                vals['weight'] = weight
        if 'part_availability' in values and values['part_availability'] != product.part_availability:
            vals['part_availability'] = values['part_availability']
        for fname in ('substitute_part_numbers', 'part_application', 'part_remarks'):
            if fname in values and values[fname] != (product[fname] or ''):
                vals[fname] = values[fname] or False
        return vals

    def _prepare_create_vals(self, values, default_currency):
        currency = values.get('currency') or default_currency
        vals = {
            'name': values['name'] or values['default_code'],
            'default_code': values['default_code'],
            'categ_id': (values.get('categ') or self.categ_id).id,
            'type': 'consu',
            'is_storable': True,
            'sale_ok': True,
            'purchase_ok': True,
            'list_price': 0.0,
            'part_list_price': values.get('part_list_price') or 0.0,
            'part_currency_id': currency.id,
            'part_import_id': self.id,
        }
        for fname in (
            'substitute_part_numbers', 'part_application', 'part_remarks', 'part_availability',
            'part_lead_time', 'part_stock_jpn', 'weight',
        ):
            if values.get(fname):
                vals[fname] = values[fname]
        if values.get('uom'):
            vals['uom_id'] = values['uom'].id
        return vals

    def _get_default_currency(self):
        """Currency of the price rule of the target category, used when the file has no currency."""
        rules = self.env['mina.part.price.rule'].with_company(self.company_id)._get_rules_by_category()
        product = self.env['product.template'].new({'categ_id': self.categ_id.id})
        return product._mina_find_price_rule(rules).currency_id

    def _process_rows(self, rows, first_row_number, table):
        """Import one batch of rows: create/update the parts, price them and store the line results."""
        self.ensure_one()
        Template = self._template_env()
        codes = [_cell_to_text(row[table['columns']['default_code']]) for row in rows
                 if table['columns']['default_code'] < len(row)]
        seen = {
            line.default_code: line.row_number
            for line in self.env['mina.part.import.line'].search([
                ('import_id', '=', self.id), ('default_code', 'in', codes), ('status', '!=', 'error'),
            ])
        }
        parsed, line_vals = self._parse_rows(rows, first_row_number, table, seen)
        default_currency = self._get_default_currency()

        existing = Template.with_context(active_test=False).search(
            [('default_code', 'in', [row['values']['default_code'] for row in parsed]),
             ('part_type', '=', self.part_type)],
            order='active desc, id',
        )
        by_code = {}
        for product in existing:
            by_code.setdefault(product.default_code, product)

        to_create = []
        to_price = Template.browse()
        to_sync_vendor = Template.browse()
        for row in parsed:
            values, line = row['values'], row['line']
            product = by_code.get(values['default_code'])
            if not product:
                if self.create_new:
                    to_create.append(row)
                else:
                    line_vals.append({**line, 'status': 'skipped', 'message': self.env._("Part baru, tidak dibuat.")})
                continue
            vals = self._prepare_update_vals(product, values, default_currency)
            if not vals:
                line_vals.append({**line, 'status': 'unchanged', 'product_tmpl_id': product.id})
            elif not self.update_existing:
                line_vals.append({**line, 'status': 'skipped', 'product_tmpl_id': product.id,
                                  'message': self.env._("Ada perubahan, tetapi update part dinonaktifkan.")})
            else:
                changed = [product._fields[fname]._description_string(self.env) for fname in vals]
                vals['part_import_id'] = self.id
                product.write(vals)
                if {'part_list_price', 'part_currency_id', 'categ_id'} & vals.keys():
                    to_price |= product
                if VENDOR_SYNC_FIELDS & vals.keys():
                    to_sync_vendor |= product
                line_vals.append({**line, 'status': 'updated', 'product_tmpl_id': product.id,
                                  'message': self.env._("Berubah: %s", changed)})

        created = Template.create([self._prepare_create_vals(row['values'], default_currency) for row in to_create])
        for row, product in zip(to_create, created):
            line_vals.append({**row['line'], 'status': 'created', 'product_tmpl_id': product.id})
        to_price |= created
        # Vendor prices are synced once per batch instead of once per product.
        (to_sync_vendor | created).with_context(**{SKIP_VENDOR_SYNC_KEY: False})._mina_sync_vendor_prices()

        priced = unpriced = 0
        if self.apply_pricing and to_price:
            rules = self.env['mina.part.price.rule'].with_company(self.company_id)._get_rules_by_category()
            for rule, products in to_price.grouped(lambda p: p._mina_find_price_rule(rules)).items():
                if not rule:
                    unpriced += len(products)
                elif rule.apply_on_import:
                    priced += rule.with_context(**{LOG_SOURCE_KEY: 'import', LOG_IMPORT_KEY: self.id})._apply_prices(
                        products,
                    )['updated']

        def count(status):
            return sum(1 for vals in line_vals if vals['status'] == status)

        line_vals.sort(key=lambda vals: vals['row_number'])
        self.write({
            'line_ids': [fields.Command.create(vals) for vals in line_vals],
            'total_rows': self.total_rows + len(line_vals),
            'created_count': self.created_count + count('created'),
            'updated_count': self.updated_count + count('updated'),
            'unchanged_count': self.unchanged_count + count('unchanged'),
            'skipped_count': self.skipped_count + count('skipped'),
            'error_count': self.error_count + count('error'),
            'priced_count': self.priced_count + priced,
            'unpriced_count': self.unpriced_count + unpriced,
        })

    def _finalize(self):
        """Link the substitutes (all rows are known now) and close the import."""
        self.ensure_one()
        Template = self._template_env()
        parts = Template.with_context(active_test=False).search([
            ('part_type', '!=', False), ('substitute_part_numbers', '!=', False),
        ])
        missing = parts._mina_resolve_substitutes()
        missing_lines = self.env['mina.part.import.line'].search([
            ('import_id', '=', self.id), ('product_tmpl_id', 'in', list(missing)),
        ])
        for line in missing_lines:
            note = self.env._("Part substitusi belum ada di master: %s", missing[line.product_tmpl_id.id])
            line.message = f"{line.message}\n{note}" if line.message else note

        summary = [self.file_warning] if self.file_warning else []
        if missing_lines:
            summary.append(self.env._(
                "%s part di file ini punya No. part substitusi yang belum ada di master data "
                "(lihat kolom Keterangan, atau filter 'Substitusi Belum Ditemukan' di Data Part).",
                len(missing_lines),
            ))
        if self.unpriced_count:
            summary.append(self.env._(
                "%s part belum dihitung harga jualnya karena belum ada Setting Harga untuk kategorinya.",
                self.unpriced_count,
            ))
        self.write({'state': 'done', 'summary': '\n'.join(summary) or False})


class MinaPartImportLine(models.Model):
    _name = 'mina.part.import.line'
    _description = "Spare Part Data Import Line"
    _order = 'import_id, row_number'

    import_id = fields.Many2one('mina.part.import', required=True, index=True, ondelete='cascade')
    row_number = fields.Integer(string="Baris")
    default_code = fields.Char(string="No. Part", index=True)
    name = fields.Char(string="Deskripsi")
    product_tmpl_id = fields.Many2one('product.template', string="Produk", index='btree_not_null', ondelete='set null')
    status = fields.Selection([
        ('created', "Baru"),
        ('updated', "Diupdate"),
        ('unchanged', "Tidak Berubah"),
        ('skipped', "Dilewati"),
        ('error', "Error"),
    ], string="Status", required=True)
    message = fields.Text(string="Keterangan")
