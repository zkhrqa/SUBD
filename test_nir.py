"""Проверки операций на изолированной базе и Qt без записи в рабочую БД."""
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import date
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6 import sip
from PyQt6.QtCore import QStandardPaths, Qt
from PyQt6.QtWidgets import QApplication, QDialog, QMessageBox
from openpyxl import load_workbook

from database import rebuild_financing, run_validation
from main import MainWindow
from nir_forms import ChangeLogDialog, NirDialog
from analysis_forms import AnalysisDialog
from analysis_reports import build_report
from funding_forms import FundingDialog
from funding_operations import approve_order, load_funding, preview_order
from report_export import TEST_COPY_PROPERTY
from nir_operations import delete_nir, duplicate_number_plan, enforce_nir_unique_key, repair_vuz_links, normalize_grnti_input, prepare_database, read_rows, save_actual_financing, save_nir


class NirTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "test.db"
        with closing(sqlite3.connect(self.path)) as conn:
            conn.executescript("""
                CREATE TABLE vuz(codvuz INTEGER PRIMARY KEY, z1 TEXT, z1full TEXT, z2 TEXT,
                    region TEXT, city TEXT, status TEXT, obl INTEGER, oblname TEXT, gr_ved TEXT, prof TEXT);
                CREATE TABLE grnti(codrub INTEGER PRIMARY KEY, rubrika TEXT);
                CREATE TABLE nir(id INTEGER PRIMARY KEY AUTOINCREMENT, codvuz INTEGER, rnw TEXT,
                    f1 TEXT, z2 TEXT, f6 TEXT, f10 TEXT, f2 TEXT, f7 TEXT, f18 INTEGER);
                CREATE TABLE financing(codvuz INTEGER PRIMARY KEY REFERENCES vuz(codvuz),
                    z2 TEXT, z3 INTEGER, z18 INTEGER, numworks INTEGER);
                CREATE TABLE validation_log(id INTEGER PRIMARY KEY, severity TEXT,
                    table_name TEXT, row_ref TEXT, message TEXT);
                INSERT INTO vuz VALUES(1,'Вуз один','','В1','Центральный','Москва','',1,'Москва','','');
                INSERT INTO vuz VALUES(2,'Вуз два','','В2','Сибирский','Томск','',2,'Томская область','','');
                INSERT INTO grnti VALUES(2,'Философия'),(20,'Информатика'),(34,'Биология'),(64,'Промышленность');
                INSERT INTO nir VALUES(1,1,'018','Ф','В1','Иванов И.И.','34.31.15; 20.23.25','Первая работа','профессор',100);
                INSERT INTO nir VALUES(2,1,'018','П','В1','Петров П.П.','02.15.51','Другая работа','доцент',200);
                INSERT INTO nir VALUES(3,2,'7','Р','В2','Сидоров С.С.','64.01.84 20.23.25','Третья работа','доцент',300);
            """)
            rebuild_financing(conn)
            conn.commit()
        self.backup = prepare_database(self.path)
        # Историю миграции проверяем отдельно; остальные тесты проверяют
        # только операции, которые выполняются в их собственных сценариях.
        with closing(sqlite3.connect(self.path)) as conn:
            conn.execute("DELETE FROM change_log")
            conn.commit()
        self.values = dict(codvuz=1, rnw=" 0 7 ", f1="П", f6="Иванов И.И.",
                           f10="34.31.15; 20.23.25", f2="Новая работа", f7="доцент", f18="450")

    def tearDown(self):
        # Явно удаляем Qt-деревья до удаления временной SQLite-базы.
        # Это исключает позднее уничтожение моделей циклическим GC Python.
        for widget in list(self.app.topLevelWidgets()):
            if not sip.isdeleted(widget):
                widget.close()
                sip.delete(widget)
        self.temporary.cleanup()

    def rows(self, sql, args=()):
        return read_rows(sql, args, path=self.path)

    def test_backup_keeps_original_and_is_not_overwritten(self):
        save_nir(self.values, path=self.path)
        prepare_database(self.path)
        with closing(sqlite3.connect(self.backup)) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM nir").fetchone()[0], 3)
            self.assertIsNone(conn.execute("SELECT name FROM sqlite_master WHERE name='change_log'").fetchone())

    def test_crud_financing_and_history_are_consistent(self):
        row_id = save_nir(self.values, path=self.path)
        row = self.rows("SELECT * FROM nir WHERE id=?", (row_id,))[0]
        self.assertEqual(row["rnw"], "07")
        self.assertEqual(row["z2"], "В1")
        self.assertEqual(self.rows("SELECT z3,numworks FROM financing WHERE codvuz=1")[0],
                         {"z3": 750, "numworks": 3})
        values = dict(self.values, f18="500", rnw="07")
        save_nir(values, row_id, path=self.path, expected=row)
        current = self.rows("SELECT * FROM nir WHERE id=?", (row_id,))[0]
        delete_nir(row_id, path=self.path, expected=current)
        self.assertEqual(self.rows("SELECT z3,numworks FROM financing WHERE codvuz=1")[0],
                         {"z3": 300, "numworks": 2})
        log = self.rows("SELECT * FROM change_log ORDER BY id")
        self.assertEqual([r["operation"] for r in log], ["INSERT", "UPDATE", "DELETE"])
        self.assertEqual(json.loads(log[1]["before_data"])["f18"], 450)
        self.assertEqual(json.loads(log[2]["before_data"])["f18"], 500)

    def test_duplicates_leading_zeros_and_existing_conflicts(self):
        save_nir(self.values, path=self.path)
        for number in ("07", "7", " 0 0 7 "):
            with self.assertRaises(ValueError):
                save_nir(dict(self.values, rnw=number), path=self.path)
        with self.assertRaises(ValueError):
            save_nir(dict(self.values, codvuz=2, rnw="07"), path=self.path)
        save_nir(dict(self.values, rnw="Р-07"), path=self.path)
        save_nir(dict(self.values, rnw="Р-7"), path=self.path)
        with self.assertRaises(ValueError):
            save_nir(dict(self.values, rnw="Р- 7"), path=self.path)
        with self.assertRaises(ValueError):
            save_nir(dict(self.values, rnw="018"), path=self.path)
        # При подготовке второй номер 018 уже заменён на 019.
        existing = self.rows("SELECT * FROM nir WHERE id=1")[0]
        save_nir(dict(existing, f2="Исправленное название"), 1, path=self.path, expected=existing)
        self.assertEqual(len(self.rows("SELECT * FROM nir WHERE rnw='018'")), 1)
        self.assertEqual(self.rows("SELECT rnw FROM nir WHERE id=2"), [{"rnw": "019"}])
        with self.assertRaises(ValueError):
            save_nir(dict(existing, rnw="019"), 1, path=self.path)
        save_nir(dict(existing, rnw="020"), 1, path=self.path)
        with closing(sqlite3.connect(self.path)) as conn:
            self.assertFalse(any("Повтор" in issue[3] for issue in run_validation(conn)))

    def test_failed_validation_leaves_no_changes(self):
        invalid = [dict(self.values, f10="34.31.15; 20.23.25; 02.15.51"),
                   dict(self.values, f10="99.11.11"), dict(self.values, f10="34.3a.15"),
                   dict(self.values, f18="1.5"), dict(self.values, f18="-1"),
                   dict(self.values, codvuz=999), dict(self.values, f6=""), dict(self.values, f7=" "),
                   dict(self.values, f2=""), dict(self.values, rnw=" ")]
        for values in invalid:
            with self.subTest(values=values), self.assertRaises(ValueError):
                save_nir(values, path=self.path)
        self.assertEqual(len(self.rows("SELECT * FROM nir")), 3)
        self.assertEqual(self.rows("SELECT * FROM change_log"), [])

    def test_unique_migration_keeps_backup_logs_changes_and_is_idempotent(self):
        path = Path(self.temporary.name) / "migration.db"
        with closing(sqlite3.connect(self.backup)) as source, closing(sqlite3.connect(path)) as target:
            source.backup(target)
        prepare_database(path)
        records = read_rows("SELECT id,rnw,f2,f18 FROM nir ORDER BY id", path=path)
        self.assertEqual([r['rnw'] for r in records], ['018', '019', '7'])
        log = read_rows("SELECT before_data,after_data FROM change_log", path=path)
        self.assertEqual(len(log), 1)
        self.assertEqual(json.loads(log[0]['before_data'])['rnw'], '018')
        self.assertEqual(json.loads(log[0]['after_data'])['rnw'], '019')
        backup = path.parent / 'backups' / 'migration_before_unique_nir_key.db'
        self.assertEqual([r['rnw'] for r in read_rows('SELECT rnw FROM nir ORDER BY id', path=backup)],
                         ['018', '018', '7'])
        self.assertEqual(enforce_nir_unique_key(path), [])
        self.assertEqual(len(read_rows('SELECT * FROM change_log', path=path)), 1)
        with closing(sqlite3.connect(path)) as conn:
            # Проверяем индекс независимо от прикладных триггеров.
            conn.execute('DROP TRIGGER nir_unique_insert')
            conn.execute('DROP TRIGGER nir_unique_update')
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("INSERT INTO nir(codvuz,rnw) VALUES(1,'019')")
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("UPDATE nir SET codvuz=1,rnw='019' WHERE id=3")
            conn.execute("INSERT INTO nir(codvuz,rnw) VALUES(NULL,'unknown')")
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("INSERT INTO nir(codvuz,rnw) VALUES(NULL,'unknown')")

    def test_number_plan_preserves_formats_and_separate_vuz_maxima(self):
        with closing(sqlite3.connect(':memory:')) as conn:
            conn.execute('CREATE TABLE nir(id INTEGER PRIMARY KEY,codvuz INTEGER,rnw TEXT)')
            conn.executemany('INSERT INTO nir VALUES(?,?,?)', [
                (1, 1, '007'), (2, 1, '007'), (3, 1, '007'), (4, 1, '034'),
                (5, 2, '007'), (6, 2, '007'), (7, 2, '09'),
                (8, 1, 'd-08'), (9, 1, 'd-08'), (10, 1, 'd-16'),
                (11, 1, '1.01'), (12, 1, '1.01'), (13, 1, '1.19'),
                (14, 1, 'f1'), (15, 1, 'f1'), (16, 1, 'f8'),
            ])
            changes = duplicate_number_plan(conn)
            self.assertEqual(changes, [
                (2, 1, '007', '035'), (3, 1, '007', '036'), (6, 2, '007', '010'),
                (9, 1, 'd-08', 'd-17'), (12, 1, '1.01', '1.20'), (15, 1, 'f1', 'f9'),
            ])

    def test_unique_migration_rolls_back_all_renumbering_on_failure(self):
        path = Path(self.temporary.name) / 'rollback.db'
        with closing(sqlite3.connect(self.backup)) as source, closing(sqlite3.connect(path)) as target:
            source.backup(target)
            target.execute("""INSERT INTO nir(codvuz,rnw,f1,z2,f6,f10,f2,f7,f18)
                              SELECT codvuz,rnw,f1,z2,f6,f10,f2,f7,f18 FROM nir WHERE id=3""")
            target.execute("""CREATE TRIGGER fail_second_change BEFORE UPDATE OF rnw ON nir
                              WHEN OLD.id=4 BEGIN SELECT RAISE(ABORT,'test failure'); END""")
            target.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            prepare_database(path)
        self.assertEqual([r['rnw'] for r in read_rows('SELECT rnw FROM nir ORDER BY id', path=path)],
                         ['018', '018', '7', '7'])
        self.assertEqual(read_rows('SELECT * FROM change_log', path=path), [])
        self.assertEqual(read_rows("SELECT name FROM sqlite_master WHERE name='nir_codvuz_rnw_unique'", path=path), [])
        with closing(sqlite3.connect(path)) as conn:
            conn.execute('DROP TRIGGER fail_second_change')
            conn.commit()
        prepare_database(path)
        self.assertEqual([r['rnw'] for r in read_rows('SELECT rnw FROM nir ORDER BY id', path=path)],
                         ['018', '019', '7', '8'])

    def test_database_rejects_duplicate_without_ui(self):
        with closing(sqlite3.connect(self.path)) as conn:
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("INSERT INTO nir(codvuz,rnw) VALUES(1,'018')")
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("INSERT INTO nir(codvuz,rnw) VALUES(1,'00 18')")
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("UPDATE nir SET codvuz=1,rnw='018' WHERE id=3")

    def test_concurrent_change_is_not_overwritten_or_deleted(self):
        original = self.rows("SELECT * FROM nir WHERE id=1")[0]
        save_nir(dict(original, f2="Другой пользователь"), 1, path=self.path)
        with self.assertRaises(ValueError):
            save_nir(original, 1, path=self.path, expected=original)
        with self.assertRaises(ValueError):
            delete_nir(1, path=self.path, expected=original)
        self.assertEqual(self.rows("SELECT f2 FROM nir WHERE id=1")[0]["f2"], "Другой пользователь")

    def test_transaction_rolls_back_when_recalculation_fails(self):
        with patch("nir_operations.rebuild_financing", side_effect=sqlite3.OperationalError("test failure")):
            with self.assertRaises(sqlite3.OperationalError):
                save_nir(self.values, path=self.path)
        self.assertEqual(len(self.rows("SELECT * FROM nir")), 3)
        self.assertEqual(self.rows("SELECT * FROM change_log"), [])

    def test_form_filter_sort_selection_and_read_only_financing(self):
        window = MainWindow(self.path)
        try:
            page = window.pages["nir"]
            self.assertTrue(page.view.isColumnHidden(page.field_names.index("id")))
            self.assertEqual([page.model.record(i).value("id") for i in range(3)], [1, 2, 3])
            page.view.sortByColumn(page.field_names.index("codvuz"), Qt.SortOrder.DescendingOrder)
            self.assertEqual(page.model.record(0).value("id"), 3)
            page.key_sort_button.click()
            self.assertEqual([page.model.record(i).value("id") for i in range(3)], [1, 2, 3])
            page.rubric.setCurrentIndex(page.rubric.findData("20"))
            self.assertEqual(page.model.rowCount(), 2)
            self.assertFalse(hasattr(page, "rubric_position"))
            page.clear_filter()
            page.geography["city"].setCurrentIndex(page.geography["city"].findData("Томск"))
            self.assertEqual(page.model.rowCount(), 1)
            dialog = NirDialog(self.path)
            dialog.vuz.setCurrentIndex(dialog.vuz.findData(1))
            self.assertEqual(dialog.rnw.text(), "")
            dialog.rnw.setText("020")
            dialog.position.setText("доцент")
            dialog.character.setCurrentIndex(dialog.character.findData("П"))
            dialog.title.setPlainText("Новая работа")
            dialog.leader.setText("Иванов И.И.")
            dialog.grnti_first.setText("34.31.15")
            dialog.money.setText("450")
            dialog.save()
            self.assertIsNotNone(dialog.saved_id)
            saved_number = dialog.rnw.text()
            with patch.object(QMessageBox, "information") as message:
                page.after_nir_save(dialog.saved_id)
                message.assert_called_once()
            self.assertEqual(page.selected_record()["id"], dialog.saved_id)
            self.assertIsNone(page.geography["city"].currentData())
            financing = window.pages["financing"]
            record = next(financing.model.record(i) for i in range(financing.model.rowCount())
                          if financing.model.record(i).value("codvuz") == 1)
            self.assertEqual(record.value("z3"), 750)
            self.assertEqual(record.value("numworks"), 3)
            log_dialog = ChangeLogDialog(self.path)
            self.assertIn("Новая работа", log_dialog.details.toPlainText())
            financing = window.pages["financing"]
            column = financing.field_names.index("z18")
            row = next(i for i in range(financing.model.rowCount())
                       if financing.model.record(i).value("codvuz") == 1)
            self.assertFalse(financing.model.setData(financing.model.index(row, column), "123", Qt.ItemDataRole.EditRole))
            # Изменение поискового поля применяет введённый фильтр заново.
            page.search_field.setCurrentIndex(page.search_field.findData("rnw"))
            page.search_edit.setText(saved_number)
            self.assertEqual(page.model.rowCount(), 1)
            page.search_field.setCurrentIndex(page.search_field.findData("f2"))
            self.assertEqual(page.model.rowCount(), 0)
            page.clear_search()
            page.clear_filter()
            page.view.sortByColumn(page.field_names.index("codvuz"), Qt.SortOrder.DescendingOrder)
            self.assertEqual(page.model.record(0).value("codvuz"), 2)
            page.select_record(dialog.saved_id)
            page.key_sort_button.click()
            self.assertEqual(page.selected_record()["id"], dialog.saved_id)
            self.assertEqual([page.model.record(i).value("rnw") for i in range(4)], ['018', '019', '020', '7'])
        finally:
            window.close()

    def test_delete_confirmation_and_ui_refresh(self):
        window = MainWindow(self.path)
        try:
            page = window.pages["nir"]
            page.select_record(3)
            with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.No):
                page.remove_nir()
            self.assertEqual(len(self.rows("SELECT * FROM nir")), 3)
            self.assertEqual(self.rows("SELECT * FROM change_log"), [])
            with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes):
                page.remove_nir()
            self.assertEqual(page.model.rowCount(), 2)
            self.assertEqual(window.pages["financing"].model.rowCount(), 1)
            self.assertEqual(self.rows("SELECT operation FROM change_log"), [{"operation": "DELETE"}])
        finally:
            window.close()

    def test_add_form_uses_manual_number_and_requires_explicit_character(self):
        dialog = NirDialog(self.path)
        self.assertIsNone(dialog.character.currentData())
        self.assertFalse(dialog.rnw.isReadOnly())
        self.assertTrue(dialog.note.isHidden())
        dialog.rnw.setText("БФУ-С/007")
        dialog.vuz.setCurrentIndex(dialog.vuz.findData(1))
        dialog.vuz.setCurrentIndex(dialog.vuz.findData(2))
        self.assertEqual(dialog.rnw.text(), "БФУ-С/007")
        dialog.vuz.setCurrentIndex(dialog.vuz.findData(1))
        dialog.title.setPlainText('Новая работа')
        dialog.leader.setText('Гон')
        dialog.position.setText('профессор')
        dialog.grnti_first.setText('34.31.15')
        with patch.object(QMessageBox, 'warning') as warning:
            dialog.save()
        self.assertIsNone(dialog.saved_id)
        self.assertIn('Выберите характер', warning.call_args.args[2])
        dialog.character.setCurrentIndex(dialog.character.findData('П'))
        dialog.save()
        row = self.rows('SELECT * FROM nir WHERE id=?', (dialog.saved_id,))[0]
        self.assertEqual(row['rnw'], 'БФУ-С/007')
        self.assertEqual(row['f6'], 'Гон')
        edit = NirDialog(self.path, row)
        self.assertEqual(edit.rnw.text(), 'БФУ-С/007')
        self.assertEqual(edit.character.currentData(), 'П')

    def test_two_add_forms_reject_duplicate_without_changing_entered_number(self):
        first, second = NirDialog(self.path), NirDialog(self.path)
        for dialog in (first, second):
            dialog.vuz.setCurrentIndex(dialog.vuz.findData(1))
            dialog.rnw.setText('С-007')
            dialog.character.setCurrentIndex(dialog.character.findData('П'))
            dialog.title.setPlainText('Новая работа')
            dialog.leader.setText('Иванов Иван')
            dialog.position.setText('доцент')
            dialog.grnti_first.setText('34.31.15')
        first.save()
        with patch.object(QMessageBox, 'warning') as warning:
            second.save()
        self.assertIsNone(second.saved_id)
        self.assertIn('уже есть', warning.call_args.args[2])
        self.assertEqual(second.rnw.text(), 'С-007')
        second.rnw.setText('С-008')
        second.save()
        self.assertEqual([r['rnw'] for r in self.rows('SELECT rnw FROM nir WHERE id IN (?,?) ORDER BY id',
                                                    (first.saved_id, second.saved_id))], ['С-007', 'С-008'])
        self.assertEqual(len(self.rows('SELECT * FROM change_log')), 2)

    def test_name_format_is_free_but_position_and_name_are_required(self):
        for index, name in enumerate(('Гон', 'Иванов Иван', 'Анна-Мария де ла Крус', '王明')):
            with self.subTest(name=name):
                row_id = save_nir(dict(self.values, rnw=f'ИМЯ-{index}', f6=name), path=self.path)
                self.assertEqual(self.rows('SELECT f6 FROM nir WHERE id=?', (row_id,))[0]['f6'], name)
        for field in ('f6', 'f7'):
            with self.subTest(field=field), self.assertRaises(ValueError):
                save_nir(dict(self.values, **{field: ' '}), path=self.path)

    def test_financing_save_rolls_back_all_rows_and_log_on_failure(self):
        with self.assertRaises(ValueError):
            save_actual_financing([(1, 123), (999, 456)], path=self.path)
        self.assertEqual(self.rows("SELECT z18 FROM financing WHERE codvuz=1"), [{"z18": 0}])
        self.assertEqual(self.rows("SELECT * FROM change_log"), [])

    def test_filter_button_applies_only_on_confirmation_and_cancel_restores_settings(self):
        window = MainWindow(self.path)
        try:
            page = window.pages["nir"]
            def cancel_draft():
                page.geography["city"].setCurrentIndex(page.geography["city"].findData("Томск"))
                self.assertEqual(page.model.rowCount(), 3)
                return QDialog.DialogCode.Rejected
            with patch.object(page.filter_dialog, "exec", side_effect=cancel_draft):
                page.filter_button.click()
            self.assertIsNone(page.geography["city"].currentData())
            self.assertEqual(page.model.filter(), "")
            self.assertEqual(page.filter_summary.text(), "Без фильтра · Строк: 3")

            def apply_draft():
                page.geography["city"].setCurrentIndex(page.geography["city"].findData("Томск"))
                self.assertEqual(page.model.rowCount(), 3)
                page.accept_filter()
                return page.filter_dialog.result()
            with patch.object(page.filter_dialog, "exec", side_effect=apply_draft):
                page.open_filter()
            self.assertEqual(page.model.rowCount(), 1)
            self.assertIn("Томск", page.filter_summary.toolTip())
            self.assertEqual(page.filter_summary.text(), "Фильтр применён · Строк: 1")

            def reset_then_cancel():
                self.assertEqual(page.geography["city"].currentData(), "Томск")
                page.filter_reset_button.click()
                self.assertEqual(page.model.rowCount(), 1)
                return QDialog.DialogCode.Rejected
            with patch.object(page.filter_dialog, "exec", side_effect=reset_then_cancel):
                page.open_filter()
            self.assertEqual(page.geography["city"].currentData(), "Томск")
            self.assertEqual(page.model.rowCount(), 1)

            def reset_then_apply():
                page.filter_reset_button.click()
                self.assertEqual(page.model.rowCount(), 1)
                page.accept_filter()
                return page.filter_dialog.result()
            with patch.object(page.filter_dialog, "exec", side_effect=reset_then_apply):
                page.open_filter()
            self.assertEqual(page.model.rowCount(), 3)
            self.assertEqual(page.filter_summary.text(), "Без фильтра · Строк: 3")
            self.assertFalse(page.editing_filter)
        finally:
            window.close()

    def test_unassigned_financing_group_includes_all_works_and_updates(self):
        with closing(sqlite3.connect(self.path)) as conn:
            conn.executemany("""INSERT INTO nir(codvuz,rnw,f1,z2,f6,f10,f2,f7,f18)
                                VALUES(?,?,'П','','Иванов И.И.','34.31.15','Неопределённая работа','доцент',?)""",
                             [(None, '031', 50), (31, '201', 60), (31, '202', 70), (31, '204', 80)])
            rebuild_financing(conn)
            conn.commit()
            self.assertEqual(conn.execute('PRAGMA foreign_key_check').fetchall(), [])
        group = self.rows("SELECT * FROM financing_display WHERE codvuz IS NULL")[0]
        self.assertEqual(group, {"codvuz": None, "z2": "Вуз не определён", "z3": 260, "z18": None, "numworks": 4})
        self.assertEqual(self.rows("SELECT SUM(numworks) AS n,SUM(z3) AS amount FROM financing_display"),
                         [{"n": 7, "amount": 860}])
        self.assertEqual(len(self.rows("SELECT * FROM vuz")), 2)
        window = MainWindow(self.path)
        try:
            page = window.pages["financing"]
            self.assertEqual(page.total_label.text(), "Итого НИР: 7")
            row = next(i for i in range(page.model.rowCount())
                       if page.model.record(i).value("z2") == "Вуз не определён")
            index = page.model.index(row, page.field_names.index("z18"))
            self.assertFalse(page.model.flags(index) & Qt.ItemFlag.ItemIsEditable)
            self.assertFalse(page.model.setData(index, 123))
            self.assertEqual(page.model.data(index), "—")
            page.search_field.setCurrentIndex(page.search_field.findData("z2"))
            page.search_edit.setText("Вуз не определён")
            self.assertEqual(page.total_label.text(), "Итого НИР: 4 (по отфильтрованным строкам)")
            page.clear_search()
            page.clear_filter()
            page.geography['city'].setCurrentIndex(page.geography['city'].findData('Москва'))
            self.assertEqual(page.total_label.text(), "Итого НИР: 2 (по отфильтрованным строкам)")
            page.clear_filter()
            missing = self.rows("SELECT * FROM nir WHERE codvuz IS NULL")[0]
            save_nir(dict(missing, codvuz=1), missing['id'], path=self.path, expected=missing)
            window.reload_database_views()
            self.assertEqual(page.total_label.text(), "Итого НИР: 7")
            self.assertEqual(self.rows("SELECT numworks,z3 FROM financing_display WHERE codvuz IS NULL"),
                             [{"numworks": 3, "z3": 210}])
            for missing in self.rows("SELECT * FROM nir WHERE codvuz=31"):
                delete_nir(missing['id'], path=self.path, expected=missing)
            window.reload_database_views()
            self.assertEqual(page.total_label.text(), "Итого НИР: 4")
            self.assertEqual(self.rows("SELECT * FROM financing_display WHERE codvuz IS NULL"), [])
        finally:
            window.close()

    def test_header_menu_stays_in_window_and_selects_tables(self):
        window = MainWindow(self.path)
        try:
            self.assertFalse(window.menuBar().isNativeMenuBar())
            self.assertNotIn("Помощь", [action.text() for action in window.menuBar().actions()])
            options = next(action.menu() for action in window.menuBar().actions() if action.text() == "Опции")
            self.assertEqual([action.text() for action in options.actions()],
                             ["Пересчитать финансирование", "Журнал изменений"])
            window.show()
            self.app.processEvents()
            self.assertIs(window.centralWidget(), window.table_stack)
            self.assertEqual(window.table_stack.count(), 4)
            self.assertIsNone(window.menuBar().cornerWidget(Qt.Corner.TopRightCorner))
            self.assertEqual(list(window.data_actions), ['nir', 'financing', 'vuz', 'grnti'])
            window.data_actions['vuz'].trigger()
            self.assertIs(window.table_stack.currentWidget(), window.pages['vuz'])
            self.assertTrue(window.data_actions['vuz'].isChecked())
            self.assertFalse(window.data_actions['nir'].isChecked())
            for name in window.pages:
                window.data_actions[name].trigger()
                self.assertIs(window.table_stack.currentWidget(), window.pages[name])
                self.assertTrue(window.data_actions[name].isChecked())
        finally:
            window.close()

    def test_cascading_geography_can_start_at_any_level_and_cancel_restores_values(self):
        with closing(sqlite3.connect(self.path)) as conn:
            conn.execute("INSERT INTO vuz(codvuz,z1,z2,region,oblname,city) VALUES(4,'Вуз четыре','В4','Центральный','Москва','Москва')")
            conn.execute("INSERT INTO vuz(codvuz,z1,z2,region,oblname,city) VALUES(5,'Вуз пять','В5','Центральный','Белгородская область','Белгород')")
            conn.commit()
        window = MainWindow(self.path)
        try:
            page = window.pages['nir']
            def choose(field, value):
                combo = page.geography[field]
                index = combo.findData(value)
                self.assertGreaterEqual(index, 0)
                combo.setCurrentIndex(index)
            def values(field):
                combo = page.geography[field]
                return [combo.itemData(i) for i in range(1, combo.count())]
            choose('city', 'Томск')
            self.assertEqual(values('codvuz'), [2])
            self.assertEqual(page.geography['region'].currentData(), 'Сибирский')
            self.assertEqual(page.geography['oblname'].currentData(), 'Томская область')
            choose('codvuz', 2)
            choose('region', 'Центральный')
            self.assertIsNone(page.geography['city'].currentData())
            self.assertIsNone(page.geography['codvuz'].currentData())
            self.assertNotIn('Томская область', values('oblname'))
            choose('oblname', 'Москва')
            self.assertEqual(values('city'), ['Москва'])
            choose('city', 'Москва')
            self.assertEqual(values('codvuz'), [1, 4])
            choose('codvuz', 1)
            original = {f: c.currentData() for f, c in page.geography.items()}
            def change_and_cancel():
                choose('region', 'Сибирский')
                self.assertNotIn(1, values('codvuz'))
                self.assertEqual(page.model.rowCount(), 2)
                return QDialog.DialogCode.Rejected
            with patch.object(page.filter_dialog, 'exec', side_effect=change_and_cancel):
                page.open_filter()
            self.assertEqual({f: c.currentData() for f, c in page.geography.items()}, original)
            self.assertEqual(values('codvuz'), [1, 4])
            page.clear_filter()
            choose('codvuz', 2)
            self.assertEqual(page.model.rowCount(), 1)
            self.assertEqual(page.geography['region'].currentData(), 'Сибирский')
            self.assertEqual(page.geography['city'].currentData(), 'Томск')
            choose('region', None)
            self.assertEqual(page.model.rowCount(), 3)
            self.assertIsNone(page.geography['codvuz'].currentData())
        finally:
            window.close()

    def test_rubric_and_geography_are_independent_of_selection_order(self):
        window = MainWindow(self.path)
        try:
            page = window.pages['nir']
            page.rubric.setCurrentIndex(page.rubric.findData('20'))
            self.assertEqual(page.model.rowCount(), 2)
            page.geography['city'].setCurrentIndex(page.geography['city'].findData('Москва'))
            first = [page.model.record(i).value('id') for i in range(page.model.rowCount())]
            page.clear_filter()
            page.geography['city'].setCurrentIndex(page.geography['city'].findData('Москва'))
            page.rubric.setCurrentIndex(page.rubric.findData('20'))
            self.assertEqual(first, [1])
            self.assertEqual([page.model.record(i).value('id') for i in range(page.model.rowCount())], first)
        finally:
            window.close()

    def test_search_is_separate_handles_cyrillic_and_preserves_filters(self):
        save_nir(dict(self.values, rnw='ТЕКСТ', f2='Работа 100%_готова'), path=self.path)
        window = MainWindow(self.path)
        try:
            page = window.pages['nir']
            self.assertIs(page.search_edit.window(), page.search_dialog)
            page.geography['city'].setCurrentIndex(page.geography['city'].findData('Москва'))
            def cancel_search():
                page.search_field.setCurrentIndex(page.search_field.findData('f2'))
                page.search_edit.setText('ПЕРВАЯ')
                self.assertEqual(page.model.rowCount(), 3)
                return QDialog.DialogCode.Rejected
            with patch.object(page.search_dialog, 'exec', side_effect=cancel_search):
                page.search_button.click()
            self.assertEqual(page.search_edit.text(), '')
            def apply_search():
                page.search_field.setCurrentIndex(page.search_field.findData('f2'))
                page.search_edit.setText('первая')
                page.accept_search()
                return page.search_dialog.result()
            with patch.object(page.search_dialog, 'exec', side_effect=apply_search):
                page.open_search()
            self.assertEqual(page.model.rowCount(), 1)
            self.assertEqual(page.geography['city'].currentData(), 'Москва')
            page.clear_filter()
            self.assertEqual(page.search_edit.text(), 'первая')
            self.assertEqual(page.model.rowCount(), 1)
            page.clear_search()
            self.assertEqual(page.model.rowCount(), 4)
            page.search_edit.setText('%_')
            self.assertEqual(page.model.rowCount(), 1)
            self.assertEqual(page.model.record(0).value('rnw'), 'ТЕКСТ')
            row = self.rows("SELECT * FROM nir WHERE rnw='ТЕКСТ'")[0]
            save_nir(dict(row, f2='Переименованная работа'), row['id'], path=self.path, expected=row)
            window.reload_database_views()
            self.assertEqual(page.model.rowCount(), 0)
            save_nir(dict(self.values, rnw='ТЕКСТ2', f2='Новая работа %_'), path=self.path)
            window.reload_database_views()
            self.assertEqual(page.model.rowCount(), 1)
            self.assertEqual(page.model.record(0).value('rnw'), 'ТЕКСТ2')
        finally:
            window.close()

    def test_vuz_repair_keeps_works_money_backup_and_reason(self):
        with closing(sqlite3.connect(self.path)) as conn:
            conn.execute('UPDATE nir SET codvuz=NULL,z2=\'\' WHERE id=1')
            conn.execute('UPDATE nir SET codvuz=31,z2=\'\' WHERE id=3')
            conn.execute('UPDATE financing SET z18=123 WHERE codvuz=2')
            conn.execute('DELETE FROM change_log')
            conn.commit()
        expected = {r['id']: r for r in self.rows('SELECT * FROM nir WHERE id IN(1,3)')}
        reason = 'Учебное исправление связи с вузом'
        repair_vuz_links({1: 1, 3: 2}, expected=expected, reason=reason, path=self.path)
        self.assertEqual(self.rows('SELECT COUNT(*) AS n,SUM(f18) AS amount FROM nir'), [{'n': 3, 'amount': 600}])
        self.assertEqual(self.rows('SELECT codvuz,z2 FROM nir WHERE id IN(1,3) ORDER BY id'),
                         [{'codvuz': 1, 'z2': 'В1'}, {'codvuz': 2, 'z2': 'В2'}])
        self.assertEqual(self.rows('SELECT z18 FROM financing WHERE codvuz=2'), [{'z18': 123}])
        self.assertEqual(self.rows('SELECT reason FROM change_log'), [{'reason': reason}, {'reason': reason}])
        backup = self.path.parent / 'backups' / 'test_before_vuz_repair.db'
        self.assertEqual([r['codvuz'] for r in read_rows('SELECT codvuz FROM nir WHERE id IN(1,3) ORDER BY id', path=backup)], [None, 31])
        dialog = ChangeLogDialog(self.path)
        self.assertIn(reason, dialog.details.toPlainText())
        with self.assertRaises(ValueError):
            repair_vuz_links({1: 1, 3: 2}, expected=expected, reason=reason, path=self.path)
        self.assertEqual(len(self.rows('SELECT * FROM change_log')), 2)

    def test_vuz_repair_rolls_back_entire_batch_on_key_conflict(self):
        save_nir(dict(self.values, codvuz=2, rnw='018'), path=self.path)
        before = self.rows('SELECT * FROM nir ORDER BY id')
        log_before = self.rows('SELECT * FROM change_log')
        expected = {r['id']: r for r in before if r['id'] in (1,3)}
        with self.assertRaises(sqlite3.IntegrityError):
            repair_vuz_links({3: 1, 1: 2}, expected=expected, reason='Учебная правка', path=self.path)
        self.assertEqual(self.rows('SELECT * FROM nir ORDER BY id'), before)
        self.assertEqual(self.rows('SELECT * FROM change_log'), log_before)

    def test_analysis_vuz_totals_and_actual_are_not_multiplied_by_works(self):
        save_actual_financing([(1, 123), (2, 456)], path=self.path)
        before = self.path.read_bytes()
        history = self.rows('SELECT * FROM change_log')
        result = build_report('vuz', path=self.path)
        self.assertEqual(result.source_count, 3)
        self.assertEqual([(r['codvuz'], r['count'], r['planned'], r['actual']) for r in result.rows],
                         [(1, 2, 300, 123), (2, 1, 300, 456)])
        self.assertEqual(result.totals, {'number': 'Итого', 'count': 3, 'planned': 600, 'actual': 579})
        filtered = build_report('vuz', {'city': 'Москва'}, path=self.path)
        self.assertEqual(filtered.totals, {'number': 'Итого', 'count': 2, 'planned': 300, 'actual': 123})
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.rows('SELECT * FROM change_log'), history)

    def test_analysis_character_has_three_rows_including_zero_and_correct_totals(self):
        result = build_report('character', path=self.path)
        self.assertEqual([(r['count'], r['planned']) for r in result.rows], [(1, 100), (1, 200), (1, 300)])
        self.assertEqual(result.totals, {'number': 'Итого', 'count': 3, 'planned': 600})
        selected = build_report('character', {'codvuz': 1}, path=self.path)
        self.assertEqual([(r['count'], r['planned']) for r in selected.rows], [(1, 100), (1, 200), (0, 0)])
        self.assertEqual(selected.totals, {'number': 'Итого', 'count': 2, 'planned': 300})
        for kind in ('vuz', 'grnti', 'character'):
            empty = build_report(kind, {'city': 'Отсутствующий город'}, path=self.path)
            self.assertEqual(empty.source_count, 0)
            if kind == 'grnti':
                self.assertIsNone(empty.totals)
                self.assertEqual(empty.rows, [])
            else:
                self.assertEqual(empty.totals['count'], 0)
                self.assertEqual(empty.totals['planned'], 0)
                self.assertEqual(len(empty.rows), 3 if kind == 'character' else 0)

    def test_analysis_grnti_counts_both_codes_and_deduplicates_same_rubric(self):
        save_nir(dict(self.values, f10='20.10.10; 20.20.20'), path=self.path)
        result = build_report('grnti', path=self.path)
        self.assertEqual({r['code']: r['count'] for r in result.rows}, {'02': 1, '20': 3, '34': 1, '64': 1})
        self.assertEqual(result.source_count, 4)
        self.assertIsNone(result.totals)
        self.assertEqual(sum(r['count'] for r in result.rows), 6)
        self.assertTrue(all(set(r) == {'code', 'rubric', 'count'} for r in result.rows))
        selected = build_report('grnti', {'city': 'Томск'}, path=self.path)
        self.assertEqual({r['code']: r['count'] for r in selected.rows}, {'20': 1, '64': 1})

    def test_analysis_filters_combine_levels_and_use_parameter_values(self):
        self.assertEqual(build_report('vuz', {'region': 'Центральный', 'oblname': 'Москва',
                                           'city': 'Москва', 'codvuz': 1}, path=self.path).source_count, 2)
        self.assertEqual(build_report('vuz', {'region': 'Сибирский', 'city': 'Москва'}, path=self.path).source_count, 0)
        self.assertEqual(build_report('vuz', {'city': "Москва' OR 1=1 --"}, path=self.path).source_count, 0)
        with self.assertRaises(ValueError):
            build_report('vuz', {'unknown': 'Москва'}, path=self.path)

    def test_analysis_keeps_unassigned_works_in_totals(self):
        with closing(sqlite3.connect(self.path)) as conn:
            conn.execute("INSERT INTO nir(codvuz,rnw,f1,f2,f18,f10) VALUES(NULL,'UNK','П','Неизвестный вуз',50,'20.10.10')")
            conn.execute("INSERT INTO nir(codvuz,rnw,f1,f2,f18,f10) VALUES(999,'UNK2','П','Неверный код',70,'20.10.10')")
            conn.commit()
        result = build_report('vuz', path=self.path)
        unknown = next(r for r in result.rows if r['codvuz'] is None)
        self.assertEqual((unknown['vuz'], unknown['count'], unknown['planned'], unknown['actual']),
                         ('Вуз не определён', 2, 120, None))
        self.assertEqual(result.totals, {'number': 'Итого', 'count': 5, 'planned': 720, 'actual': 0})
        self.assertEqual(build_report('character', path=self.path).totals['count'], 5)

    def test_analysis_dialog_numeric_sort_and_footer(self):
        save_nir(dict(self.values, f18=750), path=self.path)
        dialog = AnalysisDialog(self.path, 'vuz')
        self.assertEqual(dialog.footer_model.rowCount(), 1)
        self.assertEqual(dialog.footer_model.rows[0]['planned'], 1350)
        self.assertEqual(dialog.footer_model.rows[0]['count'], 4)
        dialog.model.sort(3, Qt.SortOrder.DescendingOrder)
        self.assertEqual([r['planned'] for r in dialog.model.rows], [1050, 300])
        dialog.model.sort(3, Qt.SortOrder.AscendingOrder)
        self.assertEqual([r['planned'] for r in dialog.model.rows], [300, 1050])
        self.assertEqual(dialog.footer_model.rows[0]['planned'], 1350)
        self.assertEqual(dialog.footer_model.data(dialog.footer_model.index(0, 3)), '1 350')
        self.assertFalse(dialog.model.flags(dialog.model.index(0, 0)) & Qt.ItemFlag.ItemIsEditable)
        rubric = AnalysisDialog(self.path, 'grnti')
        self.assertTrue(rubric.footer.isHidden())
        self.assertEqual(rubric.footer_model.rowCount(), 0)
        character = AnalysisDialog(self.path, 'character')
        for report in (dialog, rubric, character):
            report.show()
            self.app.processEvents()
            table = report.table
            self.assertEqual(table.horizontalScrollBar().maximum(), 0)
            text_width = table.columnWidth(1)
            report.resize(report.width() + 160, report.height())
            self.app.processEvents()
            self.assertEqual(table.columnWidth(1), text_width)
            if report.footer_model.rowCount():
                for col in range(report.model.columnCount()):
                    self.assertEqual(table.columnWidth(col), report.footer.columnWidth(col))
        self.assertEqual(character.table.verticalScrollBar().maximum(), 0)
        character.layout().setContentsMargins(18, 18, 18, 18)
        character.layout().setSpacing(20)
        character.fit_window()
        character.resize(character.width(), 220)
        self.app.processEvents()
        self.assertGreaterEqual(character.table.viewport().height(), character.table.verticalHeader().length())
        self.assertEqual(character.table.verticalScrollBar().maximum(), 0)

    def test_analysis_uses_the_same_nir_filters_and_exports_conditions(self):
        window = MainWindow(self.path)
        page = window.pages["nir"]
        page.geography["city"].setCurrentIndex(page.geography["city"].findData("Томск"))
        page.rubric.setCurrentIndex(page.rubric.findData("20"))
        self.assertEqual(page.model.rowCount(), 1)
        reports = []
        directory = self.path.parent / "SUBD" / "Экспорт"
        folders = {
            "vuz": "НИР по вузам", "grnti": "НИР по рубрикам ГРНТИ", "character": "НИР по характеру",
        }

        def inspect_report(dialog):
            reports.append((dialog.kind, dialog.result.source_count))
            self.assertEqual(dialog.geography["city"], "Томск")
            self.assertEqual(dialog.rubric, "20")
            folder = directory / folders[dialog.kind]
            title = dialog.config["title"]
            stem = f"09.11.2026 — Отчёт — {title}"
            with (patch("report_export.QStandardPaths.writableLocation", return_value=str(self.path.parent)),
                  patch("report_export.QFileDialog.getExistingDirectory", side_effect=lambda _, __, path: path),
                  patch.object(QMessageBox, "information") as notice, patch("analysis_forms.date") as clock):
                clock.today.return_value = date(2026, 11, 9)
                dialog.export_report()
                if dialog.kind == "vuz":
                    dialog.export_report()
                    self.assertEqual(notice.call_count, 2)
                    self.assertTrue((folder / f"{stem} (2).xlsx").is_file())
            with closing(load_workbook(folder / f"{stem}.xlsx", read_only=True)) as workbook:
                sheet = workbook.active
                values = list(sheet.values)
                self.assertEqual(sheet.title, folder.name)
                self.assertEqual(sheet["A2"].value, title)
                text = "\n".join(str(row[0]) for row in values if row)
                for value in ("Томск", "Информатика", "09.11.2026"):
                    self.assertIn(value, text)
                if dialog.kind == "vuz":
                    self.assertEqual(values[-1], ("Итого", None, 1, 300, 0))
                elif dialog.kind == "grnti":
                    self.assertIn(("20", "Информатика", 1), values)
                else:
                    self.assertEqual(values[-1], ("Итого", None, 1, 300))
            return QDialog.DialogCode.Accepted

        with patch.object(AnalysisDialog, "exec", new=inspect_report):
            for kind in ("vuz", "grnti", "character"):
                window.open_analysis(kind)
        self.assertEqual(reports, [("vuz", 1), ("grnti", 1), ("character", 1)])
        self.assertEqual({folder.name for folder in directory.iterdir()}, {*folders.values(), "Распределение траншей"})
        page.clear_filter()
        self.assertEqual(build_report("character", path=self.path).source_count, 3)

    def test_export_uses_moved_desktop_folder_and_confirms_directory_before_saving(self):
        desktop = self.path.parent / "Desktop"
        documents = self.path.parent / "Documents"
        working = desktop / "SUBD" / "Экспорт" / "НИР по вузам"
        testing = desktop / "SUBD" / "Тестовый экспорт" / "НИР по вузам"
        custom = self.path.parent / "Custom"
        for folder in (working, testing, custom, documents / "SUBD" / "Экспорт"):
            folder.mkdir(parents=True)
        dialog = AnalysisDialog(self.path, "vuz")

        def standard_location(location):
            return str(desktop if location == QStandardPaths.StandardLocation.DesktopLocation else documents)

        with (patch("report_export.QStandardPaths.writableLocation", side_effect=standard_location),
              patch("report_export.QFileDialog.getExistingDirectory") as chooser,
              patch.object(QMessageBox, "information"),
              patch.object(QMessageBox, "warning") as warning):
            chooser.return_value = ""
            dialog.export_report()
            self.assertEqual(Path(chooser.call_args.args[2]), working)
            self.assertEqual(list(working.glob("*.xlsx")), [])

            chooser.return_value = str(custom)
            dialog.export_report()
            self.assertEqual(len(list(custom.glob("*.xlsx"))), 1)
            self.assertEqual(list(working.glob("*.xlsx")), [])

            self.app.setProperty(TEST_COPY_PROPERTY, True)
            try:
                chooser.return_value = str(working)
                dialog.export_report()
                warning.assert_called_once()
                self.assertEqual(list(working.glob("*.xlsx")), [])
                self.assertEqual(Path(chooser.call_args.args[2]), testing)

                chooser.return_value = str(testing)
                dialog.export_report()
                self.assertEqual(len(list(testing.glob("*.xlsx"))), 1)
            finally:
                self.app.setProperty(TEST_COPY_PROPERTY, None)

    def test_funding_approval_is_atomic_idempotent_and_locks_nir(self):
        save_actual_financing([(1, 20)], path=self.path)
        snapshot, orders = load_funding(self.path)
        self.assertEqual(orders, [])
        preview = preview_order(snapshot, 101, 2)
        self.assertEqual(preview.order.allocations, ((1, "В1", 50), (2, "В2", 50)))
        self.assertEqual(preview.order.total, 100)
        self.assertEqual(load_funding(self.path), (snapshot, []))
        order = approve_order(preview, path=self.path)
        self.assertEqual(approve_order(preview, path=self.path), order)
        self.assertEqual((order.number, order.quarter), (1, 2))
        current, orders = load_funding(self.path)
        self.assertEqual(orders, [order])
        self.assertEqual(current.actual, 120)
        self.assertEqual(self.rows("SELECT z18 FROM financing ORDER BY codvuz"), [{"z18": 70}, {"z18": 50}])

        second = approve_order(preview_order(current, 100, 2), path=self.path)
        self.assertEqual(second.number, 2)
        self.assertEqual(load_funding(self.path)[0].actual, 220)
        for operation in (
            lambda: save_nir(self.values, path=self.path),
            lambda: save_nir(self.values, 1, path=self.path),
            lambda: delete_nir(1, path=self.path),
            lambda: save_actual_financing([(1, 0)], path=self.path),
        ):
            with self.assertRaises(ValueError):
                operation()
        with closing(sqlite3.connect(self.path)) as conn:
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("DELETE FROM nir WHERE id=1")
            with self.assertRaises(ValueError):
                rebuild_financing(conn)
        prepare_database(self.path)
        self.assertEqual(load_funding(self.path)[0].actual, 220)
        self.assertEqual(len(self.rows("SELECT id FROM nir")), 3)

    def test_funding_stale_preview_and_failed_write_do_not_approve(self):
        initial, _ = load_funding(self.path)
        stale = preview_order(initial, 100, 1)
        save_nir(self.values, path=self.path)
        with self.assertRaisesRegex(ValueError, "изменились"):
            approve_order(stale, path=self.path)

        current, _ = load_funding(self.path)
        preview = preview_order(current, 100, 1)
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute("""CREATE TRIGGER fail_funding BEFORE UPDATE OF z18 ON financing
                            WHEN NEW.codvuz=2 BEGIN SELECT RAISE(ABORT, 'test failure'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            approve_order(preview, path=self.path)
        self.assertEqual(load_funding(self.path), (current, []))
        self.assertEqual(self.rows("SELECT * FROM change_log WHERE table_name='financing'"), [])
        delete_nir(1, path=self.path)

    def test_funding_window_approval_updates_main_window_and_archive(self):
        window = MainWindow(self.path)
        root = self.path.parent / "SUBD" / "Экспорт"
        directory = root / "Распределение траншей"

        def interact(dialog):
            dialog.rate.setText("25")
            dialog.update_input("rate")
            self.assertEqual(dialog.amount.text(), "150")
            self.assertIsNone(dialog.quarter.currentData())
            self.assertFalse(dialog.calculate_button.isEnabled())
            dialog.quarter.setCurrentIndex(dialog.quarter.findData(2))
            self.assertTrue(dialog.calculate_button.isEnabled())
            dialog.calculate()
            self.assertEqual(dialog.order.total, 150)
            dialog.reset_preview()
            self.assertFalse(dialog.approve_button.isEnabled())
            self.assertEqual(load_funding(self.path)[1], [])
            dialog.calculate()
            with (patch("report_export.QStandardPaths.writableLocation", return_value=str(self.path.parent)),
                  patch("report_export.QFileDialog.getExistingDirectory", side_effect=lambda _, __, path: path),
                  patch.object(QMessageBox, "information"), patch("funding_forms.date") as clock):
                clock.today.return_value = date(2026, 11, 9)
                dialog.save_document()
            self.assertTrue((directory / "09.11.2026 — Проект распоряжения о финансировании вузов — 2-й квартал.xlsx").is_file())
            self.assertFalse((root / "Распоряжения").exists())
            with (patch("funding_operations.date") as clock,
                  patch("report_export.QStandardPaths.writableLocation", return_value=str(self.path.parent)),
                  patch("report_export.QFileDialog.getExistingDirectory") as picker,
                  patch.object(QMessageBox, "information")):
                clock.today.return_value = date(2026, 11, 8)
                dialog.approve()
                picker.assert_not_called()
            self.assertEqual(dialog.order.number, 1)
            self.assertFalse(dialog.approve_button.isEnabled())
            self.assertFalse(dialog.save_button.isEnabled())
            self.assertTrue(dialog.recover_button.isHidden())
            self.assertIn("Факт: 150 ₽", dialog.summary.text())
            output = root / "Распоряжения" / "08.11.2026 — Распоряжение о финансировании вузов № 1 — 2-й квартал.xlsx"
            with closing(load_workbook(output, read_only=True)) as workbook:
                self.assertEqual(workbook.active["A2"].value, "Распоряжение о финансировании вузов № 1 от 08.11.2026")
                self.assertEqual(workbook.active["A4"].value, "Утверждённое распоряжение")
                rows = list(workbook.active.values)
                self.assertEqual(rows[-3], (1, "В1", 75))
                self.assertEqual(rows[-1], (None, "Итого, руб.", 150))
            dialog.history.setCurrentIndex(0)
            self.assertIsNone(dialog.quarter.currentData())
            self.assertFalse(dialog.calculate_button.isEnabled())
            return QDialog.DialogCode.Accepted

        with patch.object(FundingDialog, "exec", new=interact):
            window.open_funding()
        self.assertFalse(window.pages["nir"].add_button.isEnabled())
        self.assertFalse(window.recalculate_action.isEnabled())
        self.assertEqual(build_report("vuz", path=self.path).totals["actual"], 150)
        with patch("report_export.QStandardPaths.writableLocation", return_value=str(self.path.parent)):
            reopened = FundingDialog(self.path)
            self.assertIsNone(reopened.quarter.currentData())
            reopened.history.setCurrentIndex(1)
            self.assertEqual(reopened.order.number, 1)
            self.assertEqual(reopened.quarter.currentData(), 2)
            self.assertTrue(reopened.recover_button.isHidden())

    def test_funding_recovers_failed_automatic_export_without_approving_again(self):
        self.app.setProperty(TEST_COPY_PROPERTY, True)
        try:
            with (patch("report_export.QStandardPaths.writableLocation", return_value=str(self.path.parent)),
                  patch.object(QMessageBox, "warning") as warning,
                  patch.object(QMessageBox, "information")):
                dialog = FundingDialog(self.path)
                dialog.amount.setText("150")
                dialog.update_input("amount")
                dialog.quarter.setCurrentIndex(dialog.quarter.findData(2))
                dialog.calculate()
                with patch("funding_forms.save_order_file", side_effect=PermissionError("Доступ запрещён")):
                    dialog.approve()
                self.assertEqual(warning.call_args.args[1], "Распоряжение утверждено")
                current, orders = load_funding(self.path)
                self.assertEqual(current.actual, 150)
                self.assertEqual(len(orders), 1)

                reopened = FundingDialog(self.path)
                reopened.history.setCurrentIndex(1)
                self.assertFalse(reopened.recover_button.isHidden())
                reopened.recover_order_file()
                reopened.recover_order_file()
                self.assertTrue(reopened.recover_button.isHidden())
                self.assertEqual(load_funding(self.path), (current, orders))
                root = self.path.parent / "SUBD"
                self.assertEqual(len(list((root / "Тестовый экспорт" / "Распоряжения").glob("*.xlsx"))), 1)
                self.assertFalse((root / "Экспорт").exists())
        finally:
            self.app.setProperty(TEST_COPY_PROPERTY, None)

    def test_legacy_grnti_is_read_without_losing_codes(self):
        self.assertEqual(normalize_grnti_input("64.01.84 64.01.94"), "64.01.84; 64.01.94")
        self.assertEqual(normalize_grnti_input("34.31.15."), "34.31.15")
        self.assertEqual(normalize_grnti_input("34.31.15; 20.23.25; 02.15.51"),
                         "34.31.15; 20.23.25; 02.15.51")


if __name__ == "__main__":
    unittest.main()
