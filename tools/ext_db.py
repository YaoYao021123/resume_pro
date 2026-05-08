#!/usr/bin/env python3
"""Chrome 扩展后端 — SQLite 数据库管理

管理四张表：
- fill_history: 填充历史
- corrections: 用户修正记录
- field_mappings: 字段映射学习
- applications: 投递记录
"""

import sqlite3
from datetime import datetime
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / 'data' / 'extension.db'


def _get_conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA foreign_keys=ON')
    return conn


def init_db():
    """初始化数据库表"""
    conn = _get_conn()
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS fill_history (
            id INTEGER PRIMARY KEY,
            url TEXT,
            platform TEXT,
            fields_filled INTEGER DEFAULT 0,
            fields_corrected INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS corrections (
            id INTEGER PRIMARY KEY,
            fill_id INTEGER REFERENCES fill_history(id),
            field_name TEXT,
            field_label TEXT,
            original_value TEXT,
            corrected_value TEXT,
            platform TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS field_mappings (
            id INTEGER PRIMARY KEY,
            platform TEXT,
            field_selector TEXT,
            field_label TEXT,
            mapped_to TEXT,
            confidence REAL DEFAULT 0.5,
            use_count INTEGER DEFAULT 0,
            updated_at TEXT DEFAULT (datetime('now')),
            UNIQUE(platform, field_selector)
        );

        CREATE TABLE IF NOT EXISTS applications (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            company         TEXT DEFAULT '',
            role            TEXT DEFAULT '',
            status          TEXT DEFAULT '投递',
            url             TEXT DEFAULT '',
            platform        TEXT DEFAULT '',
            resume_dir      TEXT DEFAULT '',
            applied_date    TEXT DEFAULT (date('now')),
            notes           TEXT DEFAULT '',
            fill_id         INTEGER,
            feishu_record_id TEXT DEFAULT '',
            created_at      TEXT DEFAULT (datetime('now')),
            updated_at      TEXT DEFAULT (datetime('now'))
        );
    ''')
    conn.commit()
    conn.close()


def log_fill(url: str, platform: str, fields_filled: int) -> int:
    """记录一次填充操作，返回 fill_id"""
    conn = _get_conn()
    cur = conn.execute(
        'INSERT INTO fill_history (url, platform, fields_filled) VALUES (?, ?, ?)',
        (url, platform, fields_filled)
    )
    fill_id = cur.lastrowid
    conn.commit()
    conn.close()
    return fill_id


def log_correction(fill_id: int, field_name: str, field_label: str,
                   original_value: str, corrected_value: str, platform: str):
    """记录用户修正"""
    conn = _get_conn()
    conn.execute(
        '''INSERT INTO corrections
           (fill_id, field_name, field_label, original_value, corrected_value, platform)
           VALUES (?, ?, ?, ?, ?, ?)''',
        (fill_id, field_name, field_label, original_value, corrected_value, platform)
    )
    # 更新 fill_history 的修正计数
    conn.execute(
        '''UPDATE fill_history
           SET fields_corrected = (
               SELECT COUNT(*) FROM corrections WHERE corrections.fill_id = ?
           )
           WHERE id = ?''',
        (fill_id, fill_id)
    )
    conn.commit()
    conn.close()


def get_field_mappings(platform: str = None) -> list:
    """获取字段映射规则"""
    conn = _get_conn()
    if platform:
        rows = conn.execute(
            'SELECT * FROM field_mappings WHERE platform = ? ORDER BY confidence DESC',
            (platform,)
        ).fetchall()
    else:
        rows = conn.execute(
            'SELECT * FROM field_mappings ORDER BY platform, confidence DESC'
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def update_field_mapping(platform: str, field_selector: str, field_label: str,
                         mapped_to: str, confidence: float = 0.5):
    """更新或插入字段映射"""
    conn = _get_conn()
    conn.execute(
        '''INSERT INTO field_mappings (platform, field_selector, field_label, mapped_to, confidence, use_count, updated_at)
           VALUES (?, ?, ?, ?, ?, 1, datetime('now'))
           ON CONFLICT(platform, field_selector) DO UPDATE SET
               field_label = excluded.field_label,
               mapped_to = excluded.mapped_to,
               confidence = excluded.confidence,
               use_count = use_count + 1,
               updated_at = datetime('now')''',
        (platform, field_selector, field_label, mapped_to, confidence)
    )
    conn.commit()
    conn.close()


def get_corrections_summary(limit: int = 50) -> dict:
    """获取修正汇总统计"""
    conn = _get_conn()

    # 最近的修正记录
    recent = conn.execute(
        '''SELECT c.*, fh.url, fh.platform as fill_platform
           FROM corrections c
           LEFT JOIN fill_history fh ON c.fill_id = fh.id
           ORDER BY c.created_at DESC LIMIT ?''',
        (limit,)
    ).fetchall()

    # 按字段统计修正频率
    field_stats = conn.execute(
        '''SELECT field_name, field_label, platform, COUNT(*) as count
           FROM corrections
           GROUP BY field_name, platform
           ORDER BY count DESC'''
    ).fetchall()

    # 填充历史统计
    fill_stats = conn.execute(
        '''SELECT platform, COUNT(*) as fills,
                  SUM(fields_filled) as total_filled,
                  SUM(fields_corrected) as total_corrected
           FROM fill_history
           GROUP BY platform'''
    ).fetchall()

    conn.close()

    return {
        'recent_corrections': [dict(r) for r in recent],
        'field_stats': [dict(r) for r in field_stats],
        'fill_stats': [dict(r) for r in fill_stats],
    }


def get_fill_history(limit: int = 20) -> list:
    """获取填充历史"""
    conn = _get_conn()
    rows = conn.execute(
        'SELECT * FROM fill_history ORDER BY created_at DESC LIMIT ?',
        (limit,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ─── 投递记录 CRUD ─────────────────────────────────────

def create_application(company: str = '', role: str = '', url: str = '',
                       platform: str = '', resume_dir: str = '',
                       status: str = '投递', notes: str = '',
                       fill_id: int = None) -> int:
    """创建投递记录，返回 id"""
    conn = _get_conn()
    cur = conn.execute(
        '''INSERT INTO applications
           (company, role, status, url, platform, resume_dir, notes, fill_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
        (company, role, status, url, platform, resume_dir, notes, fill_id)
    )
    app_id = cur.lastrowid
    conn.commit()
    conn.close()
    return app_id


def get_applications(limit: int = 200, status: str = None) -> list:
    """获取投递记录列表"""
    conn = _get_conn()
    if status:
        rows = conn.execute(
            'SELECT * FROM applications WHERE status = ? ORDER BY applied_date DESC, id DESC LIMIT ?',
            (status, limit)
        ).fetchall()
    else:
        rows = conn.execute(
            'SELECT * FROM applications ORDER BY applied_date DESC, id DESC LIMIT ?',
            (limit,)
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def update_application(app_id: int, **fields) -> bool:
    """更新投递记录的指定字段，返回是否成功"""
    allowed = {'company', 'role', 'status', 'url', 'applied_date',
               'notes', 'resume_dir', 'platform', 'feishu_record_id'}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return False
    updates['updated_at'] = datetime.now().isoformat()
    set_clause = ', '.join(f'{k} = ?' for k in updates)
    values = list(updates.values()) + [app_id]
    conn = _get_conn()
    cur = conn.execute(
        f'UPDATE applications SET {set_clause} WHERE id = ?', values
    )
    affected = cur.rowcount
    conn.commit()
    conn.close()
    return affected > 0


def delete_application(app_id: int) -> bool:
    """删除投递记录，返回是否成功"""
    conn = _get_conn()
    cur = conn.execute('DELETE FROM applications WHERE id = ?', (app_id,))
    affected = cur.rowcount
    conn.commit()
    conn.close()
    return affected > 0


# 模块加载时自动初始化
init_db()
