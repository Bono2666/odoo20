from odoo import api, fields, models

PART_TYPES = [
    ('engine', "Engine"),
    ('airend', "Airend (Compressor)"),
]


class ProductCategory(models.Model):
    _inherit = 'product.category'

    part_type = fields.Selection(
        PART_TYPES,
        string="Jenis Part",
        compute='_compute_part_type', store=True, readonly=False, recursive=True,
        help="Kategori dengan jenis part dipakai sebagai tujuan import data part. "
             "Sub-kategori otomatis mengikuti jenis part kategori induknya.",
    )

    @api.depends('parent_id.part_type')
    def _compute_part_type(self):
        for categ in self:
            categ.part_type = categ.parent_id.part_type or categ.part_type
