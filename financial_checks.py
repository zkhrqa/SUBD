"""Проверка правильности финансовых итогов без записи в базу."""
from __future__ import annotations

from contextlib import closing
from pathlib import Path
import sqlite3

from database import DB_PATH


def inspect_financing(path: Path = DB_PATH) -> dict:
    with closing(sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=30)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute('BEGIN')
        expected = {
            r['codvuz']: (r['planned'], r['works']) for r in conn.execute('''
                SELECT v.codvuz, COALESCE(SUM(n.f18),0) AS planned, COUNT(*) AS works
                FROM nir n JOIN vuz v ON v.codvuz=n.codvuz
                GROUP BY v.codvuz
            ''')
        }
        stored = {r['codvuz']: r for r in conn.execute('SELECT codvuz,z3,z18,numworks FROM financing')}
        problems = []
        for codvuz, (planned, works) in expected.items():
            if codvuz not in stored:
                problems.append(f'Вуз {codvuz}: строка финансирования отсутствует.')
            elif (stored[codvuz]['z3'], stored[codvuz]['numworks']) != (planned, works):
                problems.append(f'Вуз {codvuz}: в финансировании план={stored[codvuz]["z3"]}, '
                                f'НИР={stored[codvuz]["numworks"]}; в НИР план={planned}, работ={works}.')
        for codvuz in stored.keys() - expected.keys():
            problems.append(f'Вуз {codvuz}: лишняя строка финансирования без НИР.')
        unknown = conn.execute('''
            SELECT COUNT(*) AS works, COALESCE(SUM(n.f18),0) AS planned
            FROM nir n LEFT JOIN vuz v ON v.codvuz=n.codvuz WHERE v.codvuz IS NULL
        ''').fetchone()
        return {
            'university_count': len(expected),
            'works': sum(v[1] for v in expected.values()) + unknown['works'],
            'planned': sum(v[0] for v in expected.values()) + unknown['planned'],
            'actual': sum(int(r['z18']) for r in stored.values()),
            'unknown_works': unknown['works'],
            'issues': problems,
        }
