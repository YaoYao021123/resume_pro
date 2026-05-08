"""Shared test fixtures and helpers for resume_generator_pro tests.

Provides temporary directory setup and common data builders to avoid
coupling tests to the real data/ directory.
"""

import json
import os
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class TmpDataDir:
    """Context manager that creates an isolated data directory structure
    and patches the module-level paths in person_manager / generate_resume
    so that tests never touch real user data.

    Usage::

        with TmpDataDir() as t:
            t.write_profile(person_id='alice', content='...')
            t.write_experience(person_id='alice', filename='01_foo.md', content='...')
            # run code that imports person_manager...
    """

    MINIMAL_PROFILE = """\
# 个人信息

## 基本信息

```
姓名（中文）：张三
姓名（英文）：San Zhang
邮箱：san@example.com
电话：13800000000
```

## 教育背景

```
学校：示例大学
学历：硕士
专业：计算机科学
学院：信息学院
时间：2022/09 -- 2025/06
课程：数据结构；算法设计；机器学习
```

## 语言与技能

```
语言能力：英语（CET-6）
技术技能：Python, SQL, Pandas
软件工具：VS Code, Git
```
"""

    MINIMAL_EXPERIENCE = """\
# 公司名

## 基本信息

```
公司：示例公司
城市：北京
部门：技术部
职位：数据分析实习生
时间：2024/06 -- 2024/09
标签：数据分析, Python, SQL
```

## 工作内容

### 数据分析

完成了数据清洗与分析工作，处理超过100万条数据记录，产出3份深度分析报告

## 备注

日常数据分析和报告撰写
"""

    def __init__(self):
        self._tmpdir = None
        self._patches = []

    def __enter__(self):
        self._tmpdir = tempfile.mkdtemp(prefix='resume_test_')
        self._tmp_path = Path(self._tmpdir)

        # Create base structure
        (self._tmp_path / 'data').mkdir()
        (self._tmp_path / 'data' / '_shared' / 'experiences').mkdir(parents=True)
        (self._tmp_path / 'output').mkdir()
        (self._tmp_path / 'latex_src' / 'resume').mkdir(parents=True)

        # Patch module-level paths
        import tools.person_manager as pm
        self._orig_pm = {
            'PROJECT_ROOT': pm.PROJECT_ROOT,
            'DATA_DIR': pm.DATA_DIR,
            'PERSONS_FILE': pm.PERSONS_FILE,
            'SHARED_DIR': pm.SHARED_DIR,
        }
        pm.PROJECT_ROOT = self._tmp_path
        pm.DATA_DIR = self._tmp_path / 'data'
        pm.PERSONS_FILE = self._tmp_path / 'data' / 'persons.json'
        pm.SHARED_DIR = self._tmp_path / 'data' / '_shared'

        return self

    def __exit__(self, *args):
        # Restore module-level paths
        import tools.person_manager as pm
        for attr, val in self._orig_pm.items():
            setattr(pm, attr, val)

        # Clean up temp dir
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    @property
    def root(self) -> Path:
        return self._tmp_path

    @property
    def data_dir(self) -> Path:
        return self._tmp_path / 'data'

    def write_persons(self, data: dict):
        """Write persons.json."""
        path = self.data_dir / 'persons.json'
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')

    def init_person(self, person_id: str = 'default', display_name: str = '测试用户'):
        """Initialize a person with directories and persons.json."""
        person_dir = self.data_dir / person_id
        (person_dir / 'experiences').mkdir(parents=True, exist_ok=True)
        (person_dir / 'work_materials').mkdir(parents=True, exist_ok=True)
        self.write_persons({
            'active': person_id,
            'persons': [{'id': person_id, 'display_name': display_name, 'created_at': '2026-01-01T00:00:00'}],
        })

    def write_profile(self, person_id: str = 'default', content: str = None):
        """Write a profile.md for the given person."""
        if content is None:
            content = self.MINIMAL_PROFILE
        path = self.data_dir / person_id / 'profile.md'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding='utf-8')

    def write_experience(self, person_id: str = 'default', filename: str = '01_示例公司.md', content: str = None):
        """Write an experience file."""
        if content is None:
            content = self.MINIMAL_EXPERIENCE
        path = self.data_dir / person_id / 'experiences' / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding='utf-8')
