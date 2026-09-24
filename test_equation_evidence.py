"""Synthetic regressions: clarification and bounded equation evidence, no paid calls."""
from copy import deepcopy
from fractions import Fraction
import json
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

from http_test_support import LocalModelServer
from step_check import UnsupportedExpression
from study.diagnosis import validate_analysis, AnalysisValidationError
from study.equation_evidence import solution, equation_issue
from study.example import ANALYSIS
from study.notebook import Notebook, make_entry
from study.corrections import validate_result
from study.run_audit import RunAudit
from study.smoke import create_run, execute, default_config
from tools.verify_study_live import local_fixture

ROOT = Path(__file__).parent
QUESTION = '解方程 5x - 4 = 16。'
WORK = '(5x - 4)/5 = 16/5\nx - 4/5 = 16/5\nx = 20/5 = 4'
ISSUE = 'step_equation_verdict_mismatch'


def analysis(work=WORK, wrong=False):
    value = deepcopy(ANALYSIS)
    value.update(summary='按等式性质核对两边的操作。', answer='x=4',
                 steps=['两边同除以5，再移项，得到x=4。'], diagnosis=[])
    value['student_review'] = {'work_kind':'steps', 'verdict':'incorrect' if wrong else 'correct',
        'observed_approach':'先两边同除以非零常数，再移项。', 'answer_feedback':'对照等式性质。',
        'comparisons':[{'student_excerpt':line, 'reference_step':'保持方程等价。',
                        'verdict':'incorrect' if wrong and i==0 else 'correct',
                        'explanation':'这是人工编写的校验反例，不代表真实模型成绩。'}
                       for i,line in enumerate(work.splitlines())]}
    return value


def clarification():
    value=json.loads((ROOT/'evaluation/study_smoke_local_responses.json').read_text())['responses']['b05']
    value.update(summary='', next_practice='', takeaway='')
    return value


