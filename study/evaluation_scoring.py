"""Human-only score validation; no automatic judge and no quality score for local fixtures."""
from pathlib import Path

from study.evaluation import DIMENSIONS, report
from study.run_audit import AuditError, read_json, write_json


def summarize(manifest, sheet, results):
    if (set(sheet) != {'schema_version', 'plan_id', 'label', 'rows'} or sheet['schema_version'] != 1
            or sheet['plan_id'] != manifest['plan_id'] or len(sheet['rows']) != len(manifest['rows'])):
        raise AuditError('Scoring sheet does not match the frozen plan.')
    expected = {r['row_id']: r for r in manifest['rows']}
    cases = {c['case_id']: c for c in manifest['cases']}
    seen, scored, paired = set(), {}, {}
    summary = {'planned_rows': len(expected), 'reviewer_roles': [], 'real_completed_rows': 0,
               'rows_with_any_score': 0, 'unsupported_inference': {'reviewed': 0, 'flagged': 0},
               'dimensions': {}, 'quality_claim': 'Human grades, not learning-outcome evidence.'}
    roles = set()
    for row in sheet['rows']:
        spec = expected.get(row['row_id'])
        if spec is None or row['row_id'] in seen or any(row[k] != spec[k] for k in spec):
            raise AuditError('Unknown, duplicate or changed scoring row.')
        seen.add(row['row_id'])
        case = cases[row['case_id']]
        checks = case['human_only']['checks'] + [case['human_only']['turn_checks'][row['turn_index']]]
        if row['reference'] != case['human_only']['reference'] or row['checks'] != checks:
            raise AuditError('Do not change the preregistered references or checks during grading.')
        if set(row['scores']) != set(DIMENSIONS):
            raise AuditError('Unknown scoring dimensions.')
        values = row['scores']
        if any(v is not None and (type(v) is not int or v not in (0, 1, 2)) for v in values.values()):
            raise AuditError('Scores must be null or integer 0/1/2; blank is not zero.')
        flag = row['unsupported_inference']
        if flag is not None and type(flag) is not bool:
            raise AuditError('Unsupported inference must be null/true/false.')
        result = results.get(row['row_id'])
        eligible = bool(result and result['status'] == 'completed' and result['mode'] == 'real_api')
        summary['real_completed_rows'] += int(eligible)
        annotated = any(v is not None for v in values.values()) or flag is not None
        if annotated:
            if not eligible:
                raise AuditError('Preview, failure, pending and local fixtures cannot receive model quality grades.')
            if (not isinstance(row['reviewer'], str) or not row['reviewer'].strip()
                    or row['reviewer_role'] not in ('developer_self', 'independent_teacher', 'independent_peer')
                    or not isinstance(row['evidence'], str) or not row['evidence'].strip()):
                raise AuditError('Each grade needs reviewer, role and concrete reply evidence.')
            roles.add(row['reviewer_role'])
        if values['history_use'] is not None and not case['history_enabled']:
            raise AuditError('History scoring is not applicable when history is disabled.')
        sources = (result.get('trace') or {}).get('sources', {}) if result else {}
        if values['source_support'] is not None and not sources:
            raise AuditError('Source support requires actual returned sources.')
        if any(v is not None for v in values.values()):
            summary['rows_with_any_score'] += 1
        if flag is not None:
            summary['unsupported_inference']['reviewed'] += 1
            summary['unsupported_inference']['flagged'] += int(flag)
        scored[row['row_id']] = row
        paired.setdefault((row['case_id'], row['turn_index']), {})[row['arm']] = row
    for dim in DIMENSIONS:
        samples = {a: [r['scores'][dim] for r in scored.values() if r['arm'] == a and r['scores'][dim] is not None]
                   for a in ('A', 'B')}
        pairs = [p for p in paired.values() if set(p) == {'A', 'B'} and
                 all(p[a]['scores'][dim] is not None for a in ('A', 'B'))]
        differences = [p['B']['scores'][dim] - p['A']['scores'][dim] for p in pairs]
        summary['dimensions'][dim] = {
            'scored_rows': {a: len(v) for a, v in samples.items()},
            'mean': {a: sum(v) / len(v) if v else None for a, v in samples.items()},
            'complete_pairs': len(pairs), 'paired_mean_B_minus_A': sum(differences) / len(differences) if differences else None}
    summary['reviewer_roles'] = sorted(roles)
    summary['scoring_coverage'] = (summary['rows_with_any_score'] / summary['real_completed_rows']
                                   if summary['real_completed_rows'] else None)
    return summary


def score_report(directory):
    directory = Path(directory)
    manifest = read_json(directory / 'manifest.json')
    # Reporting deliberately works after code changes; frozen input identities must still match.
    from study.evaluation_live import SUITE, EvaluationPlan
    if manifest.get('suite') == SUITE:
        plan = EvaluationPlan(directory)
        reliability = plan.status()
        results = {row_id: row for row_id in plan.rows if (row := plan.row(row_id)) is not None}
    else:
        reliability = report(directory)
        results = {r['row_id']: read_json(directory / 'rows' / (r['row_id'] + '.json'))
                   for r in manifest['rows'] if (directory / 'rows' / (r['row_id'] + '.json')).exists()}
    output = {'reliability': reliability, 'human_scoring': summarize(manifest, read_json(directory / 'scores.json'), results)}
    write_json(directory / 'scoring-summary.json', output)
    return output
