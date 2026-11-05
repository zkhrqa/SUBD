#!/bin/zsh
cd -- "${0:A:h}" || exit 1

if [[ ! -x .venv/bin/python ]]; then
    print "Не найдено окружение .venv. Выполните в папке проекта:"
    print "python3 -m venv .venv"
    print ".venv/bin/python -m pip install -r requirements.txt"
    read -r "reply?Нажмите Enter для закрытия…"
    exit 1
fi

.venv/bin/python -B main.py
result=$?
if (( result != 0 )); then
    print "Программа завершилась с ошибкой (код $result)."
    read -r "reply?Нажмите Enter для закрытия…"
fi
exit $result
