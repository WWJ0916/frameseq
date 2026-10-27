import json,tempfile,unittest
from pathlib import Path
from scripts.create_admin_trial import create_admin_trial
from video_platform.auth import AuthStore

class AdminTrialTests(unittest.TestCase):
    def test_additional_admin_preserves_operator_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);auth=AuthStore(root)
            operator=auth.create_user('operator','fixture-admin-password','Operator',admin=True)
            first=create_admin_trial(root);self.assertTrue(first['created'])
            secret=json.loads(Path(first['credential_file']).read_text())
            _,user,_=auth.login(secret['username'],secret['password']);self.assertEqual(user['role'],'admin');self.assertNotEqual(user['id'],operator['id'])
            self.assertNotIn('password',first)
            self.assertFalse(create_admin_trial(root)['created'])
            self.assertEqual(auth.login('operator','fixture-admin-password')[1]['id'],operator['id'])

    def test_existing_creator_is_never_promoted_or_password_reset(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);auth=AuthStore(root);admin=auth.create_user('operator','fixture-admin-password','Operator',admin=True)
            user=auth.create_user('admin_trial','fixture-user-password','User',invite=auth.invite(admin['id']))
            with self.assertRaises(ValueError):create_admin_trial(root)
            self.assertEqual(auth.login('admin_trial','fixture-user-password')[1]['role'],'creator')
            self.assertFalse((root/'.secrets/admin-trial-admin_trial.json').exists())