class EquationTests(unittest.TestCase):
    def test_exact_fraction_decimal_signed_and_parenthesized_equations(self):
        for text, symbol, expected in [('(5x-4)/5=16/5','x',4),('x=20/5=4','x',4),
                ('－2x＋1＝9','x',-4),('0.5x+1=2','x',2),('2(y+1)=8','y',3),
                ('(x+1)/(-2)=3','x',-7)]:
            with self.subTest(text=text): self.assertEqual(Fraction(expected),solution(text,symbol))

    def test_nonlinear_domain_changing_unsafe_and_large_inputs_unsupported(self):
        for text in ('x*x=4','x^2=4','x/x=1','x/0=4','x+y=4','__import__(x)=4',
                     '0x=0','0x=1','x=4=5','x='+'('*20+'4'+')'*20,
                     '9999999999999x=4','x=0.000000000001/100000000000'):
            with self.subTest(text=text), self.assertRaises((ValueError,ZeroDivisionError)):
                solution(text,'x')

    def test_generated_equivalent_methods_do_not_require_standard_order(self):
        for a in (2,-3,5):
            for b in (-6,4):
                for root in (-4,0,7):
                    c=a*root+b; question=f'{a}x+({b})={c}'
                    work=f'({a}x+({b}))/({a})=({c})/({a})\nx={root}'
                    rows=[{'student_excerpt':line,'verdict':'incorrect'} for line in work.splitlines()]
                    self.assertEqual(ISSUE,equation_issue(question,work,rows))
                    for row in rows:row['verdict']='correct'
                    self.assertIsNone(equation_issue(question,work,rows))

    def test_first_wrong_step_checked_but_later_error_propagation_not_condemned(self):
        work='5x=12\nx=12/5'
        self.assertEqual(ISSUE,equation_issue(QUESTION,work,[{'student_excerpt':'5x=12','verdict':'correct'}]))
        self.assertIsNone(equation_issue(QUESTION,work,[{'student_excerpt':'5x=12','verdict':'incorrect'},
            {'student_excerpt':'x=12/5','verdict':'correct'}]))

    def test_missing_question_word_problem_and_other_symbols_do_not_guess(self):
        rows=[{'student_excerpt':WORK.splitlines()[0],'verdict':'incorrect'}]
        for question in (None,'请解这道应用题：5x-4=16，求x。','x+y=4','x*x=16','5y-4=16'):
            self.assertIsNone(equation_issue(question,WORK,rows))

    def test_unsupported_prefix_duplicate_partial_and_uncertain_quotes_skipped(self):
        first=WORK.splitlines()[0]
        for work,quote in [('先猜答案\n'+WORK,first),(first+'\n'+first,first),(WORK,'x=20/5')]:
            self.assertIsNone(equation_issue(QUESTION,work,[{'student_excerpt':quote,'verdict':'incorrect'}]))
        self.assertIsNone(equation_issue(QUESTION,WORK,[{'student_excerpt':first,'verdict':'uncertain'}]))

    def test_valid_alternative_accepted_and_false_error_blocked_without_rewriting(self):
        value=analysis();self.assertEqual(value,validate_analysis(value,student_work=WORK,work_kind='steps',question=QUESTION))
        value=analysis(wrong=True);before=deepcopy(value)
        with self.assertRaises(AnalysisValidationError) as caught:
            validate_analysis(value,student_work=WORK,work_kind='steps',question=QUESTION)
        self.assertEqual(ISSUE,caught.exception.code);self.assertEqual(before,value)

    def test_new_save_blocks_false_error_but_legacy_read_and_repeat_are_compatible(self):
        value=analysis(wrong=True)
        entry=make_entry(uuid4().hex,question=QUESTION,level='初中',my_work=WORK,analysis=value)
        with tempfile.TemporaryDirectory() as temp:
            book=Notebook(Path(temp)/'new')
            with self.assertRaises(AnalysisValidationError):book.save_new(entry)
            self.assertFalse(book.directory.exists())
            book.directory.mkdir();book.path(entry['id']).write_text(json.dumps(entry,ensure_ascii=False))
            before=book.path(entry['id']).read_bytes()
            self.assertEqual(entry,book.get(entry['id']))
            self.assertEqual('already_saved',book.save_new(entry))
            self.assertEqual(before,book.path(entry['id']).read_bytes())

    def test_correction_also_receives_question_and_rejects_false_error(self):
        value={'schema_version':1,'analysis':analysis(wrong=True),'comparison':{'summary':'方法对照','changes':[]}}
        with self.assertRaises(AnalysisValidationError) as caught:
            validate_result(value,previous_work='',previous_analysis=None,answer=WORK,work_kind='steps',question=QUESTION)
        self.assertEqual(ISSUE,caught.exception.code)

    def test_notebook_correction_preserves_original_when_equation_verdict_is_false(self):
        with tempfile.TemporaryDirectory() as temp:
            book=Notebook(temp)
            entry=make_entry(uuid4().hex,question=QUESTION,level='初中',my_work=WORK,analysis=analysis())
            book.save_new(entry);before=book.path(entry['id']).read_bytes()
            result={'schema_version':1,'analysis':analysis(wrong=True),'comparison':{'summary':'方法对照','changes':[]}}
            with self.assertRaises(AnalysisValidationError):
                book.add_correction(entry['id'],1,uuid4().hex,based_on='original',answer=WORK,
                                    work_kind='steps',result=result,analysis_origin='本机手写测试')
            self.assertEqual(before,book.path(entry['id']).read_bytes())


