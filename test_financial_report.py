"""Независимые проверки финансовой аналитики без графического интерфейса."""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from openpyxl import load_workbook
from analysis_reports import REPORTS, build_report, percent
from report_export import write_excel_report
from financial_checks import inspect_financing


class FinancialReportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "finance.sqlite"
        with sqlite3.connect(self.path) as conn:
            conn.executescript("""
                CREATE TABLE vuz (codvuz INTEGER PRIMARY KEY,z1 TEXT,z2 TEXT,region TEXT,oblname TEXT,city TEXT);
                CREATE TABLE nir (id INTEGER PRIMARY KEY,codvuz INTEGER,f18 INTEGER,f1 TEXT,f10 TEXT);
                CREATE TABLE financing (codvuz INTEGER PRIMARY KEY,z18 INTEGER);
                CREATE TABLE grnti (codrub INTEGER,rubrika TEXT);
                INSERT INTO vuz VALUES(1,'Вуз А','А','Центральный','Москва','Москва');
                INSERT INTO vuz VALUES(2,'Вуз Б','Б','Центральный','Москва','Москва');
                INSERT INTO vuz VALUES(3,'Вуз В','В','Сибирский','Томская область','Томск');
                INSERT INTO nir VALUES(1,1,100,'Ф','20.10.10');
                INSERT INTO nir VALUES(2,1,200,'П','20.10.10');
                INSERT INTO nir VALUES(3,2,300,'Р','34.10.10');
                INSERT INTO nir VALUES(4,3,0,'Р','34.10.10');
                INSERT INTO financing VALUES(1,150);
                INSERT INTO financing VALUES(2,60);
                INSERT INTO financing VALUES(3,0);
            """)

    def test_financial_summary_does_not_multiply_fact(self):
        for group in ('region', 'subject', 'city', 'vuz'):
            with self.subTest(group=group):
                r = build_report('financing', path=self.path, group_by=group)
                self.assertEqual(r.source_count, 4)
                self.assertEqual(r.totals['universities'], 3)
                self.assertEqual(r.totals['count'], 4)
                self.assertEqual(r.totals['planned'], 600)
                self.assertEqual(r.totals['actual'], 210)
                self.assertEqual(r.totals['execution'], 35.0)
        subject = build_report('financing', path=self.path)
        moscow = next(row for row in subject.rows if 'Москва' in row['group'])
        self.assertEqual((moscow['universities'], moscow['count'], moscow['planned'], moscow['actual'], moscow['execution']),
                         (2, 3, 600, 210, 35.0))
        tomsk = next(row for row in subject.rows if 'Томская' in row['group'])
        self.assertIsNone(tomsk['execution'])

    def test_filtered_finances_recalculate_percent_on_selected_subset(self):
        selected = build_report('financing', {'codvuz': 1}, path=self.path)
        self.assertEqual(selected.totals['count'], 2)
        self.assertEqual(selected.totals['planned'], 300)
        self.assertEqual(selected.totals['actual'], 150)
        self.assertEqual(selected.totals['execution'], 50.0)
        empty = build_report('financing', {'city': 'Несуществующий город'}, path=self.path)
        self.assertEqual((empty.source_count, empty.rows, empty.totals['count']), (0, [], 0))
        self.assertIsNone(empty.totals['execution'])
        with self.assertRaises(ValueError):
            build_report('financing', path=self.path, group_by='DROP TABLE financing')

    def test_unknown_university_does_not_inflate_execution_percent(self):
        with sqlite3.connect(self.path) as conn:
            conn.execute("INSERT INTO nir VALUES(5,NULL,100,'П','20.10.10')")
            conn.execute("INSERT INTO nir VALUES(6,999,50,'П','20.10.10')")
        report = build_report('financing', path=self.path)
        unknown = next(row for row in report.rows if row['group'] == 'Вуз не определён')
        self.assertEqual((unknown['count'], unknown['planned'], unknown['actual'], unknown['execution']),
                         (2, 150, None, None))
        self.assertEqual(report.totals['count'], 6)
        self.assertEqual(report.totals['planned'], 750)
        self.assertEqual(report.totals['execution'], 35.0)

    def test_financial_check_detects_disagreements_without_changing_db(self):
        with sqlite3.connect(self.path) as conn:
            conn.execute('ALTER TABLE financing ADD COLUMN z3 INTEGER DEFAULT 0')
            conn.execute('ALTER TABLE financing ADD COLUMN numworks INTEGER DEFAULT 0')
            conn.execute('UPDATE financing SET z3=CASE codvuz WHEN 1 THEN 300 WHEN 2 THEN 300 ELSE 0 END')
            conn.execute('UPDATE financing SET numworks=CASE codvuz WHEN 1 THEN 2 ELSE 1 END')
        self.assertEqual(inspect_financing(self.path)['issues'], [])
        with sqlite3.connect(self.path) as conn:
            conn.execute('UPDATE financing SET z3=999 WHERE codvuz=1')
        result = inspect_financing(self.path)
        self.assertEqual(len(result['issues']),1)
        self.assertIn('Вуз 1', result['issues'][0])
        self.assertEqual(result['works'],4)
        self.assertEqual(result['planned'],600)

    def test_zero_plan_is_not_zero_percent(self):
        self.assertIsNone(percent(0,0))
        self.assertIsNone(percent(None,200))
        self.assertEqual(percent(150,300),50.0)
        self.assertEqual(percent(400,200),200.0)

    def test_excel_exports_values_and_total_and_preserves_codes(self):
        report = build_report('financing', path=self.path)
        file = Path(self.temp.name) / 'Сводка.xlsx'
        write_excel_report(file, REPORTS['financing']['title'], REPORTS['financing']['columns'],
                           report.rows, report.totals, geography={'region':'Центральный'},
                           grouping='subject', source_count=report.source_count)
        wb = load_workbook(file)
        sh = wb.active
        self.assertEqual(sh['A2'].value, 'Количество уникальных НИР в выборке: 4')
        self.assertEqual(sh.cell(7+len(report.rows),4).value, 4)
        self.assertEqual(sh.cell(7+len(report.rows),7).value, .35)
        self.assertEqual(sh.freeze_panes, 'A7')
        workbook = Path(self.temp.name) / 'safe.xlsx'
        write_excel_report(workbook, 'Тест', (('group','Название'),), [{'group':'=1+1'}], None)
        test = load_workbook(workbook)
        self.assertEqual(test.active['A7'].data_type, 's')


if __name__ == '__main__':
    unittest.main()
