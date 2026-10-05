import base64
import io
import itertools
import json
import logging
import time

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError
from odoo.models import fix_import_export_id_paths
from odoo.tools.lru import LRU

from ..tools import spreadsheet

_logger = logging.getLogger(__name__)

# Subfields understood by Model.load() after a relational field ("partner_id/id")
SPECIAL_SUBFIELDS = {
    'id': 'id', 'external id': 'id', 'xml id': 'id',
    '.id': '.id', 'database id': '.id', 'db id': '.id',
}
# Context that turns off chatter tracking and other per-record overhead
FAST_CONTEXT = {
    'import_file': True,
    'tracking_disable': True,
    'mail_create_nolog': True,
    'mail_create_nosubscribe': True,
    'mail_notrack': True,
    'mail_auto_subscribe_no_notify': True,
}
MATCH_FIELD_TYPES = ('char', 'integer', 'selection')


class BulkImportJob(models.Model):
    _name = 'bulk.import.job'
    _inherit = 'bulk.job.mixin'
    _description = 'Bulk Import Job'
    _order = 'id desc'

    name = fields.Char(required=True, default=lambda self: _('New import'))
    state = fields.Selection([
        ('draft', 'Draft'),
        ('queued', 'Queued'),
        ('running', 'Running'),
        ('done', 'Done'),
        ('failed', 'Failed'),
        ('cancelled', 'Cancelled'),
    ], default='draft', required=True, readonly=True, copy=False)
    test_mode = fields.Boolean(readonly=True, copy=False,
                               help="Test run: every row is checked, nothing is saved.")
    model_id = fields.Many2one('ir.model', string='Import into', required=True, ondelete='cascade',
                               domain=[('transient', '=', False)])
    model_name = fields.Char(related='model_id.model')
    file = fields.Binary(attachment=True, copy=False)
    file_name = fields.Char(copy=False)
    mode = fields.Selection([
        ('create', 'Create new records only'),
        ('create_update', 'Create new and update existing records'),
        ('update', 'Update existing records only'),
    ], default='create_update', required=True)
    match_field_id = fields.Many2one(
        'ir.model.fields', string='Match existing records by', ondelete='set null',
        domain="[('model_id', '=', model_id), ('store', '=', True), ('ttype', 'in', %s)]" % (list(MATCH_FIELD_TYPES),),
        help="Column used to find records that already exist, for example Internal Reference or Email.\n"
             "Leave empty to match on an 'External ID' (id) or 'Database ID' (.id) column if the file has one.")
    chunk_size = fields.Integer(default=500, required=True,
                                help="Rows saved per transaction. A failing row never blocks the rest of its chunk.")
    ignore_empty = fields.Boolean(string='Skip empty cells', default=True,
                                  help="Empty cells keep the current value on update and the default value on create, "
                                       "instead of clearing the field.")
    create_missing_relations = fields.Boolean(
        string='Create missing linked records',
        help="When a many2one value (for example a product category name) is not found, create it instead of "
             "reporting an error.")
    column_ids = fields.One2many('bulk.import.column', 'job_id', string='Columns', copy=True)
    error_ids = fields.One2many('bulk.import.error', 'job_id', string='Errors', readonly=True)
    row_count = fields.Integer(string='Rows in file', readonly=True, copy=False)
    next_row = fields.Integer(string='Rows processed', readonly=True, copy=False)
    created_count = fields.Integer(string='Created', readonly=True, copy=False)
    updated_count = fields.Integer(string='Updated', readonly=True, copy=False)
    error_count = fields.Integer(string='Failed rows', readonly=True, copy=False)
    progress = fields.Float(compute='_compute_progress')
    error_file = fields.Binary(string='Error report', attachment=True, readonly=True, copy=False)
    error_file_name = fields.Char(copy=False)
    template_file = fields.Binary(attachment=True, readonly=True, copy=False)
    message = fields.Text(readonly=True, copy=False)
    user_id = fields.Many2one('res.users', default=lambda self: self.env.user, required=True, readonly=True)
    company_id = fields.Many2one('res.company', default=lambda self: self.env.company, required=True)
    date_start = fields.Datetime(readonly=True, copy=False)
    date_end = fields.Datetime(readonly=True, copy=False)
    duration = fields.Char(compute='_compute_duration')

    _sql_constraints = [
        ('chunk_size_positive', 'CHECK(chunk_size > 0)', 'The chunk size must be at least 1.'),
    ]

    @api.constrains('model_id')
    def _check_model_id(self):
        for job in self:
            model = self.env.get(job.model_id.model)
            if model is None or model._abstract or not model._auto:
                raise ValidationError(_("%s has no records of its own and cannot be used here.", job.model_id.name))

    @api.depends('row_count', 'next_row')
    def _compute_progress(self):
        for job in self:
            job.progress = 100.0 * job.next_row / job.row_count if job.row_count else 0.0

    @api.depends('date_start', 'date_end')
    def _compute_duration(self):
        for job in self:
            if job.date_start and job.date_end:
                seconds = int((job.date_end - job.date_start).total_seconds())
                job.duration = '%d:%02d:%02d' % (seconds // 3600, seconds % 3600 // 60, seconds % 60)
            else:
                job.duration = False

    # ------------------------------------------------------------------
    # File reading and column mapping
    # ------------------------------------------------------------------

    def _iter_file_rows(self):
        """Yield (row_number, values) for the header row and every data row."""
        self.ensure_one()
        if not self.file:
            raise UserError(_("Upload a file first."))
        try:
            yield from spreadsheet.iter_rows(base64.b64decode(self.file), self.file_name)
        except spreadsheet.SpreadsheetError as e:
            raise UserError(str(e)) from e
        except UserError:
            raise
        except Exception as e:
            raise UserError(_("The file could not be read: %s", e)) from e

    @api.onchange('file', 'model_id')
    def _onchange_file(self):
        if self.file and self.file_name and self.name in (False, _('New import')):
            self.name = self.file_name
        if self.file and self.model_id:
            self.column_ids = [(5, 0, 0)] + [(0, 0, vals) for vals in self._read_columns()]
        else:
            self.column_ids = [(5, 0, 0)]
            self.row_count = 0

    def _read_columns(self):
        """Read the header, count the data rows and suggest a field for every column."""
        rows = self._iter_file_rows()
        header = next(rows, None)
        if not header:
            raise UserError(_("The file is empty."))
        self.row_count = sum(1 for _row in rows)
        model = self.env[self.model_id.model]
        return [{
            'sequence': index,
            'column_index': index,
            'header': title,
            'field_path': self._match_header(model, title),
        } for index, title in enumerate(header[1]) if title]

    @api.model
    def _match_header(self, model, header):
        """Return the field path for a column title, or False when nothing matches.

        Accepts technical names ("partner_id/id"), labels ("Customer / External ID")
        and the format of Odoo's own export.
        """
        if not header or header.startswith('__'):
            return False
        path = []
        for part in [p.strip() for p in header.split('/') if p.strip()]:
            special = SPECIAL_SUBFIELDS.get(part.lower())
            if special:
                path.append(special)
                break
            name = self._match_field(model, part)
            if not name:
                return False
            path.append(name)
            field = model._fields[name]
            if not field.relational:
                break
            model = self.env[field.comodel_name]
        return '/'.join(path)

    @api.model
    def _match_field(self, model, title):
        if title in model._fields:
            return title
        lowered = title.lower()
        by_label = {}
        for name, field in model._fields.items():
            if name in models.MAGIC_COLUMNS or not field.store and not field.inverse:
                continue
            for label in {field.string, field._description_string(model.env)}:
                if label and label.lower() == lowered:
                    by_label.setdefault(name, field)
        if not by_label:
            return None
        # Prefer editable stored fields when several fields share a label
        return sorted(by_label, key=lambda n: (by_label[n].readonly, not by_label[n].store, n))[0]

    def _check_columns(self):
        """Validate every mapped field path; return (paths, column indexes)."""
        self.ensure_one()
        model = self.env[self.model_name]
        columns = self.column_ids.filtered('field_path').sorted('sequence')
        if not columns:
            raise UserError(_("Map at least one column to a field."))
        paths = [c.field_path.strip() for c in columns]
        duplicates = {p for p in paths if paths.count(p) > 1}
        if duplicates:
            raise UserError(_("These fields are mapped to more than one column: %s", ', '.join(sorted(duplicates))))
        for column in columns:
            error = column._path_error(model)
            if error:
                raise UserError(_("Column \"%(column)s\": %(error)s", column=column.header, error=error))
        if self.match_field_id and self.match_field_id.name not in paths:
            raise UserError(_("The file has no column mapped to %s, which is used to match existing records.",
                              self.match_field_id.field_description))
        return paths, columns.mapped('column_index')

    # ------------------------------------------------------------------
    # Buttons
    # ------------------------------------------------------------------

    def action_start(self):
        return self._start(test_mode=False)

    def action_test(self):
        return self._start(test_mode=True)

    def _start(self, test_mode):
        self.ensure_one()
        if self.state != 'draft':
            raise UserError(_("Only draft imports can be started."))
        if not self.column_ids:
            self.column_ids = [(0, 0, vals) for vals in self._read_columns()]
        elif not self.row_count:
            self.row_count = sum(1 for _row in self._iter_file_rows()) - 1
        self._check_columns()
        self.env[self.model_name].check_access_rights('write' if self.mode == 'update' else 'create')
        self.write({
            'state': 'queued', 'test_mode': test_mode, 'next_row': 0, 'created_count': 0,
            'updated_count': 0, 'error_count': 0, 'message': False, 'error_file': False,
            'date_start': fields.Datetime.now(), 'date_end': False,
        })
        self.error_ids.unlink()
        inline_limit = int(self.env['ir.config_parameter'].sudo().get_param(
            'bulk_data_import_export.inline_rows', 500))
        if self.row_count <= inline_limit:
            # Small files finish within the request, no need to wait for the scheduler
            self._process(deadline=None, commit=False)
            return True
        self.env.ref('bulk_data_import_export.ir_cron_bulk_jobs')._trigger()
        return self._notify_action(_("The import runs in the background. You'll be notified when it is done."))

    def action_resume(self):
        for job in self.filtered(lambda j: j.state in ('failed', 'cancelled')):
            job.write({'state': 'queued', 'message': False})
        self.env.ref('bulk_data_import_export.ir_cron_bulk_jobs')._trigger()

    def action_cancel(self):
        self.filtered(lambda j: j.state in ('queued', 'running')).write({
            'state': 'cancelled', 'date_end': fields.Datetime.now(),
        })

    def action_reset(self):
        self.error_ids.unlink()
        self.write({
            'state': 'draft', 'test_mode': False, 'next_row': 0, 'created_count': 0, 'updated_count': 0,
            'error_count': 0, 'error_file': False, 'message': False, 'date_start': False, 'date_end': False,
        })

    def action_refresh(self):
        return True

    def action_download_errors(self):
        self.ensure_one()
        return self._download('error_file', 'error_file_name')

    def action_download_template(self):
        self.ensure_one()
        if not self.model_id:
            raise UserError(_("Choose the model to import into first."))
        self.template_file = base64.b64encode(self._build_template())
        return self._download('template_file', None, '%s_template.xlsx' % self.model_name.replace('.', '_'))

    # ------------------------------------------------------------------
    # Processing
    # ------------------------------------------------------------------

    @api.model
    def _cron_process_jobs(self):
        """Process queued imports and exports within the scheduler's time budget."""
        budget = float(self.env['ir.config_parameter'].sudo().get_param(
            'bulk_data_import_export.time_budget', 50))
        deadline = time.monotonic() + budget
        cron = self.env.ref('bulk_data_import_export.ir_cron_bulk_jobs')
        for model in ('bulk.import.job', 'bulk.export.job'):
            for job in self.env[model].search([('state', 'in', ('queued', 'running'))], order='id'):
                if time.monotonic() >= deadline:
                    cron._trigger()
                    return
                if not job._lock():
                    continue
                finished = job._run_safely(deadline)
                if not finished:
                    cron._trigger()
                    return

    def _run_safely(self, deadline):
        try:
            return self._process(deadline=deadline, commit=True)
        except Exception as e:
            _logger.exception("Bulk import %s failed", self.id)
            self.env.cr.rollback()
            self.write({'state': 'failed', 'message': str(e), 'date_end': fields.Datetime.now()})
            self._send_done_notification()
            self.env.cr.commit()
            return True

    def _import_model(self):
        ctx = dict(FAST_CONTEXT, lang=self.user_id.lang)
        model = self.env[self.model_name]
        if self.create_missing_relations:
            names = {name: True for name, f in model._fields.items() if f.type == 'many2one'}
            ctx['name_create_enabled_fields'] = ctx['name_create_enabled_fieds'] = names
        else:
            # Name lookups can be cached, except in models this import creates records in
            ctx['bulk_import_no_cache'] = tuple({model._name} | {
                f.comodel_name for f in model._fields.values() if f.type == 'one2many'})
        return self.env[self.model_name].with_user(self.user_id).with_company(self.company_id).with_context(ctx)

    def _process(self, deadline=None, commit=False):
        """Import rows from ``next_row`` on, one chunk per transaction.

        Returns True when the job is finished, False when it stopped at the
        deadline and must be resumed by the next scheduler run.
        """
        self.ensure_one()
        if self.state == 'queued':
            self.state = 'running'
        paths, indexes = self._check_columns()
        model = self._import_model()
        rows = self._iter_file_rows()
        header = next(rows)[1]
        rows = itertools.islice(rows, self.next_row, None)
        chunk, chunk_rows = [], 0
        # Once a chunk had errors the file is "dirty": check the next chunks
        # before saving, so bad rows are set aside without a second save
        precheck = bool(self.error_count)
        for group in self._group_records(rows, paths, indexes):
            chunk.append(group)
            chunk_rows += len(group)
            if chunk_rows >= self.chunk_size:
                precheck = self._import_chunk(model, paths, indexes, chunk, chunk_rows, precheck) or precheck
                chunk, chunk_rows = [], 0
                if commit:
                    self.env.cr.commit()
                if self._read_state() == 'cancelled':
                    return True
                if deadline and time.monotonic() >= deadline:
                    return False
        if chunk:
            self._import_chunk(model, paths, indexes, chunk, chunk_rows, precheck)
        self._finish(header)
        if commit:
            self.env.cr.commit()
        return True

    def _read_state(self):
        self.env.cr.execute('SELECT state FROM bulk_import_job WHERE id = %s', [self.id])
        return self.env.cr.fetchone()[0]

    @api.model
    def _group_records(self, rows, paths, indexes):
        """Group rows into records: like Odoo's importer, a row that only fills
        one2many columns continues the record above it (e.g. order lines)."""
        model = self.env[self.model_name]
        main = [i for i, path in zip(indexes, paths)
                if path.split('/')[0] in ('id', '.id') or model._fields[path.split('/')[0]].type != 'one2many']
        group = []
        for number, values in rows:
            if group and not any(values[i] for i in main if i < len(values)):
                group.append((number, values))
                continue
            if group:
                yield group
            group = [(number, values)]
        if group:
            yield group

    def _import_chunk(self, model, paths, indexes, groups, chunk_rows, precheck=False):
        """Import one chunk; return the number of failed records."""
        cr = self.env.cr
        if 'bulk_import_no_cache' in model.env.context:
            model = model.with_context(bulk_import_name_cache={})
        savepoint = cr.savepoint(flush=False) if self.test_mode else None
        try:
            existing = self._find_existing(model, paths, indexes, groups)
            errors, to_load = [], []
            for group, record_id in zip(groups, existing):
                if record_id and self.mode == 'create':
                    errors.append((group, _("A matching record already exists (mode is 'Create new records only').")))
                elif not record_id and self.mode == 'update':
                    errors.append((group, _("No existing record matches this row (mode is 'Update existing records only').")))
                else:
                    to_load.append((group, record_id))
            created = updated = 0
            for load_paths, batch in self._split_batches(paths, indexes, to_load):
                parts = [batch]
                if precheck:
                    suspects = self._precheck(model, load_paths, batch)
                    if suspects:
                        # Suspects go last: a row may only look bad because it
                        # refers to a record created by another row of the file
                        parts = [[e for i, e in enumerate(batch) if i not in suspects],
                                 [e for i, e in enumerate(batch) if i in suspects]]
                loaded, failed = [], []
                for part in parts:
                    part_loaded, part_failed = self._load_groups(model, load_paths, part)
                    loaded += part_loaded
                    failed += part_failed
                errors.extend(failed)
                updated += sum(1 for _group, _data, record_id in loaded if record_id)
                created += sum(1 for _group, _data, record_id in loaded if not record_id)
        finally:
            if savepoint:
                savepoint.close(rollback=True)
                self.env.invalidate_all()
                self.env.registry.reset_changes()
        if errors:
            self.env['bulk.import.error'].sudo().create([{
                'job_id': self.id,
                'row_number': group[0][0],
                'message': message,
                'row_values': json.dumps([values for _number, values in group]),
            } for group, message in errors])
        self.write({
            'next_row': self.next_row + chunk_rows,
            'created_count': self.created_count + created,
            'updated_count': self.updated_count + updated,
            'error_count': self.error_count + len(errors),
        })
        # Keep memory flat on very large files
        self.env.flush_all()
        self.env.invalidate_all()
        return len(errors)

    def _precheck(self, model, paths, batch):
        """Run only the conversion step of Model.load() (no record is saved)
        and return the positions of the batch entries it rejects."""
        data, owner = self._flatten(batch)
        messages = []
        records = model.with_context(import_cache=LRU(1024))
        fixed = [fix_import_export_id_paths(path) for path in paths]
        savepoint = self.env.cr.savepoint(flush=False)
        try:
            for _converted in records._convert_records(
                    records._extract_records(fixed, data, log=messages.append), log=messages.append):
                pass
        except Exception:
            return set()
        finally:
            savepoint.close(rollback=True)
            if self.create_missing_relations:
                self.env.invalidate_all()
        return set(self._errors_by_position(messages, owner))

    @api.model
    def _flatten(self, batch):
        data, owner = [], []
        for position, (_group, rows, _record_id) in enumerate(batch):
            data.extend(rows)
            owner.extend([position] * len(rows))
        return data, owner

    def _errors_by_position(self, messages, owner):
        bad = {}
        for message in messages:
            if message.get('type') != 'error':
                continue
            rows = message.get('rows') or {}
            if 'from' in rows:
                for index in range(rows['from'], rows['to'] + 1):
                    if 0 <= index < len(owner):
                        bad.setdefault(owner[index], []).append(self._message_text(message))
        return bad

    def _find_existing(self, model, paths, indexes, groups):
        """Return the id of the existing record for each group (or False), with one query per chunk."""
        def cell(group, path):
            index = indexes[paths.index(path)]
            values = group[0][1]
            return values[index] if index < len(values) else ''

        if self.match_field_id:
            key = self.match_field_id.name
            values = {cell(g, key) for g in groups} - {''}
            if model._fields[key].type == 'integer':
                values = {int(v) for v in values if v.lstrip('-').isdigit()}
            found = {}
            for record in model.with_context(active_test=False).search_read([(key, 'in', list(values))], [key]):
                found.setdefault(str(record[key]), record['id'])
            return [found.get(cell(g, key), False) for g in groups]
        if 'id' in paths:
            xmlids = {cell(g, 'id') for g in groups} - {''}
            found = {}
            for xmlid in xmlids:
                module, _sep, name = xmlid.rpartition('.')
                found[xmlid] = (module or '__import__', name)
            data = self.env['ir.model.data'].sudo().search_read(
                [('model', '=', self.model_name), ('name', 'in', [n for _m, n in found.values()])],
                ['module', 'name', 'res_id'])
            by_key = {(d['module'], d['name']): d['res_id'] for d in data}
            return [by_key.get(found.get(cell(g, 'id'), (None, None)), False) for g in groups]
        if '.id' in paths:
            ids = {int(cell(g, '.id')) for g in groups if cell(g, '.id').isdigit()}
            existing = set(model.with_context(active_test=False).browse(ids).exists().ids)
            return [int(cell(g, '.id')) if cell(g, '.id').isdigit() and int(cell(g, '.id')) in existing else False
                    for g in groups]
        return [False] * len(groups)

    def _split_batches(self, paths, indexes, to_load):
        """Yield (field paths, [(group, data, record_id)]) ready for Model.load().

        With "skip empty cells", rows are batched by which cells they fill so
        empty cells are left out of the load instead of clearing values."""
        use_db_id = bool(self.match_field_id)
        batches = {}
        for group, record_id in to_load:
            data = [[values[i] if i < len(values) else '' for i in indexes] for _number, values in group]
            if self.ignore_empty and len(group) == 1:
                keep = tuple(i for i, value in enumerate(data[0]) if value)
            else:
                keep = tuple(range(len(paths)))
            data = [[row[i] for i in keep] for row in data]
            load_paths = [paths[i] for i in keep]
            if use_db_id:
                load_paths = load_paths + ['.id']
                data = [row + [str(record_id) if record_id and n == 0 else ''] for n, row in enumerate(data)]
            batches.setdefault(tuple(load_paths), []).append((group, data, record_id))
        for load_paths, batch in batches.items():
            yield list(load_paths), batch

    def _load_groups(self, model, paths, batch):
        """Load records with Model.load(); never let one bad row block the others.

        Rows reported in load()'s messages are set aside and the rest is loaded
        again. When an error cannot be tied to a row, the batch is split in two
        until the bad record is isolated, so a few bad rows cost only a few
        extra load() calls even in a big chunk.
        Returns (loaded entries, [(group, error message)]).
        """
        loaded, errors = [], []
        pending = list(batch)
        while pending:
            data, owner = self._flatten(pending)
            try:
                with self.env.cr.savepoint():
                    result = model.load(paths, data)
            except Exception as e:
                self.env.invalidate_all()
                result = {'ids': False, 'messages': [{'type': 'error', 'message': str(e)}]}
            messages = [m for m in result['messages'] if m.get('type') == 'error']
            if not messages and result['ids'] is not False:
                loaded.extend(pending)
                return loaded, errors
            bad = self._errors_by_position(messages, owner)
            if not bad:
                text = ' | '.join(dict.fromkeys(self._message_text(m) for m in messages)) or _("Unknown error")
                if len(pending) == 1:
                    errors.append((pending[0][0], text))
                    return loaded, errors
                half = len(pending) // 2
                for part in (pending[:half], pending[half:]):
                    part_loaded, part_errors = self._load_groups(model, paths, part)
                    loaded.extend(part_loaded)
                    errors.extend(part_errors)
                return loaded, errors
            errors.extend((pending[position][0], ' | '.join(dict.fromkeys(texts))) for position, texts in bad.items())
            pending = [entry for position, entry in enumerate(pending) if position not in bad]
        return loaded, errors

    @api.model
    def _message_text(self, message):
        text = str(message.get('message') or '')
        field = message.get('field_name') or message.get('field')
        if field and field not in text:
            text = '%s: %s' % (field, text)
        return text.strip()

    def _finish(self, header):
        values = {'state': 'done', 'date_end': fields.Datetime.now()}
        if self.error_ids:
            values['error_file'] = base64.b64encode(self._build_error_report(header))
            values['error_file_name'] = '%s_errors.xlsx' % (self.file_name or 'import').rsplit('.', 1)[0]
        self.write(values)
        self._send_done_notification()

    def _send_done_notification(self):
        if self.state == 'failed':
            kind, text = 'danger', _("Import \"%s\" failed: %s", self.name, self.message)
        elif self.test_mode:
            kind = 'warning' if self.error_count else 'success'
            text = _("Test of \"%(name)s\" finished: %(ok)s rows OK, %(errors)s rows with errors.",
                     name=self.name, ok=self.created_count + self.updated_count, errors=self.error_count)
        else:
            kind = 'warning' if self.error_count else 'success'
            text = _("Import \"%(name)s\" finished: %(created)s created, %(updated)s updated, %(errors)s failed.",
                     name=self.name, created=self.created_count, updated=self.updated_count, errors=self.error_count)
        self._bus_notify(_("Bulk import"), text, kind, sticky=bool(self.error_count))

    def _build_error_report(self, header):
        """The failed rows exactly as in the file, plus the reason in the last column.

        The extra columns start with "__", which the importer ignores, so the
        user can fix the rows and import this same file again.
        """
        width = max([len(header)] + [len(v) for e in self.error_ids for v in json.loads(e.row_values)])
        titles = list(header) + [''] * (width - len(header)) + ['__row', '__error']
        output = io.BytesIO()
        writer = spreadsheet.XlsxWriter(output, titles, sheet_name='Failed rows')
        red = writer.book.add_format({'font_color': '#B71C1C'})
        writer.sheet.set_column(width + 1, width + 1, 80)
        for error in self.error_ids.sorted('row_number'):
            for offset, values in enumerate(json.loads(error.row_values)):
                values = list(values) + [''] * (width - len(values))
                writer.write_row(values + [error.row_number + offset])
                if offset == 0:
                    writer.sheet.write_string(writer.row - 1, width + 1, error.message, red)
        writer.close()
        return output.getvalue()

    def _build_template(self):
        model = self.env[self.model_name]
        importable = []
        for name, field in sorted(model._fields.items()):
            if name in models.MAGIC_COLUMNS or field.type in ('one2many', 'binary') or not field.store:
                continue
            if field.readonly and not field.inverse or field.compute and not field.inverse:
                continue
            importable.append((name, field))
        defaults = model.default_get([n for n, f in importable if f.required])
        required = [(n, f) for n, f in importable if f.required and n not in defaults]
        columns = [('id', None)] + required
        if 'name' in model._fields and 'name' not in [n for n, _f in required]:
            columns.insert(1, ('name', model._fields['name']))
        output = io.BytesIO()
        writer = spreadsheet.XlsxWriter(output, [n for n, _f in columns],
                                        sheet_name='Import')
        for col, (name, field) in enumerate(columns):
            note = _("External ID: any unique code. Importing again with the same code updates the record.") \
                if field is None else self._field_note(field)
            writer.sheet.write_comment(0, col, note, {'x_scale': 2, 'y_scale': 1.5})
        sheet = writer.book.add_worksheet('All fields')
        bold = writer.book.add_format({'bold': True, 'bg_color': '#E8EAF6'})
        for col, title in enumerate([_('Column title'), _('Label'), _('Type'), _('Required'), _('Notes')]):
            sheet.write_string(0, col, title, bold)
        for row, (name, field) in enumerate(importable, 1):
            sheet.write_row(row, 0, [name, field._description_string(self.env),
                                     field.type, 'yes' if field.required else '', self._field_note(field)])
        sheet.set_column(0, 1, 30)
        sheet.set_column(4, 4, 90)
        writer.close()
        return output.getvalue()

    def _field_note(self, field):
        if field.type == 'selection':
            values = field._description_selection(self.env)
            return _("One of: %s", ', '.join('%s (%s)' % (label, key) for key, label in values))
        if field.type == 'many2one':
            return _("Name of the %s, or its External ID in a column titled \"%s/id\".",
                     self.env[field.comodel_name]._description, field.name)
        if field.type == 'many2many':
            return _("Names separated by commas, or External IDs in a column titled \"%s/id\".", field.name)
        if field.type == 'date':
            return _("Date, for example 2026-12-31.")
        if field.type == 'datetime':
            return _("Date and time, for example 2026-12-31 14:30:00.")
        if field.type == 'boolean':
            return _("True / False (also yes / no, 1 / 0).")
        return field.type
