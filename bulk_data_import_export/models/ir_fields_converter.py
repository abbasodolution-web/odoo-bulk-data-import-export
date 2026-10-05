from odoo import models


class IrFieldsConverter(models.AbstractModel):
    _inherit = 'ir.fields.converter'

    def db_id_for(self, model, field, subfield, value):
        """Remember linked records found by name during a bulk import.

        Odoo searches the name again for every row ("Pakistan" 50,000 times);
        the cache makes that one search per value and chunk. Only active inside
        bulk imports, and never for models the import itself creates records in.
        """
        cache = self.env.context.get('bulk_import_name_cache')
        if (cache is None or subfield is not None or not value
                or field.comodel_name in self.env.context.get('bulk_import_no_cache', ())):
            return super().db_id_for(model, field, subfield, value)
        key = (field.comodel_name, value)
        if key in cache:
            return cache[key], 'name', []
        record_id, field_type, warnings = super().db_id_for(model, field, subfield, value)
        if record_id:
            cache[key] = record_id
        return record_id, field_type, warnings
