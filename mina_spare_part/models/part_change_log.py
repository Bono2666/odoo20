from odoo import fields, models

LOG_SOURCES = [
    ('import', "Import Data Part"),
    ('rate', "Update Kurs"),
    ('rule', "Hitung Ulang Harga"),
    ('manual', "Edit Manual"),
]


class MinaPartChangeLog(models.Model):
    _name = 'mina.part.change.log'
    _description = "Spare Part Change Log"
    _order = 'date desc, id desc'
    _rec_name = 'field_label'

    date = fields.Datetime(string="Tanggal", required=True, readonly=True, default=fields.Datetime.now, index=True)
    product_tmpl_id = fields.Many2one(
        'product.template', string="Produk", required=True, readonly=True, index=True, ondelete='cascade',
    )
    default_code = fields.Char(string="No. Part", readonly=True, index='trigram')
    categ_id = fields.Many2one(related='product_tmpl_id.categ_id', string="Kategori")
    part_type = fields.Selection(related='product_tmpl_id.part_type', string="Jenis Part")
    change_type = fields.Selection(
        [('create', "Baru"), ('update', "Perubahan")],
        string="Tipe", required=True, readonly=True,
    )
    field_name = fields.Char(string="Nama Teknis Field", readonly=True)
    field_label = fields.Char(string="Field", readonly=True)
    old_value = fields.Char(string="Nilai Lama", readonly=True)
    new_value = fields.Char(string="Nilai Baru", readonly=True)
    source = fields.Selection(LOG_SOURCES, string="Sumber", required=True, readonly=True, default='manual')
    import_id = fields.Many2one('mina.part.import', string="Import", readonly=True, index='btree_not_null', ondelete='set null')
    rule_id = fields.Many2one('mina.part.price.rule', string="Setting Harga", readonly=True, ondelete='set null')
    user_id = fields.Many2one('res.users', string="Diubah Oleh", readonly=True, default=lambda self: self.env.user)
