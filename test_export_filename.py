"""Проверка имени файлов Excel (независимо от Qt)."""
from datetime import date
from pathlib import Path
import unittest

from export_filename import dated_excel_filename


class ExportFilenameTests(unittest.TestCase):
    def test_adds_creation_date_to_full_export(self):
        self.assertEqual(
            dated_excel_filename("Все_НИР.xlsx", on_date=date(2026, 11, 5)),
            "Все_НИР_2026-11-05.xlsx",
        )

    def test_does_not_duplicate_date_and_uses_directory(self):
        p = Path("reports") / "Отчёт_vuz_2026-11-05.xlsx"
        self.assertEqual(dated_excel_filename(p, on_date=date(2026, 11, 5)), str(p))

    def test_manual_name_also_gets_date_and_extension(self):
        self.assertEqual(dated_excel_filename("Мой отчёт", on_date=date(2026, 11, 5)),
                         "Мой отчёт_2026-11-05.xlsx")
        self.assertEqual(dated_excel_filename("Результаты.xlsx", on_date=date(2026, 11, 5)),
                         "Результаты_2026-11-05.xlsx")

if __name__ == "__main__":
    unittest.main()
