"""仅匹配自写笔记；工具不接收文件路径，不执行笔记内容。"""
from pathlib import Path as _BootstrapPath
import sys as _bootstrap_sys
_bootstrap_sys.path.insert(0,str(_BootstrapPath(__file__).resolve().parents[1]))
import json
from pathlib import Path
import re
import subprocess
import sys
import time

from legacy.core import ValidationError

NOTES_DIR = Path(__file__).resolve().parents[1] / 'notes'


class ToolError(ValueError):
    pass


def check_deadline(deadline):
    if time.monotonic() >= deadline:
        raise TimeoutError('本轮工具调用超过总时限')


def _terms(text):
    """英文词与中文连续词组/双字组，属于关键词匹配，不是语义检索。"""
    result = set(re.findall(r'[a-z_][a-z_0-9]*', text.lower()))
    for word in re.findall(r'[\u4e00-\u9fff]+', text):
        if len(word) <= 2:
            result.add(word)
        else:
            result.update(word[i:i+2] for i in range(len(word)-1))
    return result


def search_notes(query, *, notes_dir=NOTES_DIR, deadline=float('inf')):
    if type(query) is not str or not query.strip() or len(query) > 200:
        raise ValidationError('query 必须为 1–200 字符的非空字符串')
    root = Path(notes_dir).resolve()
    check_deadline(deadline)
    if not root.is_dir():
        raise ToolError('笔记目录不存在')
    # 仅固定目录的一层 JSON，不跟随指向其他目录的符号链接。
    paths = sorted(root.glob('*.json'))
    if len(paths) > 32:
        raise ToolError('笔记数量超过首版限制 32')
    matches, seen = [], set()
    query_terms = _terms(query)
    for path in paths:
        check_deadline(deadline)
        if path.is_symlink() or path.resolve().parent != root or not path.is_file():
            raise ToolError('拒绝笔记符号链接或目录外文件')
        with path.open('rb') as stream:
            raw = stream.read(65537)
        if len(raw) > 65536:
            raise ToolError('单篇笔记超过 64 KiB')
        try:
            note = json.loads(raw)
        except (ValueError, UnicodeError) as exc:
            raise ToolError('笔记 JSON 损坏') from exc
        if (type(note) is not dict or set(note) != {'source_id', 'title', 'tags', 'body'}
                or any(type(note[k]) is not str or not note[k].strip() for k in ('source_id', 'title', 'body'))
                or type(note['tags']) is not list
                or any(type(x) is not str or not x.strip() for x in note['tags'])
                or note['source_id'] in seen):
            raise ToolError('笔记格式错误或来源 ID 重复')
        seen.add(note['source_id'])
        title = _terms(note['title'])
        tags = _terms(' '.join(note['tags']))
        body = _terms(note['body'])
        score = 3 * len(query_terms & title) + 2 * len(query_terms & tags) + len(query_terms & body)
        if score:
            matches.append({'source_id': note['source_id'], 'title': note['title'],
                            'snippet': relevant_snippet(note['body'],query_terms), 'score': score})
    check_deadline(deadline)
    return sorted(matches, key=lambda x: (-x['score'], x['source_id']))[:3]


def relevant_snippet(body,query_terms):
    """短笔记原样返回；长笔记优先取命中词的句段，仍是标明来源的节选。"""
    if len(body)<=360:return body
    pieces=re.split(r'(?<=[。！？!?\n])',body)
    scores=[len(query_terms & _terms(piece)) for piece in pieces]
    start=max(range(len(pieces)),key=lambda i:scores[i])
    selected=pieces[start]
    for following in pieces[start+1:]:
        if len(selected)+len(following)>360:break
        selected+=following
    if len(selected)<=360:return selected
    # 参考笔记允许节选，当前题目与作答不走此函数。
    positions=[selected.casefold().find(term) for term in query_terms]
    hit=min((p for p in positions if p>=0),default=0)
    return selected[max(0,hit-80):max(0,hit-80)+360]


class ToolBudget:
    """每轮创建一次；所有工具请求共用两次上限和同一个时间截止点。"""
    def __init__(self, notes_dir=NOTES_DIR, timeout_seconds=3.0):
        self.notes_dir = notes_dir
        self.deadline = time.monotonic() + timeout_seconds
        self.records = []
        self.attempts = 0

    def call(self, name, arguments):
        self.attempts += 1
        entry = {'tool': name, 'arguments': arguments, 'results': [], 'status': 'error'}
        self.records.append(entry)
        start = time.monotonic()
        try:
            check_deadline(self.deadline)
            if self.attempts > 2:
                raise ToolError('每轮最多允许两次工具请求')
            if name != 'search_notes':
                raise ToolError('不允许的工具名')
            if type(arguments) is not dict or set(arguments) != {'query'}:
                raise ToolError('工具参数只能包含 query')
            query = arguments['query']
            if type(query) is not str or not query.strip() or len(query) > 200:
                raise ValidationError('query 必须为 1–200 字符的非空字符串')
            # 固定执行本工具文件；shell=False，query 只是数据，不是命令。
            # 子进程使阻塞的磁盘读取也能在剩余时限到达时被终止。
            try:
                worker = subprocess.run(
                    [sys.executable, str(Path(__file__).resolve()), str(Path(self.notes_dir).resolve()), query],
                    capture_output=True, text=True, timeout=max(0.001, self.deadline - time.monotonic()),
                )
            except subprocess.TimeoutExpired as exc:
                raise TimeoutError('本轮工具调用超过总时限，检索进程已终止') from exc
            check_deadline(self.deadline)
            payload = json.loads(worker.stdout)
            if worker.returncode:
                raise ToolError(payload.get('error', '笔记读取失败'))
            entry['results'] = payload['results']
            entry['status'] = 'ok' if entry['results'] else 'no_results'
            return entry['results']
        except (OSError, ValueError, TimeoutError) as exc:
            entry['error'] = str(exc)
            raise
        finally:
            entry['elapsed_ms'] = round((time.monotonic() - start) * 1000, 2)


if __name__ == '__main__':
    try:
        results = search_notes(sys.argv[2], notes_dir=Path(sys.argv[1]))
        print(json.dumps({'results': results}, ensure_ascii=False))
    except (OSError, ValueError) as error:
        print(json.dumps({'error': str(error)}, ensure_ascii=False))
        sys.exit(1)