class ClarificationTests(unittest.TestCase):
    def test_minimal_clarification_accepted_unchanged(self):
        value=clarification();before=deepcopy(value)
        self.assertEqual(value,validate_analysis(value,student_work='',work_kind='none'))
        self.assertEqual(before,value)

    def test_clarification_still_requires_question_and_forbids_answer_and_steps(self):
        for field,new,code in [('clarification','','clarification_required'),
                               ('answer','13','clarification_contains_answer'),
                               ('steps',['假设增加5，所以原数13。'],'clarification_contains_solution')]:
            value=clarification();value[field]=new
            with self.subTest(field=field), self.assertRaises(AnalysisValidationError) as caught:
                validate_analysis(value,student_work='',work_kind='none')
            self.assertEqual(code,caught.exception.code)

    def test_solved_still_requires_summary_and_next_practice(self):
        for field,code in [('summary','analysis_summary_required'),('next_practice','analysis_next_practice_required')]:
            value=analysis();value[field]=''
            with self.assertRaises(AnalysisValidationError) as caught:
                validate_analysis(value,student_work=WORK,work_kind='steps',question=QUESTION)
            self.assertEqual(code,caught.exception.code)

    def test_wrong_types_and_oversized_optional_clarification_text_still_rejected(self):
        for field,new in [('summary',None),('next_practice',[]),('summary','a'*4001)]:
            value=clarification();value[field]=new
            with self.assertRaises(ValueError):validate_analysis(value,student_work='',work_kind='none')


class ServiceTests(unittest.TestCase):
    def test_agent_and_fixed_baseline_both_use_confirmed_equation(self):
        from study.agent import AgentStudyService
        from study.agent_tools import ToolScope
        from study.evaluation_baseline import FixedWorkflowService
        from study.context import learning,task_for
        from study.preferences import DEFAULTS
        from study.service import PhotoTransport
        from test_study_agent import final
        for cls in (AgentStudyService,FixedWorkflowService):
            with self.subTest(service=cls.__name__), tempfile.TemporaryDirectory() as temp, LocalModelServer() as server:
                server.body=final(analysis(wrong=True))
                options={'scope':ToolScope(Path(temp)/'notebook'),'transport':PhotoTransport(server.chat_url)}
                if cls is AgentStudyService:options['audit_dir']=Path(temp)/'audit'
                tutor=cls(default_config(),'local-test-key',**options)
                context=learning(task_for(None,'task','revision',{}),DEFAULTS)
                with self.assertRaises(ValueError) as caught:
                    tutor.analyze(QUESTION,'初中',WORK,work_kind='steps',learning=context)
                self.assertEqual(ISSUE,caught.exception.code)
                self.assertEqual(1,len(server.requests))
                self.assertFalse((Path(temp)/'notebook').exists())

    def test_json_and_strict_clarification_audited_without_automatic_save(self):
        for mode in ('json_object','strict_tool'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temp, LocalModelServer() as server:
                audit=create_run(Path(temp)/'run',mode='local_http_test',row_ids=['b05'],output_mode=mode)
                fixture=local_fixture(audit)
                def reply(payload):
                    envelope=fixture(payload);message=envelope['choices'][0]['message'];raw=json.dumps(clarification())
                    if mode=='strict_tool':message['tool_calls'][0]['function']['arguments']=raw
                    else:message['content']=raw
                    return envelope
                server.body=reply;status=execute(audit,server=server)
                self.assertEqual(1,status['counts']['reply_valid']);self.assertEqual(1,len(server.requests))
                self.assertEqual('',audit.row('b05')['result']['summary'])
                self.assertFalse((audit.directory/'notebook').exists())

    def test_strict_false_equation_error_gets_diagnostic_and_stops_next_request(self):
        with tempfile.TemporaryDirectory() as temp, LocalModelServer() as server:
            audit=create_run(Path(temp)/'run',mode='local_http_test',row_ids=['b04','b05'],output_mode='strict_tool')
            fixture=local_fixture(audit)
            def reply(payload):
                envelope=fixture(payload)
                envelope['choices'][0]['message']['tool_calls'][0]['function']['arguments']=json.dumps(analysis(wrong=True))
                return envelope
            server.body=reply;status=execute(audit,server=server)
            self.assertEqual(1,status['counts']['failed']);self.assertEqual(1,status['counts']['pending'])
            self.assertEqual(ISSUE,audit.row('b04')['call']['validation_issue'])
            self.assertEqual(1,len(server.requests));self.assertIsNone(audit.row('b04')['result'])
            self.assertFalse((audit.directory/'notebook').exists())


if __name__ == '__main__':unittest.main()
