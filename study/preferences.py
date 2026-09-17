"""本机确认过的三项偏好和逐题历史权限；读取不创建文件。"""
from copy import deepcopy
from pathlib import Path
import fcntl
import re

from core import SETTING_OPTIONS
from study.run_audit import read_json, write_json

DEFAULTS={'step_size':'small','explanation_mode':'hint','presentation_density':'brief'}


def validate_settings(value):
    if type(value) is not dict or set(value)!=set(DEFAULTS):raise ValueError('学习设置只能包含已接通的三项。')
    if any(type(v) is not str or v not in SETTING_OPTIONS[k] for k,v in value.items()):
        raise ValueError('学习设置的选项无效。')
    return value


class LocalPreferences:
    def __init__(self,directory,*,history=False):
        self.directory=Path(directory);self.history=history
        self.path=self.directory/('history-controls.json' if history else 'learning-settings.json')
        self.field='records' if history else 'settings'

    def validate(self,value):
        if (type(value) is not dict or set(value)!={'schema_version','version',self.field}
                or type(value['schema_version']) is not int or value['schema_version']!=1
                or type(value['version']) is not int or value['version']<0):raise ValueError('本机设置文件无效，未覆盖。')
        data=value[self.field]
        if not self.history:validate_settings(data)
        elif (type(data) is not dict or len(data)>200 or any(
            type(k) is not str or not re.fullmatch(r'[a-f0-9]{32}',k) or type(v) is not dict or set(v)!={'enabled','include_model'}
            or any(type(x) is not bool for x in v.values()) for k,v in data.items())):
            raise ValueError('逐题历史权限无效，未覆盖。')
        return value

    def load(self):
        if self.directory.is_symlink() or self.path.is_symlink():raise ValueError('设置路径不能是符号链接。')
        return self.validate(read_json(self.path)) if self.path.exists() else {
            'schema_version':1,'version':0,self.field:{} if self.history else deepcopy(DEFAULTS)}

    def save(self,version,data):
        candidate=self.validate({'schema_version':1,'version':version,self.field:deepcopy(data)})
        self.load()  # 先检查原文件，损坏时不覆盖。
        self.directory.mkdir(parents=True,exist_ok=True,mode=0o700)
        lock=self.path.with_suffix('.lock')
        if lock.is_symlink():raise ValueError('设置锁路径无效。')
        with lock.open('a') as stream:
            fcntl.flock(stream,fcntl.LOCK_EX)
            current=self.load()
            if current[self.field]==data:return current
            if current['version']!=version:raise ValueError('设置已在另一处变化，请重新打开后确认。')
            candidate['version']+=1
            write_json(self.path,candidate)
            return candidate


def history_rules(directory):
    return LocalPreferences(Path(directory).parent,history=True).load()['records']
