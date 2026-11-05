"""Проверки двусторонней географии, журнала и полной выгрузки без Qt."""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from openpyxl import load_workbook
from geography_filters import resolve_geography
from database import rebuild_financing
from nir_operations import (prepare_database, read_rows, save_nir, delete_nir,
                            undo_nir_change)
from analysis_reports import build_report
from report_export import write_all_nir_excel


class NewFeatureTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / 'test.sqlite'
        with sqlite3.connect(self.path) as conn:
            conn.executescript('''
                CREATE TABLE vuz(codvuz INTEGER PRIMARY KEY, z1 TEXT, z1full TEXT, z2 TEXT,
                    region TEXT, city TEXT, status TEXT, obl INTEGER, oblname TEXT, gr_ved TEXT, prof TEXT);
                CREATE TABLE grnti(codrub INTEGER PRIMARY KEY, rubrika TEXT);
                CREATE TABLE nir(id INTEGER PRIMARY KEY AUTOINCREMENT,codvuz INTEGER,rnw TEXT,
                    f1 TEXT,z2 TEXT,f6 TEXT,f10 TEXT,f2 TEXT,f7 TEXT,f18 INTEGER);
                CREATE TABLE financing(codvuz INTEGER PRIMARY KEY,z2 TEXT,z3 INTEGER,z18 INTEGER,numworks INTEGER);
                CREATE TABLE validation_log(id INTEGER PRIMARY KEY,severity TEXT,table_name TEXT,row_ref TEXT,message TEXT);
                INSERT INTO vuz VALUES(1,'Вуз 1','','В1','Центральный','Москва','','1','Москва','','');
                INSERT INTO vuz VALUES(2,'Вуз 2','','В2','Центральный','Москва','','1','Москва','','');
                INSERT INTO vuz VALUES(3,'Вуз 3','','В3','Сибирский','Томск','','2','Томская область','','');
                INSERT INTO grnti VALUES(20,'Информатика'),(34,'Биология');
                INSERT INTO nir VALUES(1,1,'001','П','В1','Иванов','34.10.10; 20.20.20','Работа 1','профессор',100);
                INSERT INTO nir VALUES(2,2,'002','Ф','В2','Петров','34.10.10','Работа 2','доцент',200);
                INSERT INTO nir VALUES(3,3,'003','Р','В3','Сидоров','20.20.20','Работа 3','доцент',300);
            ''')
            rebuild_financing(conn)
            conn.commit()
        prepare_database(self.path)

    def test_university_autofills_city_subject_region(self):
        chosen = resolve_geography(self.path, {'codvuz':3}, changed='codvuz')
        self.assertEqual(chosen, {'region':'Сибирский','oblname':'Томская область',
                                  'city':'Томск','codvuz':3})

    def test_city_autofills_parent_levels_without_forcing_university(self):
        chosen = resolve_geography(self.path, {'city':'Москва'}, changed='city')
        self.assertEqual(chosen, {'region':'Центральный','oblname':'Москва',
                                  'city':'Москва','codvuz':None})

    def test_changing_region_clears_incompatible_city_and_vuz(self):
        chosen = resolve_geography(self.path,
            {'region':'Сибирский','oblname':'Москва','city':'Москва','codvuz':1}, changed='region')
        self.assertEqual(chosen['region'], 'Сибирский')
        self.assertIsNone(chosen['codvuz'])
        self.assertIsNone(chosen['city'])

    def test_undo_delete_restores_exact_snapshot_and_financing(self):
        old = read_rows('SELECT * FROM nir WHERE id=1', path=self.path)[0]
        delete_nir(1, path=self.path, expected=old)
        deletion = read_rows("SELECT id FROM change_log WHERE operation='DELETE' ORDER BY id DESC LIMIT 1", path=self.path)[0]['id']
        self.assertEqual(len(read_rows('SELECT * FROM nir WHERE id=1', path=self.path)), 0)
        self.assertIn('восстановлена', undo_nir_change(deletion, path=self.path))
        self.assertEqual(read_rows('SELECT * FROM nir WHERE id=1', path=self.path)[0], old)
        self.assertEqual(read_rows('SELECT z3,numworks FROM financing WHERE codvuz=1', path=self.path)[0],
                         {'z3':100,'numworks':1})
        reason = read_rows('SELECT reason FROM change_log ORDER BY id DESC LIMIT 1', path=self.path)[0]['reason']
        self.assertIn(f'№{deletion}', reason)

    def test_undo_insert_deletes_only_untouched_row(self):
        values = {'codvuz':1, 'rnw':'007','f1':'П','f6':'Иванов',
                  'f10':'20.20.20','f2':'Новая работа','f7':'доцент','f18':77}
        row_id = save_nir(values, path=self.path)
        insertion = read_rows("SELECT id FROM change_log WHERE row_id=? AND operation='INSERT' ORDER BY id DESC LIMIT 1",
                              (row_id,), path=self.path)[0]['id']
        self.assertIn('удалена', undo_nir_change(insertion, path=self.path))
        self.assertEqual(read_rows('SELECT * FROM nir WHERE id=?', (row_id,), path=self.path), [])
        with self.assertRaisesRegex(ValueError,'уже менялась'):
            undo_nir_change(insertion, path=self.path)

    def test_undo_insert_rejects_later_edits(self):
        values = {'codvuz':1, 'rnw':'007','f1':'П','f6':'Иванов',
                  'f10':'20.20.20','f2':'Новая работа','f7':'доцент','f18':77}
        row_id = save_nir(values, path=self.path)
        insertion = read_rows("SELECT id FROM change_log WHERE row_id=? AND operation='INSERT' ORDER BY id DESC LIMIT 1",
                              (row_id,), path=self.path)[0]['id']
        save_nir(dict(values, f18=99), row_id, path=self.path)
        with self.assertRaisesRegex(ValueError,'уже менялась'):
            undo_nir_change(insertion, path=self.path)
        self.assertEqual(read_rows('SELECT f18 FROM nir WHERE id=?', (row_id,), path=self.path)[0]['f18'],99)

    def test_rubric_applies_to_both_codes_and_reports(self):
        result = build_report('financing', path=self.path, rubric='20')
        self.assertEqual(result.source_count, 2)
        self.assertEqual(result.totals['planned'], 400)
        moscow = build_report('vuz', {'city':'Москва'}, path=self.path, rubric='20')
        self.assertEqual(moscow.source_count, 1)
        with self.assertRaises(ValueError):
            build_report('vuz', path=self.path, rubric='20 OR TRUE')

    def test_export_all_ignores_selected_filters_and_keeps_codes_as_text(self):
        filename = self.path.with_suffix('.xlsx')
        count = write_all_nir_excel(filename, db_path=self.path)
        self.assertEqual(count, 3)
        workbook = load_workbook(filename)
        sheet = workbook['Все НИР']
        self.assertEqual(sheet['B6'].value, 1)
        self.assertEqual(sheet['C6'].value, '001')
        self.assertEqual(sheet['C6'].number_format, '@')
        self.assertEqual(sheet['G6'].value, '34.10.10; 20.20.20')
        self.assertEqual(sheet['J8'].value, 300)
        self.assertEqual(sheet.freeze_panes, 'D6')


if __name__ == '__main__':
    unittest.main()
