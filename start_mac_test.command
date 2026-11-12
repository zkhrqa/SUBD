#!/bin/zsh
cd -- "${0:A:h}" || exit 1

if [[ ! -x .venv/bin/python ]]; then
    print "Не найдено окружение .venv. Выполните в папке проекта:"
    print "python3 -m venv .venv"
    print ".venv/bin/python -m pip install -r requirements.txt"
    [[ -t 0 ]] && read -r "reply?Нажмите Enter для закрытия…"
    exit 1
fi

.venv/bin/python -B - <<'PY'
import sqlite3
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from PyQt6.QtWidgets import QApplication

from database import DB_PATH
from main import MainWindow
from nir_operations import prepare_database
from report_export import TEST_COPY_PROPERTY

with TemporaryDirectory(prefix="subd-test-") as folder:
    copy = Path(folder) / "test.db"
    with (closing(sqlite3.connect(DB_PATH.as_uri() + "?mode=ro", uri=True)) as source,
          closing(sqlite3.connect(copy)) as target):
        source.backup(target)

    prepare_database(copy)
    app = QApplication([])
    app.setApplicationName("СУБД — тестовая копия")
    app.setProperty(TEST_COPY_PROPERTY, True)
    window = MainWindow(copy)
    window.setWindowTitle(app.applicationName())
    window.show()
    raise SystemExit(app.exec())
PY
result=$?
if (( result != 0 )); then
    print "Не удалось запустить тестовую копию (код $result)."
    [[ -t 0 ]] && read -r "reply?Нажмите Enter для закрытия…"
fi
exit $result
