"""离线规则验收样例，不调用模型，不评估真实学习效果。"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from curriculum import check_solution

ROOT = Path(__file__).resolve().parent
SOURCES = ['core.py', 'curriculum.py', 'step_check.py', 'workflow.py', 'retrieval.py', 'app.py',
           'model_boundary.py', 'model_api.py', 'http_worker.py', 'run_app.py', 'evaluate_steps.py',
           'reading_check.py', 'reading_ui.py']


def hashes():
    return {name: hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in SOURCES}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--split', choices=['development', 'reserved'], required=True)
    parser.add_argument('--output', type=Path, required=True, help='新建输出文件；不覆盖历史运行')
    parser.add_argument('--freeze', type=Path, help='开发样例通过后写入代码与样例哈希；预留样例要求传入同一文件')
    args = parser.parse_args()
    code_hashes = hashes()
    fixture_hashes = {name: hashlib.sha256((ROOT/'evaluation'/f'{name}.jsonl').read_bytes()).hexdigest()
                      for name in ['development','reserved']}
    if args.split == 'reserved':
        if args.freeze is None or not args.freeze.exists():
            parser.error('请先运行开发样例并写入 freeze 文件')
        saved = json.loads(args.freeze.read_text())
        if saved['code_hashes'] != code_hashes or saved['fixture_hashes'] != fixture_hashes:
            parser.error('代码或样例已改变，不能把这次运行算作同一固定版本的预留验收')
    rows=[]
    for line in (ROOT/'evaluation'/f'{args.split}.jsonl').read_text().splitlines():
        case=json.loads(line)
        result=check_solution(case['task_id'],case['steps'])
        passed=result.status==case['expected_status'] and result.first_issue==case['expected_first_issue']
        rows.append({**case,'actual':result.to_dict(),'passed':passed,'split':args.split,
                     'timestamp':datetime.now(timezone.utc).isoformat(),'code_hashes':code_hashes,'real_api_calls':0})
    args.output.parent.mkdir(parents=True,exist_ok=True)
    with args.output.open('x',encoding='utf-8') as file:
        for row in rows:
            file.write(json.dumps(row,ensure_ascii=False)+'\n')
    passed=sum(row['passed'] for row in rows)
    print(f'{args.split}: {passed}/{len(rows)} 条规则样例符合预期；真实模型调用数 0')
    for row in rows:
        if not row['passed']:
            print(row['case_id'], '期望',row['expected_status'],row['expected_first_issue'],
                  '实际',row['actual']['status'],row['actual']['first_issue'])
    if args.split=='development' and args.freeze and passed==len(rows):
        with args.freeze.open('x',encoding='utf-8') as file:
            json.dump({'code_hashes':code_hashes,'fixture_hashes':fixture_hashes},file,indent=2)
    raise SystemExit(0 if passed==len(rows) else 1)


if __name__=='__main__':
    main()
