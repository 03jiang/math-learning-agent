"""三问读题卡：未选择、误解题意、修改选择；不保存状态、不调用模型。"""
from dataclasses import asdict
import json

from reading_check import READING_TASKS, check_reading


def main():
    for task_id in READING_TASKS:
        print(f'\n【{task_id}】固定选项练习，无真实模型')
        for label, choices, expected in (
            ('尚未选择', {}, 'incomplete'),
            ('把第二次误读成剩下部分，并把已用当成所求',
             {'whole': 'original', 'second_reference': 'remaining', 'target': 'used_fraction'}, 'retry'),
            ('修改后再次检查',
             {'whole': 'original', 'second_reference': 'original', 'target': 'remaining_fraction'}, 'matched'),
        ):
            result = check_reading(task_id, choices)
            assert result.status == expected
            print(label, '→', result.status)
            for row in result.rows:
                if row['status'] != 'correct':
                    print(' ', row['feedback'])
            print(result.next_prompt)
        print('最后一次选择：', json.dumps(asdict(result)['selections'], ensure_ascii=False))
    print('\n全部演示断言通过。选择相符不表示已掌握；本演示无文件写入、无进度更新、真实 API 调用 0 次。')


if __name__ == '__main__':
    main()
