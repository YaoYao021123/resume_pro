"""Tests for tools/person_manager.py"""

import json
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tests.conftest import TmpDataDir
from tools.person_manager import sanitize_person_id


class SanitizePersonIdTests(unittest.TestCase):
    """Pure function — no filesystem needed."""

    def test_basic_slug(self):
        self.assertEqual(sanitize_person_id('Alice Bob'), 'alice_bob')

    def test_chinese_characters_preserved(self):
        result = sanitize_person_id('张三')
        self.assertEqual(result, '张三')

    def test_special_chars_replaced(self):
        result = sanitize_person_id('user@name!#')
        self.assertNotIn('@', result)
        self.assertNotIn('!', result)

    def test_empty_defaults_to_person(self):
        self.assertEqual(sanitize_person_id(''), 'person')
        self.assertEqual(sanitize_person_id('   '), 'person')

    def test_reserved_names_get_suffix(self):
        # '_shared' is stripped to 'shared' (strip('_')), so not reserved
        self.assertEqual(sanitize_person_id('_shared'), 'shared')
        self.assertEqual(sanitize_person_id('_template'), 'template')
        # 'persons' stays as-is and hits reserved check
        self.assertEqual(sanitize_person_id('persons'), 'persons_1')

    def test_consecutive_underscores_collapsed(self):
        result = sanitize_person_id('a   b   c')
        self.assertNotIn('__', result)

    def test_leading_trailing_underscores_stripped(self):
        result = sanitize_person_id(' _test_ ')
        self.assertFalse(result.startswith('_') and result != '_shared_1')


class CreatePersonTests(unittest.TestCase):

    def test_first_person_is_active(self):
        with TmpDataDir() as t:
            from tools import person_manager as pm
            person = pm.create_person('Alice')
            data = json.loads((t.data_dir / 'persons.json').read_text())
            self.assertEqual(data['active'], person['id'])

    def test_creates_directories(self):
        with TmpDataDir() as t:
            from tools import person_manager as pm
            person = pm.create_person('Bob')
            pid = person['id']
            self.assertTrue((t.data_dir / pid / 'experiences').is_dir())
            self.assertTrue((t.data_dir / pid / 'work_materials').is_dir())

    def test_unique_id_when_duplicate(self):
        with TmpDataDir() as t:
            from tools import person_manager as pm
            p1 = pm.create_person('Test')
            p2 = pm.create_person('Test')
            self.assertNotEqual(p1['id'], p2['id'])
            self.assertTrue(p2['id'].endswith('_2'))

    def test_empty_name_raises(self):
        with TmpDataDir() as t:
            from tools import person_manager as pm
            with self.assertRaises(ValueError):
                pm.create_person('')

    def test_custom_person_id(self):
        with TmpDataDir() as t:
            from tools import person_manager as pm
            person = pm.create_person('Alice', person_id='custom_id')
            self.assertEqual(person['id'], 'custom_id')


class SetActivePersonTests(unittest.TestCase):

    def test_nonexistent_raises(self):
        with TmpDataDir() as t:
            from tools import person_manager as pm
            t.write_persons({'active': None, 'persons': []})
            with self.assertRaises(ValueError):
                pm.set_active_person('nonexistent')

    def test_switch_active(self):
        with TmpDataDir() as t:
            from tools import person_manager as pm
            p1 = pm.create_person('A')
            p2 = pm.create_person('B')
            pm.set_active_person(p2['id'])
            self.assertEqual(pm.get_active_person_id(), p2['id'])


class DeletePersonTests(unittest.TestCase):

    def test_reassigns_active_when_deleting_active(self):
        with TmpDataDir() as t:
            from tools import person_manager as pm
            p1 = pm.create_person('A')
            p2 = pm.create_person('B')
            pm.set_active_person(p1['id'])
            pm.delete_person(p1['id'])
            self.assertEqual(pm.get_active_person_id(), p2['id'])

    def test_delete_with_data_cleanup(self):
        with TmpDataDir() as t:
            from tools import person_manager as pm
            person = pm.create_person('Temp')
            pid = person['id']
            self.assertTrue((t.data_dir / pid).exists())
            pm.delete_person(pid, delete_data=True)
            self.assertFalse((t.data_dir / pid).exists())

    def test_delete_last_person_sets_active_none(self):
        with TmpDataDir() as t:
            from tools import person_manager as pm
            person = pm.create_person('Only')
            pm.delete_person(person['id'])
            self.assertIsNone(pm.get_active_person_id())


class RenamePersonTests(unittest.TestCase):

    def test_rename_success(self):
        with TmpDataDir() as t:
            from tools import person_manager as pm
            person = pm.create_person('Old Name')
            pm.rename_person(person['id'], 'New Name')
            updated = pm.get_person(person['id'])
            self.assertEqual(updated['display_name'], 'New Name')

    def test_rename_nonexistent_raises(self):
        with TmpDataDir() as t:
            from tools import person_manager as pm
            t.write_persons({'active': None, 'persons': []})
            with self.assertRaises(ValueError):
                pm.rename_person('ghost', 'New')


class ListAndGetTests(unittest.TestCase):

    def test_list_persons_empty(self):
        with TmpDataDir() as t:
            from tools import person_manager as pm
            t.write_persons({'active': None, 'persons': []})
            self.assertEqual(pm.list_persons(), [])

    def test_get_person_found(self):
        with TmpDataDir() as t:
            from tools import person_manager as pm
            person = pm.create_person('Found')
            result = pm.get_person(person['id'])
            self.assertIsNotNone(result)
            self.assertEqual(result['display_name'], 'Found')

    def test_get_person_not_found(self):
        with TmpDataDir() as t:
            from tools import person_manager as pm
            t.write_persons({'active': None, 'persons': []})
            self.assertIsNone(pm.get_person('nonexistent'))


class PathResolutionTests(unittest.TestCase):

    def test_legacy_mode_returns_data_dir(self):
        from tools import person_manager as pm
        result = pm.get_person_data_dir(None)
        self.assertEqual(result, pm.DATA_DIR)

    def test_person_mode_returns_subdir(self):
        from tools import person_manager as pm
        result = pm.get_person_data_dir('alice')
        self.assertEqual(result.name, 'alice')

    def test_profile_path(self):
        from tools import person_manager as pm
        result = pm.get_person_profile_path('bob')
        self.assertEqual(result.name, 'profile.md')
        self.assertIn('bob', str(result))


if __name__ == '__main__':
    unittest.main()
