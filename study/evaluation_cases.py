"""New family-separated A/B cases. Grading references never belong to model inputs."""
from copy import deepcopy
from pathlib import Path
import re

from study.context import INTENTS, learning, task_for
from study.preferences import validate_settings
from study.run_audit import AuditError, digest, read_json
from study.service import analysis_context

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / 'evaluation/agent_ab_v1'
CASE_KEYS = {'case_id', 'family_id', 'split', 'title', 'tags', 'settings',
             'history_enabled', 'history_topic', 'fault', 'turns', 'human_only'}
TURN_KEYS = {'family_id', 'question', 'level', 'student_work', 'work_kind',
             'operation', 'intent', 'request', 'overrides', 'select_previous_next'}


def load_cases(path=None):
    data = read_json(path or ASSETS / 'cases.json')
    if data.get('schema_version') != 1 or data.get('suite') != 'agent-ab-v1':
        raise AuditError('Unknown evaluation suite.')
    cases = data['cases']
    if type(cases) is not list or len(cases) != 30:
        raise AuditError('Suite requires 30 authored cases.')
    ids, families, questions = set(), {}, {}
    for case in cases:
        if not CASE_KEYS <= set(case) or set(case) - CASE_KEYS - {'transcription'}:
            raise AuditError('Unknown case fields.')
        cid, split = case['case_id'], case['split']
        if not re.fullmatch(r'e\d{2}', cid) or cid in ids or split not in ('development', 'reserved'):
            raise AuditError('Invalid case ID or split.')
        ids.add(cid)
        validate_settings(case['settings'])
        if (type(case['history_enabled']) is not bool or case['fault'] not in (None, 'search_notes')
                or not 1 <= len(case['turns']) <= 5
                or case['turns'][0]['family_id'] != case['family_id']
                or len(case['human_only']['turn_checks']) != len(case['turns'])):
            raise AuditError('Invalid scenario specification.')
        for turn in case['turns']:
            if (set(turn) != TURN_KEYS or turn['operation'] not in ('analyze', 'coach')
                    or turn['intent'] not in INTENTS or type(turn['select_previous_next']) is not bool
                    or (turn['operation'] == 'analyze') != (turn['intent'] == 'analyze')):
                raise AuditError('Invalid turn specification.')
            analysis_context(turn['question'], turn['level'], turn['student_work'], work_kind=turn['work_kind'])
            learning(task_for(None, cid, 'validation', 'off'), case['settings'],
                     request=turn['request'], intent=turn['intent'], overrides=turn['overrides'])
            family = turn['family_id']
            if not re.fullmatch(r'[a-z_]+', family):
                raise AuditError('Invalid family ID.')
            question = ''.join(turn['question'].split())
            if families.setdefault(family, split) != split or questions.setdefault(question, split) != split:
                raise AuditError('Problem family or question crosses the development/reserved split.')
    if sum(c['split'] == 'development' for c in cases) != 20:
        raise AuditError('Suite requires 20 development and 10 reserved cases.')
    return cases


def inventory(cases):
    turns = [t for c in cases for t in c['turns']]
    return {'cases': len(cases), 'families': len({t['family_id'] for t in turns}),
            'distinct_confirmed_questions': len({t['question'] for t in turns}),
            'student_participants': 0, 'multi_turn_cases': sum(len(c['turns']) > 1 for c in cases),
            'conversation_turns_per_arm': len(turns), 'paired_turn_rows': 2 * len(turns),
            'max_model_requests_A': len(turns), 'max_model_requests_B': 4 * len(turns),
            'max_model_requests_total': 5 * len(turns)}


def model_context(case, turn, task, arm):
    """Explicit allowlist: no grading keys, raw OCR, scenario labels or references."""
    from study.context import attach
    value = learning(task, case['settings'], request=turn['request'], intent=turn['intent'],
                     overrides=turn['overrides'])
    if arm == 'A':
        value['recent_dialogue'] = value['recent_dialogue'][-1:]
    elif arm != 'B':
        raise AuditError('Unknown arm.')
    return attach(analysis_context(turn['question'], turn['level'], turn['student_work'],
                                   work_kind=turn['work_kind']), value)


def task_for_turn(previous, case, turn):
    # Question switch resets discussion and selected tasks; settings remain explicit.
    return task_for(previous, case['case_id'] + '-' + turn['family_id'],
                    digest([turn['question'], turn['student_work']]), case['history_enabled'])


def selected_cases(split='development'):
    if split not in ('development', 'reserved', 'all'):
        raise AuditError('Unknown split.')
    return deepcopy([c for c in load_cases() if split == 'all' or c['split'] == split])
