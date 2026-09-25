"""生成逐条人工评分表；只汇总人工填写的有效分数，不调用模型。"""
from pathlib import Path as _BootstrapPath
import sys as _bootstrap_sys
_bootstrap_sys.path.insert(0,str(_BootstrapPath(__file__).resolve().parents[1]))
import argparse
import csv
import json
from pathlib import Path
from statistics import mean

from legacy.core import ValidationError

METRICS = ('math_correctness', 'support_fit', 'reading_understanding', 'settings_match', 'source_fidelity')
METRIC_LABELS = {'math_correctness': '数学正确性', 'support_fit': '教学帮助是否适合当前困难',
                 'reading_understanding': '是否处理读题卡结果', 'settings_match': '是否遵守六项设置',
                 'source_fidelity': '是否忠于提供的笔记'}
FIELDS = ('row_id', 'case_id', 'variant', 'repeat', 'response_file', *METRICS, 'reviewer', 'notes')


def applicable(row, metric):
    context = row['request']['context']
    return not ((metric == 'reading_understanding' and context['reading_check'] is None)
                or (metric == 'source_fidelity' and not context['sources']))


def create_scorecard(output, manifest):
    with (Path(output) / 'scores.csv').open('x', encoding='utf-8-sig', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=FIELDS)
        writer.writeheader()
        for row in manifest['plan']:
            writer.writerow({'row_id': row['row_id'], 'case_id': row['case']['case_id'], 'variant': row['variant'],
                             'repeat': row['repeat'], 'response_file': f"responses/{row['row_id']}.json",
                             **{metric: '' if applicable(row, metric) else 'NA' for metric in METRICS}})
    lines = ['# 逐条评估索引', '', '本表不自动评分。先阅读 evaluation/RUBRIC.md，再填写 scores.csv 的分数、reviewer 和 notes。',
             '0 = 未达到，1 = 部分达到，2 = 达到。空白 = 尚未评分；NA 只用于预设的不适用项。',
             '本机手写响应只能演练流程。不能写成真实模型教学效果。', '',
             '| 行 | 用例 / 提示词 | 本条重点 | 请求 | 回复 |', '|---|---|---|---|---|']
    for row in manifest['plan']:
        key = row['row_id']
        lines.append(f"| {key} | {row['case']['case_id']} / {row['variant']} | {row['case']['expected_focus']} | [请求](requests/{key}.json) | [回复](responses/{key}.json) |")
    (Path(output) / 'review.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')


def summarize_scores(output):
    output = Path(output)
    manifest = json.loads((output / 'manifest.json').read_text(encoding='utf-8'))
    plan = {row['row_id']: row for row in manifest['plan']}
    with (output / 'scores.csv').open(encoding='utf-8-sig', newline='') as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames != list(FIELDS):
            raise ValidationError('评分表列名发生变化')
        scores = list(reader)
    if len(scores) != len(plan) or {row.get('row_id') for row in scores} != set(plan):
        raise ValidationError('评分表缺行、重复行或含未知行 ID')
    complete, partial, statuses = {}, 0, {}
    for score in scores:
        row = plan[score['row_id']]
        if (score['case_id'] != row['case']['case_id'] or score['variant'] != row['variant']
                or score['repeat'] != str(row['repeat']) or score['response_file'] != f"responses/{row['row_id']}.json"
                or None in score or any(value is None for value in score.values())):
            raise ValidationError('评分表的行映射或 CSV 结构发生变化')
        result_path = output / score['response_file']
        result = json.loads(result_path.read_text(encoding='utf-8')) if result_path.exists() else {'status': 'pending'}
        statuses[result['status']] = statuses.get(result['status'], 0) + 1
        numeric = {}
        for metric in METRICS:
            value = score[metric].strip()
            if not applicable(row, metric):
                if value != 'NA':
                    raise ValidationError(f"{row['row_id']} 的 {metric} 必须为 NA")
            elif value not in ('', '0', '1', '2'):
                raise ValidationError(f"{row['row_id']} 的 {metric} 只能为空或 0/1/2")
            elif value:
                numeric[metric] = int(value)
        if numeric and result['status'] != 'reply_valid':
            raise ValidationError('预览、失败、待运行或完成情况未知的行不能填教学分数')
        if numeric and not score['reviewer'].strip():
            raise ValidationError('填写分数时也须填写 reviewer，以便追溯人工评分')
        required = sum(applicable(row, metric) for metric in METRICS)
        if len(numeric) == required:
            complete[row['row_id']] = numeric
        elif numeric:
            partial += 1
    groups = {}
    for row_id, values in complete.items():
        row = plan[row_id]
        groups.setdefault((row['case']['case_id'], row['repeat']), {})[row['variant']] = values
    comparison = {}
    for metric in METRICS:
        pairs = [(rows['baseline'][metric], rows['protocol'][metric]) for rows in groups.values()
                 if set(rows) == {'baseline', 'protocol'} and metric in rows['baseline'] and metric in rows['protocol']]
        comparison[metric] = {'paired_count': len(pairs),
                              'baseline_mean': round(mean(a for a, b in pairs), 3) if pairs else None,
                              'protocol_mean': round(mean(b for a, b in pairs), 3) if pairs else None,
                              'mean_difference': round(mean(b - a for a, b in pairs), 3) if pairs else None}
    return {'mode': manifest['mode'], 'suite': manifest['suite'], 'total_rows': len(plan),
            'statuses': statuses, 'fully_scored_rows': len(complete), 'partially_scored_rows': partial,
            'unscored_rows': len(plan) - len(complete) - partial, 'comparison': comparison,
            'interpretation': ('真实回复的人工评分；仅针对这四道合成题，不代表学生学习效果。' if manifest['mode'] == 'real_api'
                               else '流程演练，未获得真实模型教学效果；不得作为简历中的模型性能结果。')}


def render_report(summary):
    lines = ['# 人工评分汇总', '', summary['interpretation'], '',
             f"模式：{summary['mode']}；用例集：{summary['suite']}；共 {summary['total_rows']} 行。",
             f"完整评分 {summary['fully_scored_rows']} 行，部分评分 {summary['partially_scored_rows']} 行，未评分 {summary['unscored_rows']} 行。",
             f"运行状态：{summary['statuses']}", '',
             '只有两种提示词都完整评分的同一用例、同一轮次，才进入对应指标的成对比较。空白不按零分计算。', '',
             '| 指标 | 完整配对数 | 基线均分 | 协议均分 | 协议减基线 |', '|---|---:|---:|---:|---:|']
    for metric, values in summary['comparison'].items():
        numbers = ['待评分' if values[key] is None else str(values[key]) for key in ('baseline_mean', 'protocol_mean', 'mean_difference')]
        lines.append(f"| {METRIC_LABELS[metric]} | {values['paired_count']} | " + ' | '.join(numbers) + ' |')
    lines += ['', '这是提示词版本的描述性比较。两边共享本地检查、资料、状态边界和 JSON 校验，不能据此宣称整个系统优于普通聊天。',
              '运行失败单独报告；只统计有效回复会产生选择偏差。单次运行、小样本和自评不能支持显著性或学习增益结论。']
    return '\n'.join(lines) + '\n'


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True, help='已生成的评估报告目录')
    parser.add_argument('--report', type=Path, help='另存新的 Markdown 文件；已存在则拒绝覆盖')
    args = parser.parse_args(argv)
    try:
        summary = summarize_scores(args.output)
        if args.report:
            with args.report.open('x', encoding='utf-8') as stream:
                stream.write(render_report(summary))
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError):
        print('评分汇总失败：检查报告目录、评分表格式和分数；不会更改已有记录。')
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
