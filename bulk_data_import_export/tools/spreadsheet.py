"""Streaming readers and writers for CSV, XLS and XLSX files.

Readers yield ``(row_number, [str, ...])`` one row at a time, so even very
large files are never fully converted in memory. ``row_number`` is the row
number shown by the spreadsheet application (1-based), which is what users
need to find a bad line in their file.
"""
import csv
import datetime
import io

from odoo.tools import DEFAULT_SERVER_DATE_FORMAT, DEFAULT_SERVER_DATETIME_FORMAT

try:
    import openpyxl
except ImportError:
    openpyxl = None

try:
    import xlrd
    XLRD_READS_XLSX = int(xlrd.__VERSION__.split('.')[0]) < 2
except ImportError:
    xlrd = None
    XLRD_READS_XLSX = False

import xlsxwriter

CSV_DELIMITERS = ',;\t|'
XLSX_MAX_ROWS = 1048576


class SpreadsheetError(ValueError):
    pass


def file_extension(filename):
    return (filename or '').rsplit('.', 1)[-1].lower() if '.' in (filename or '') else ''


def format_cell(value):
    """Convert a Python cell value to the string format ``Model.load`` expects."""
    if value is None:
        return ''
    if isinstance(value, bool):
        return 'True' if value else 'False'
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else repr(value)
    if isinstance(value, datetime.datetime):
        if value.time() == datetime.time.min:
            return value.strftime(DEFAULT_SERVER_DATE_FORMAT)
        return value.strftime(DEFAULT_SERVER_DATETIME_FORMAT)
    if isinstance(value, datetime.date):
        return value.strftime(DEFAULT_SERVER_DATE_FORMAT)
    if isinstance(value, datetime.time):
        return value.strftime('%H:%M:%S')
    return str(value).strip()


def iter_rows(content, filename):
    """Yield ``(row_number, values)`` for every non-blank row of the file."""
    ext = file_extension(filename)
    if ext in ('csv', 'txt'):
        rows = _iter_csv(content)
    elif ext == 'xlsx':
        if openpyxl:
            rows = _iter_openpyxl(content)
        elif XLRD_READS_XLSX:
            rows = _iter_xlrd(content)
        else:
            raise SpreadsheetError("Reading .xlsx files needs the Python package openpyxl.")
    elif ext == 'xls':
        if not xlrd:
            raise SpreadsheetError("Reading .xls files needs the Python package xlrd.")
        rows = _iter_xlrd(content)
    else:
        raise SpreadsheetError("Unsupported file type. Use a .xlsx, .xls or .csv file.")
    for number, values in rows:
        if any(values):
            yield number, values


def _decode(content):
    for encoding in ('utf-8-sig', 'cp1252', 'latin-1'):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise SpreadsheetError("Could not detect the text encoding of the CSV file.")


def _iter_csv(content):
    text = _decode(content)
    sample = text[:65536]
    try:
        delimiter = csv.Sniffer().sniff(sample, delimiters=CSV_DELIMITERS).delimiter
    except csv.Error:
        delimiter = ','
    reader = csv.reader(io.StringIO(text, newline=''), delimiter=delimiter)
    for number, row in enumerate(reader, 1):
        yield number, [cell.strip() for cell in row]


def _iter_openpyxl(content):
    book = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    try:
        sheet = book.worksheets[0]
        for number, row in enumerate(sheet.iter_rows(values_only=True), 1):
            yield number, [format_cell(value) for value in row]
    finally:
        book.close()


def _iter_xlrd(content):
    book = xlrd.open_workbook(file_contents=content, on_demand=True)
    sheet = book.sheet_by_index(0)
    for rowx in range(sheet.nrows):
        values = []
        for cell in sheet.row(rowx):
            if cell.ctype == xlrd.XL_CELL_DATE:
                values.append(format_cell(datetime.datetime(*xlrd.xldate_as_tuple(cell.value, book.datemode))))
            elif cell.ctype == xlrd.XL_CELL_BOOLEAN:
                values.append(format_cell(bool(cell.value)))
            elif cell.ctype == xlrd.XL_CELL_ERROR:
                values.append('')
            else:
                values.append(format_cell(cell.value))
        yield rowx + 1, values


class XlsxWriter:
    """Write rows to an .xlsx file sequentially with low memory use.

    Starts a new sheet (repeating the header) when Excel's row limit is hit.
    """

    def __init__(self, path, header, sheet_name='Data'):
        self.book = xlsxwriter.Workbook(path, {'constant_memory': True, 'strings_to_urls': False})
        self.header = header
        self.sheet_name = sheet_name
        self.bold = self.book.add_format({'bold': True, 'bg_color': '#E8EAF6'})
        self.date = self.book.add_format({'num_format': 'yyyy-mm-dd'})
        self.datetime = self.book.add_format({'num_format': 'yyyy-mm-dd hh:mm:ss'})
        self.sheets = 0
        self._new_sheet()

    def _new_sheet(self):
        self.sheets += 1
        name = self.sheet_name if self.sheets == 1 else '%s %s' % (self.sheet_name, self.sheets)
        self.sheet = self.book.add_worksheet(name)
        self.sheet.freeze_panes(1, 0)
        for col, title in enumerate(self.header):
            self.sheet.write_string(0, col, title, self.bold)
            self.sheet.set_column(col, col, min(max(len(title) + 4, 12), 40))
        self.row = 1

    def write_row(self, values):
        if self.row >= XLSX_MAX_ROWS:
            self._new_sheet()
        for col, value in enumerate(values):
            if value is None or (isinstance(value, str) and not value):
                continue
            if isinstance(value, bool):
                self.sheet.write_boolean(self.row, col, value)
            elif isinstance(value, datetime.datetime):
                self.sheet.write_datetime(self.row, col, value, self.datetime)
            elif isinstance(value, datetime.date):
                self.sheet.write_datetime(self.row, col, value, self.date)
            elif isinstance(value, (int, float)):
                self.sheet.write_number(self.row, col, value)
            elif isinstance(value, bytes):
                self.sheet.write_string(self.row, col, value.decode(errors='replace'))
            else:
                self.sheet.write_string(self.row, col, str(value))
        self.row += 1

    def close(self):
        self.book.close()
