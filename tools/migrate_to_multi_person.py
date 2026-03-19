#!/usr/bin/env python3
"""将旧的单人数据结构迁移到多人模式

检测条件：data/profile.md 存在 且 data/persons.json 不存在
幂等设计：已有 persons.json 则跳过

迁移步骤：
1. 从 profile.md 读取姓名
2. 创建 data/_shared/experiences/，移入 _template.md + README.md
3. 创建 data/default/，移入 profile.md、experiences/*.md、work_materials/*
4. 生成 persons.json
5. 移动 output/* 到 output/default/
"""

import json
import re
import shutil
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / 'data'
PERSONS_FILE = DATA_DIR / 'persons.json'
OUTPUT_DIR = PROJECT_ROOT / 'output'


def _extract_name_from_profile(profile_path: Path) -> str:
    """从 profile.md 中提取姓名"""
    if not profile_path.exists():
        return '默认'
    text = profile_path.read_text(encoding='utf-8')
    # 匹配 "姓名（中文）：XXX"
    m = re.search(r'姓名[（(]中文[)）][：:]\s*(.+)', text)
    if m:
        name = m.group(1).strip()
        if name and not name.startswith('['):
            return name
    return '默认'


def needs_migration() -> bool:
    """检查是否需要迁移"""
    profile = DATA_DIR / 'profile.md'
    return profile.exists() and not PERSONS_FILE.exists()


def migrate():
    """执行迁移"""
    if not needs_migration():
        return False

    print('[migrate] 检测到旧的单人数据结构，开始迁移到多人模式...')

    # --- Step 1: 读取姓名 ---
    old_profile = DATA_DIR / 'profile.md'
    display_name = _extract_name_from_profile(old_profile)
    print(f'[migrate] 从 profile.md 提取姓名: {display_name}')

    # --- Step 2: 创建 _shared/experiences/ ---
    shared_exp = DATA_DIR / '_shared' / 'experiences'
    shared_exp.mkdir(parents=True, exist_ok=True)

    old_exp_dir = DATA_DIR / 'experiences'
    if old_exp_dir.exists():
        for tmpl_name in ('_template.md', 'README.md'):
            src = old_exp_dir / tmpl_name
            if src.exists():
                dest = shared_exp / tmpl_name
                shutil.copy2(src, dest)
                print(f'[migrate] 复制共享模板: {tmpl_name}')

    # --- Step 3: 创建 data/default/ ---
    default_dir = DATA_DIR / 'default'
    default_dir.mkdir(parents=True, exist_ok=True)

    # 移动 profile.md
    if old_profile.exists():
        shutil.move(str(old_profile), str(default_dir / 'profile.md'))
        print('[migrate] 移动 profile.md → default/profile.md')

    # 移动 experiences/*.md（排除模板）
    default_exp = default_dir / 'experiences'
    default_exp.mkdir(parents=True, exist_ok=True)
    if old_exp_dir.exists():
        for f in old_exp_dir.iterdir():
            if f.is_file() and f.suffix == '.md':
                dest = default_exp / f.name
                shutil.move(str(f), str(dest))
                print(f'[migrate] 移动经历: {f.name}')
        # 清理空的旧 experiences 目录
        try:
            old_exp_dir.rmdir()
        except OSError:
            pass  # 目录非空（可能有子目录），留着

    # 移动 work_materials/
    old_wm = DATA_DIR / 'work_materials'
    default_wm = default_dir / 'work_materials'
    if old_wm.exists() and any(old_wm.iterdir()):
        if default_wm.exists():
            # 逐个移动子目录
            for item in old_wm.iterdir():
                if item.name == 'README.md' or item.name.startswith('.'):
                    continue
                dest = default_wm / item.name
                if not dest.exists():
                    shutil.move(str(item), str(dest))
                    print(f'[migrate] 移动工作材料: {item.name}')
        else:
            shutil.move(str(old_wm), str(default_wm))
            print('[migrate] 移动 work_materials/ → default/work_materials/')
    else:
        default_wm.mkdir(parents=True, exist_ok=True)

    # --- Step 4: 生成 persons.json ---
    persons_data = {
        'active': 'default',
        'persons': [
            {
                'id': 'default',
                'display_name': display_name,
                'created_at': datetime.now().isoformat(timespec='seconds'),
            }
        ],
    }
    PERSONS_FILE.write_text(
        json.dumps(persons_data, ensure_ascii=False, indent=2),
        encoding='utf-8',
    )
    print('[migrate] 生成 persons.json')

    # --- Step 5: 移动 output/ 内容到 output/default/ ---
    if OUTPUT_DIR.exists():
        items = [d for d in OUTPUT_DIR.iterdir()
                 if d.is_dir() and d.name != 'default' and not d.name.startswith('.')]
        if items:
            default_output = OUTPUT_DIR / 'default'
            default_output.mkdir(parents=True, exist_ok=True)
            for item in items:
                dest = default_output / item.name
                if not dest.exists():
                    shutil.move(str(item), str(dest))
                    print(f'[migrate] 移动输出: {item.name} → default/{item.name}')

    print('[migrate] 迁移完成！')
    return True


def maybe_migrate():
    """自动检测并迁移（可在启动时调用）"""
    if needs_migration():
        return migrate()
    return False


if __name__ == '__main__':
    if needs_migration():
        migrate()
    else:
        print('无需迁移（persons.json 已存在或 profile.md 不存在）')
