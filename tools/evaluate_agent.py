"""E1/E2 offline preflight, local HTTP rehearsal and blank human scoring sheets."""
import argparse
import json
from pathlib import Path

from study.evaluation import freeze, run_local, report
from study.evaluation_scoring import score_report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('preview', 'local', 'status', 'scores'))
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--split', choices=('development', 'reserved', 'all'), default='development')
    args = parser.parse_args(argv)
    try:
        if args.action in ('preview', 'local'):
            freeze(args.output, args.split)
            value = run_local(args.output) if args.action == 'local' else report(args.output)
        elif args.action == 'scores':
            value = score_report(args.output)
        else:
            value = report(args.output)
        print(json.dumps(value, ensure_ascii=False))
    except (ValueError, OSError, KeyError, TypeError):
        print('评估已停止：目录已存在、冻结版本变化或记录无效；不覆盖、不自动重试。')
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
