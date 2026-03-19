#!/usr/bin/env python3
"""多人档案管理核心模块

被 web/server.py 和 tools/generate_resume.py 共同引用。
管理 data/persons.json 注册表和各人员的数据目录。
"""

import json
import re
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / 'data'
PERSONS_FILE = DATA_DIR / 'persons.json'
SHARED_DIR = DATA_DIR / '_shared'


def _read_persons() -> dict:
    """读取 persons.json，不存在则返回空结构"""
    if not PERSONS_FILE.exists():
        return {'active': None, 'persons': []}
    return json.loads(PERSONS_FILE.read_text(encoding='utf-8'))


def _write_persons(data: dict):
    """写入 persons.json"""
    PERSONS_FILE.parent.mkdir(parents=True, exist_ok=True)
    PERSONS_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding='utf-8',
    )


def is_multi_person_mode() -> bool:
    """判断是否已初始化多人模式（persons.json 存在）"""
    return PERSONS_FILE.exists()


def get_active_person_id() -> Optional[str]:
    """获取当前活跃人员 ID。未初始化时返回 None（legacy 模式）。"""
    if not is_multi_person_mode():
        return None
    data = _read_persons()
    return data.get('active')


def get_person_data_dir(person_id: Optional[str] = None) -> Path:
    """获取人员数据根目录。person_id=None 时回退 legacy 模式。"""
    if person_id is None:
        return DATA_DIR
    return DATA_DIR / person_id


def get_person_profile_path(person_id: Optional[str] = None) -> Path:
    """获取 profile.md 路径"""
    return get_person_data_dir(person_id) / 'profile.md'


def get_person_experiences_dir(person_id: Optional[str] = None) -> Path:
    """获取 experiences 目录路径"""
    return get_person_data_dir(person_id) / 'experiences'


def get_person_work_materials_dir(person_id: Optional[str] = None) -> Path:
    """获取 work_materials 目录路径"""
    return get_person_data_dir(person_id) / 'work_materials'


def get_person_output_dir(person_id: Optional[str] = None) -> Path:
    """获取输出目录路径"""
    output_base = PROJECT_ROOT / 'output'
    if person_id is None:
        return output_base
    return output_base / person_id


def list_persons() -> List[Dict]:
    """返回所有人员列表"""
    data = _read_persons()
    return data.get('persons', [])


def get_person(person_id: str) -> Optional[Dict]:
    """获取指定人员信息"""
    for p in list_persons():
        if p['id'] == person_id:
            return p
    return None


def sanitize_person_id(name: str) -> str:
    """将显示名转为文件系统安全的 slug ID"""
    # 先尝试用 ascii 拼音
    slug = name.strip().lower()
    # 替换空格和特殊字符
    slug = re.sub(r'[^\w\u4e00-\u9fff-]', '_', slug)
    slug = re.sub(r'_+', '_', slug).strip('_')
    if not slug:
        slug = 'person'
    # 确保不与保留名冲突
    reserved = {'_shared', '_template', 'persons'}
    if slug in reserved:
        slug = slug + '_1'
    return slug


def _ensure_unique_id(base_id: str) -> str:
    """确保 ID 不与已有人员重复"""
    existing_ids = {p['id'] for p in list_persons()}
    if base_id not in existing_ids:
        return base_id
    i = 2
    while f'{base_id}_{i}' in existing_ids:
        i += 1
    return f'{base_id}_{i}'


def create_person(display_name: str, person_id: str = None) -> dict:
    """创建新人员，返回人员信息 dict"""
    if not display_name.strip():
        raise ValueError('显示名不能为空')

    data = _read_persons()

    # 生成 ID
    if person_id:
        base_id = sanitize_person_id(person_id)
    else:
        base_id = sanitize_person_id(display_name)
    final_id = _ensure_unique_id(base_id)

    person = {
        'id': final_id,
        'display_name': display_name.strip(),
        'created_at': datetime.now().isoformat(timespec='seconds'),
    }
    data['persons'].append(person)

    # 如果是第一个人员，自动设为活跃
    if data.get('active') is None:
        data['active'] = final_id

    _write_persons(data)

    # 创建目录结构
    person_dir = DATA_DIR / final_id
    (person_dir / 'experiences').mkdir(parents=True, exist_ok=True)
    (person_dir / 'work_materials').mkdir(parents=True, exist_ok=True)

    # 复制共享模板到新人员的 experiences 目录
    shared_exp = SHARED_DIR / 'experiences'
    if shared_exp.exists():
        for tmpl_file in shared_exp.iterdir():
            dest = person_dir / 'experiences' / tmpl_file.name
            if not dest.exists():
                import shutil
                shutil.copy2(tmpl_file, dest)

    return person


def set_active_person(person_id: str):
    """设置活跃人员"""
    data = _read_persons()
    if not any(p['id'] == person_id for p in data['persons']):
        raise ValueError(f'人员不存在: {person_id}')
    data['active'] = person_id
    _write_persons(data)


def delete_person(person_id: str, delete_data: bool = False):
    """删除人员。默认不删除数据目录。"""
    data = _read_persons()
    data['persons'] = [p for p in data['persons'] if p['id'] != person_id]

    # 如果删除的是活跃人员，切换到第一个
    if data.get('active') == person_id:
        data['active'] = data['persons'][0]['id'] if data['persons'] else None

    _write_persons(data)

    if delete_data:
        import shutil
        person_dir = DATA_DIR / person_id
        if person_dir.exists() and person_dir != DATA_DIR:
            shutil.rmtree(person_dir)
        output_dir = PROJECT_ROOT / 'output' / person_id
        if output_dir.exists():
            shutil.rmtree(output_dir)


def rename_person(person_id: str, new_display_name: str):
    """重命名人员的显示名"""
    data = _read_persons()
    for p in data['persons']:
        if p['id'] == person_id:
            p['display_name'] = new_display_name.strip()
            _write_persons(data)
            return
    raise ValueError(f'人员不存在: {person_id}')
