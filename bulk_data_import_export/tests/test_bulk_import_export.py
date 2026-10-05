import base64
import csv
import io
import time
from unittest.mock import patch

from odoo.addons.bulk_data_import_export.tools import spreadsheet
from odoo.exceptions import UserError, ValidationError
from odoo.tests import Form, TransactionCase, tagged


def csv_file(rows):
    output = io.StringIO()
    csv.writer(output).writerows(rows)
    return base64.b64encode(output.getvalue().encode())


def xlsx_file(rows):
    output = io.BytesIO()
    writer = spreadsheet.XlsxWriter(output, rows[0])
    for row in rows[1:]:
        writer.write_row(row)
    writer.close()
    return base64.b64encode(output.getvalue())


def read_back(content, filename):
    return [values for _number, values in spreadsheet.iter_rows(base64.b64decode(content), filename)]


@tagged('post_install', '-at_install')
class TestBulkImportExport(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.partner_model = cls.env.ref('base.model_res_partner')
        cls.Job = cls.env['bulk.import.job']
        cls.tag = 'BULKTEST'

    def _job(self, rows, filename='partners.csv', **values):
        content = xlsx_file(rows) if filename.endswith('.xlsx') else csv_file(rows)
        job = self.Job.create(dict({'model_id': self.partner_model.id, 'file': content, 'file_name': filename},
                                   **values))
        return job

    def _partners(self):
        return self.env['res.partner'].search([('ref', '=like', self.tag + '%')])

    def test_bad_rows_do_not_stop_the_import(self):
        job = self._job([
            ['Name', 'ref', 'country_id', 'type'],
            ['Good One', 'BULKTEST-1', 'Pakistan', 'contact'],
            ['Bad Country', 'BULKTEST-2', 'Atlantis', 'contact'],
            ['Good Two', 'BULKTEST-3', '', 'invoice'],
            ['Bad Type', 'BULKTEST-4', '', 'spaceship'],
            ['Good Three', 'BULKTEST-5', 'Pakistan', ''],
        ], chunk_size=10)
        job.action_start()
        self.assertEqual(job.state, 'done')
        self.assertEqual(job.created_count, 3)
        self.assertEqual(job.error_count, 2)
        self.assertEqual(sorted(self._partners().mapped('name')), ['Good One', 'Good Three', 'Good Two'])
        self.assertEqual(job.error_ids.mapped('row_number'), [3, 5])
        report = read_back(job.error_file, job.error_file_name)
        self.assertEqual(report[0], ['Name', 'ref', 'country_id', 'type', '__row', '__error'])
        self.assertEqual([r[0] for r in report[1:]], ['Bad Country', 'Bad Type'])
        self.assertTrue(all(r[5] for r in report[1:]))

    def test_error_report_can_be_imported_again(self):
        job = self._job([['name', 'ref', 'type'], ['Fix me', 'BULKTEST-9', 'spaceship']])
        job.action_start()
        rows = read_back(job.error_file, job.error_file_name)
        rows[1][2] = 'contact'
        again = self._job(rows)
        again.action_start()
        self.assertFalse(any(again.column_ids.filtered(lambda c: c.header.startswith('__')).mapped('field_path')))
        self.assertEqual((again.created_count, again.error_count), (1, 0))

    def test_chunks_and_resume(self):
        rows = [['name', 'ref']] + [['Partner %s' % i, 'BULKTEST-%03d' % i] for i in range(7)]
        job = self._job(rows, chunk_size=2)
        job.column_ids = [(0, 0, vals) for vals in job._read_columns()]
        job.state = 'queued'
        finished = job._process(deadline=time.monotonic() - 1)
        self.assertFalse(finished)
        self.assertEqual((job.state, job.next_row, job.created_count), ('running', 2, 2))
        self.assertTrue(job._process(deadline=None))
        self.assertEqual((job.state, job.next_row, job.created_count), ('done', 7, 7))
        self.assertEqual(len(self._partners()), 7)

    def test_update_by_match_field_keeps_empty_cells(self):
        partner = self.env['res.partner'].create({'name': 'Old', 'ref': 'BULKTEST-U', 'phone': '111'})
        job = self._job([['ref', 'name', 'phone'], ['BULKTEST-U', 'New name', ''], ['BULKTEST-NONE', 'X', '']],
                        mode='update', match_field_id=self.env.ref('base.field_res_partner__ref').id)
        job.action_start()
        self.assertEqual((job.updated_count, job.created_count, job.error_count), (1, 0, 1))
        self.assertEqual((partner.name, partner.phone), ('New name', '111'))
        self.assertIn('No existing record', job.error_ids.message)

    def test_create_only_rejects_existing_external_id(self):
        self.env['ir.model.data'].create({'module': '__import__', 'name': 'bulk_p1', 'model': 'res.partner',
                                          'res_id': self.env['res.partner'].create({'name': 'Exists'}).id})
        job = self._job([['id', 'name'], ['bulk_p1', 'Again'], ['bulk_p2', 'Brand new']], mode='create')
        job.action_start()
        self.assertEqual((job.created_count, job.error_count), (1, 1))
        self.assertTrue(self.env.ref('__import__.bulk_p2', raise_if_not_found=False))

    def test_test_mode_saves_nothing(self):
        job = self._job([['name', 'ref', 'type'], ['A', 'BULKTEST-T1', 'contact'], ['B', 'BULKTEST-T2', 'nope']])
        job.action_test()
        self.assertEqual(job.state, 'done')
        self.assertEqual((job.created_count, job.error_count), (1, 1))
        self.assertFalse(self._partners())

    def test_one2many_lines_stay_with_their_record(self):
        job = self._job([
            ['name', 'ref', 'child_ids/name'],
            ['Company A', 'BULKTEST-C', 'Contact 1'],
            ['', '', 'Contact 2'],
            ['Company B', 'BULKTEST-D', 'Contact 3'],
        ], chunk_size=1)
        job.action_start()
        company = self._partners().filtered(lambda p: p.ref == 'BULKTEST-C')
        self.assertEqual(sorted(company.child_ids.mapped('name')), ['Contact 1', 'Contact 2'])
        self.assertEqual(job.created_count, 2)

    def test_unlocated_error_is_isolated_by_splitting(self):
        Partner = type(self.env['res.partner'])
        original = Partner.load

        def load(records, fields, data):
            if any('BOOM' in row for row in data):
                raise ValueError('boom')
            return original(records, fields, data)

        rows = [['name', 'ref']] + [['P%s' % i, 'BULKTEST-B%s' % i] for i in range(8)]
        rows[6][0] = 'BOOM'
        job = self._job(rows, chunk_size=100)
        with patch.object(Partner, 'load', load):
            job.action_start()
        self.assertEqual((job.created_count, job.error_count), (7, 1))
        self.assertEqual(job.error_ids.row_number, 7)
        self.assertIn('boom', job.error_ids.message)

    def test_dirty_file_still_links_rows_created_in_same_chunk(self):
        job = self._job([
            ['id', 'name', 'ref', 'parent_id/id', 'type'],
            ['bulk_bad', 'Bad', 'BULKTEST-0', '', 'spaceship'],
            ['bulk_x', 'Filler', 'BULKTEST-1', '', 'contact'],
            # Second chunk is pre-checked: the child refers to a parent created just above it
            ['bulk_parent', 'Parent', 'BULKTEST-2', '', 'contact'],
            ['bulk_child', 'Child', 'BULKTEST-3', 'bulk_parent', 'contact'],
            ['bulk_orphan', 'Orphan', 'BULKTEST-4', 'bulk_missing', 'contact'],
        ], chunk_size=2)
        job.action_start()
        self.assertEqual((job.created_count, job.error_count), (3, 2))
        child = self.env.ref('__import__.bulk_child')
        self.assertEqual(child.parent_id, self.env.ref('__import__.bulk_parent'))
        self.assertEqual(job.error_ids.mapped('row_number'), [2, 6])

    def test_name_cache_does_not_change_results(self):
        rows = [['name', 'ref', 'country_id']] + [['C%s' % i, 'BULKTEST-N%s' % i, 'Pakistan' if i % 2 else 'Germany']
                                                   for i in range(6)]
        job = self._job(rows, chunk_size=3)
        job.action_start()
        partners = self._partners().sorted('ref')
        self.assertEqual([p.country_id.code for p in partners], ['DE', 'PK', 'DE', 'PK', 'DE', 'PK'])

    def test_xlsx_with_labels_and_dates(self):
        job = self._job([['Name', 'Reference', 'Country / External ID'],
                         ['Excel Partner', 'BULKTEST-X', 'base.pk']], filename='partners.xlsx')
        job.action_start()
        self.assertEqual(job.column_ids.mapped('field_path'), ['name', 'ref', 'country_id/id'])
        partner = self._partners()
        self.assertEqual(partner.country_id, self.env.ref('base.pk'))

    def test_unknown_column_is_skipped_and_bad_path_rejected(self):
        job = self._job([['name', 'not a field'], ['Skip', 'x']])
        job.column_ids = [(0, 0, vals) for vals in job._read_columns()]
        self.assertEqual(job.column_ids.mapped('field_path'), ['name', False])
        job.column_ids[1].field_path = 'name/oops'
        with self.assertRaises(UserError):
            job.action_start()

    def test_export_and_round_trip(self):
        partners = self.env['res.partner'].create([
            {'name': 'Export %s' % i, 'ref': 'BULKTEST-E%s' % i, 'country_id': self.env.ref('base.pk').id}
            for i in range(5)])
        fields = self.env['ir.model.fields'].search([('model', '=', 'res.partner'),
                                                     ('name', 'in', ('name', 'ref', 'country_id'))])
        for file_format in ('xlsx', 'csv'):
            export = self.env['bulk.export.job'].create({
                'model_id': self.partner_model.id, 'domain': "[('ref', '=like', 'BULKTEST-E%')]",
                'field_ids': [(6, 0, fields.ids)], 'relations_as_xmlid': True, 'file_format': file_format,
                'chunk_size': 2,
            })
            export.action_export()
            self.assertEqual((export.state, export.exported_count), ('done', 5))
            rows = read_back(export.file, export.file_name)
            self.assertEqual(rows[0], ['id', 'name', 'country_id/id', 'ref'])
            self.assertEqual(len(rows), 6)
        # Edit the exported file and import it back: the same records are updated
        for row in rows[1:]:
            row[1] = row[1].replace('Export', 'Renamed')
        job = self._job(rows)
        job.action_start()
        self.assertEqual((job.updated_count, job.created_count, job.error_count), (5, 0, 0))
        self.assertTrue(all(name.startswith('Renamed') for name in partners.mapped('name')))

    def test_template(self):
        job = self.Job.create({'model_id': self.partner_model.id})
        job.action_download_template()
        rows = read_back(job.template_file, 'template.xlsx')
        self.assertEqual(rows[0][:2], ['id', 'name'])

    def test_model_choice(self):
        for model in ('bulk.import.job', 'bulk.export.job'):
            # The field domain must be valid for the model picker in the form
            self.assertTrue(self.env['ir.model'].search(self.env[model]._fields['model_id'].domain, limit=1))
            with self.assertRaises(ValidationError):
                self.env[model].create({'model_id': self.env['ir.model']._get('base').id})

    def test_form_keeps_column_titles(self):
        with Form(self.Job.with_context(default_file_name='partners.csv')) as form:
            form.model_id = self.partner_model
            form.file = csv_file([['Name', 'Reference'], ['Form Partner', 'BULKTEST-F']])
        job = form.save()
        self.assertEqual(job.column_ids.mapped('header'), ['Name', 'Reference'])
        self.assertEqual(job.row_count, 1)
