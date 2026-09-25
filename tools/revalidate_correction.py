"""离线复核已有订正回复，不读密钥、不发请求；原失败账本保持不变。"""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from study.revalidation import create_review, load_review, decide_review
from study.run_audit import AuditError, read_json


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('preview','status','accept','reject'))
    parser.add_argument('--source',type=Path,help='原单条订正失败的报告目录，仅 preview')
    parser.add_argument('--row',help='原订正编号，例如 b06，仅 preview')
    parser.add_argument('--output',type=Path,help='新复核目录，仅 preview')
    parser.add_argument('--directory',type=Path,help='已有复核目录')
    args=parser.parse_args(argv)
    try:
        if args.action=='preview':
            if args.source is None or not args.row or args.output is None or args.directory is not None:
                raise AuditError('preview 需要 --source、--row 与新的 --output。')
            value=create_review(args.source,args.row,args.output)
        else:
            if args.directory is None or args.source is not None or args.output is not None or args.row is not None:
                raise AuditError('只提供已有 --directory，来源不能在确认时改变。')
            value=load_review(args.directory)[0] if args.action=='status' else decide_review(args.directory,args.action)
        status='awaiting_user_decision' if args.action in ('preview','status') else value['action']
        if args.action=='status' and (args.directory/'decision.json').exists():
            decision=read_json(args.directory/'decision.json')
            if decision['review_id']!=value['review_id']:
                raise AuditError('决定与复核候选不一致。')
            status=decision['action']
        print(json.dumps({'mode':'offline_revalidation','review_id':value['review_id'],
            'additional_model_calls':0,'original_status':'failed',
            'status':status},ensure_ascii=False))
        return 0
    except AuditError as exc:
        print(str(exc),file=sys.stderr)
        return 2
    except (ValueError,OSError,KeyError,TypeError):
        print('复核未通过；原回复和失败状态未改变，没有模型请求或自动保存。',file=sys.stderr)
        return 2


if __name__=='__main__': raise SystemExit(main())
