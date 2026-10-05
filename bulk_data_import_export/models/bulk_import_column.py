from odoo import _, api, fields, models


class BulkImportColumn(models.Model):
    _name = 'bulk.import.column'
    _description = 'Bulk Import Column'
    _order = 'sequence, id'

    job_id = fields.Many2one('bulk.import.job', required=True, ondelete='cascade')
    sequence = fields.Integer()
    column_index = fields.Integer(required=True)
    header = fields.Char(string='Column in file', readonly=True)
    field_path = fields.Char(string='Import into field',
                             help="Technical field name. Use \"field/id\" to give an External ID and "
                                  "\"line_ids/field\" for lines. Leave empty to skip the column.")
    field_info = fields.Char(string='Field', compute='_compute_field_info')

    @api.depends('field_path', 'job_id.model_id')
    def _compute_field_info(self):
        for column in self:
            if not column.field_path:
                column.field_info = _("Skipped")
                continue
            model = self.env.get(column.job_id.model_id.model)
            if model is None:
                column.field_info = False
                continue
            column.field_info = column._path_error(model) or column._path_label(model)

    def _walk(self, model):
        """Yield (model, part, field) for each part of the field path."""
        for part in self.field_path.strip().split('/'):
            if part in ('id', '.id'):
                yield model, part, None
                return
            field = model._fields.get(part)
            yield model, part, field
            if field is None or not field.relational:
                return
            model = self.env[field.comodel_name]

    def _path_error(self, model):
        parts = self.field_path.strip().split('/')
        for depth, (current, part, field) in enumerate(self._walk(model)):
            if part in ('id', '.id'):
                if depth + 1 < len(parts):
                    return _("\"%s\" must be the last part of the path.", part)
                continue
            if field is None:
                return _("%(model)s has no field \"%(field)s\".", model=current._description, field=part)
            if not field.relational and depth + 1 < len(parts):
                return _("\"%s\" is not a relational field.", part)
        return False

    def _path_label(self, model):
        labels = []
        for _current, part, field in self._walk(model):
            if part == 'id':
                labels.append(_('External ID'))
            elif part == '.id':
                labels.append(_('Database ID'))
            else:
                labels.append(field._description_string(self.env))
        return ' / '.join(labels)
