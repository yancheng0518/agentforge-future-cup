"""Run inside the workbench's Docker application runner."""
import argparse
import json
import os
from pathlib import Path

from ledger import add_expense, list_expenses


def main():
    parser = argparse.ArgumentParser(description='A small persistent expense ledger')
    commands = parser.add_subparsers(dest='command', required=True)
    add = commands.add_parser('add')
    add.add_argument('--title', required=True)
    add.add_argument('--cents', required=True, type=int)
    add.add_argument('--category', required=True)
    commands.add_parser('list')
    args = parser.parse_args()
    path = Path(os.environ.get('AGENTFORGE_DATA_DIR', '.')) / 'expenses.json'
    entries = json.loads(path.read_text(encoding='utf-8')) if path.exists() else []
    if args.command == 'add':
        entries = add_expense(entries, args.title, args.cents, args.category)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(entries, ensure_ascii=False), encoding='utf-8')
        result = entries[-1]
    else:
        result = list_expenses(entries)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
