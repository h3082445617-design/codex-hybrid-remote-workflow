"""Behavioral tests with synthetic credentials; no real services or accounts."""
import contextlib
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import tomllib
import types
import unittest
from unittest import mock
import urllib.error

SPEC = importlib.util.spec_from_file_location('workflow', Path(__file__).resolve().parents[1] / 'scripts/hybrid_remote.py')
w = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(w)

FAKE_KEY = 'unit-test-only-not-a-real-credential'
OAUTH = {'auth_mode': 'chatgpt', 'tokens': {
    'access_token': 'synthetic-access', 'refresh_token': 'synthetic-refresh', 'id_token': 'synthetic-id'}}
CONFIG = '''# Keep this comment.
model = "gpt-6-astra"
model_provider = "gateway"
model_reasoning_effort = "xhigh"
notify = ["notify-helper", "keep me"]
[model_providers.gateway]
name = "Existing gateway"
base_url = "https://gateway.example/v1"
wire_api = "responses"
requires_openai_auth = true
stream_idle_timeout_ms = 300000
[projects."/example/project"]
trust_level = "trusted"
'''


class FakeManagement:
    def __init__(self):
        self.base = 'http://127.0.0.1:10100'
        self.token_path = Path('/synthetic/admin-token')
        self.combos = {}
        self.authless = True
        self.unrelated = {'preserve': 'original'}
        self.calls = []
        self.fail_settings_once = False

    def request(self, path, method='GET', body=None):
        self.calls.append((path, method))
        if path == '/readyz':
            return {'status': 'ready'}
        if path == '/api/combos' and method == 'GET':
            return {'combos': [{'id': k, 'model': v['alias'], **copy.deepcopy(v)} for k, v in self.combos.items()]}
        if path == '/api/combos' and method == 'PUT':
            self.combos[body['id']] = copy.deepcopy(body['combo'])
            return {'success': True}
        if path.startswith('/api/combos?id=') and method == 'DELETE':
            del self.combos[path.split('=', 1)[1]]
            return {'success': True}
        if path == '/api/settings' and method == 'GET':
            return {'codexDesktopAuthless': self.authless}
        if path == '/api/settings' and method == 'PUT':
            if self.fail_settings_once:
                self.fail_settings_once = False
                raise w.WorkflowError('Simulated settings failure.')
            self.authless = body['codexDesktopAuthless']
            return {'ok': True}
        raise AssertionError('Unexpected fake management operation')


class ConfigTests(unittest.TestCase):
    def test_only_managed_fields_change(self):
        result = w.patch_config(CONFIG, 'gateway', FAKE_KEY, 'http://127.0.0.1:10100/v1')
        actual, expected = tomllib.loads(result), tomllib.loads(CONFIG)
        self.assertIn('# Keep this comment.', result)
        expected['openai_base_url'] = 'http://127.0.0.1:10100/v1'
        expected['model_providers']['gateway']['experimental_bearer_token'] = FAKE_KEY
        self.assertEqual(actual, expected)
        self.assertEqual(result, w.patch_config(result, 'gateway', FAKE_KEY, expected['openai_base_url']))

    def test_key_with_quotes_backslash_and_newline_roundtrips(self):
        key = 'synthetic-"quoted"-\\-\n-line'
        result = w.patch_config(CONFIG, 'gateway', key, 'http://127.0.0.1:10100/v1')
        self.assertEqual(tomllib.loads(result)['model_providers']['gateway']['experimental_bearer_token'], key)

    def test_unrelated_base_url_is_rejected(self):
        config = 'openai_base_url = "https://other.example/v1"\n' + CONFIG
        with self.assertRaises(w.WorkflowError):
            w.patch_config(config, 'gateway', FAKE_KEY, 'http://127.0.0.1:10100/v1')

    def test_missing_table_is_rejected(self):
        with self.assertRaises(w.WorkflowError):
            w.patch_config(CONFIG, 'missing', FAKE_KEY, 'http://127.0.0.1:10100/v1')

    def test_real_oauth_fields_required(self):
        self.assertTrue(w.oauth_valid(OAUTH))
        self.assertFalse(w.oauth_valid({'auth_mode': 'apikey', 'tokens': OAUTH['tokens']}))
        self.assertFalse(w.oauth_valid({'auth_mode': 'chatgpt', 'tokens': {'access_token': 'synthetic'}}))

    def test_no_redirect_and_no_raw_http_error_output(self):
        self.assertIsNone(w.NoRedirect().redirect_request(None))
        with tempfile.TemporaryDirectory() as directory:
            token = Path(directory) / 'token'
            token.write_text(FAKE_KEY)
            api = w.Management(10100, token)
            opener = mock.Mock()
            opener.open.side_effect = urllib.error.HTTPError('http://localhost', 400, FAKE_KEY, {}, None)
            with mock.patch.object(w.urllib.request, 'build_opener', return_value=opener):
                with self.assertRaises(w.WorkflowError) as caught:
                    api.request('/api/combos')
            self.assertNotIn(FAKE_KEY, str(caught.exception))
            self.assertIn('HTTP 400', str(caught.exception))

    def test_atomic_write_permissions_and_symlink_guard(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'private'
            w.atomic_write(target, FAKE_KEY)
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)
            link = Path(directory) / 'link'
            link.symlink_to(target)
            with self.assertRaises(w.WorkflowError):
                w.atomic_write(link, 'changed')
            self.assertEqual(target.read_text(), FAKE_KEY)


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.codex, self.ocx = root / 'codex', root / 'opencodex'
        self.codex.mkdir()
        self.ocx.mkdir()
        (self.codex / 'config.toml').write_text(CONFIG)
        self.original_auth = json.dumps({'auth_mode': 'apikey', 'OPENAI_API_KEY': FAKE_KEY})
        (self.codex / 'auth.json').write_text(self.original_auth)
        (self.ocx / 'config.json').write_text(json.dumps({'port': 10100, 'providers': {
            'gateway-api': {'adapter': 'openai-responses', 'apiKey': FAKE_KEY, 'baseUrl': 'https://gateway.example/v1'}}}))
        self.oauth = root / 'official-auth.json'
        self.oauth.write_text(json.dumps(OAUTH))
        self.args = types.SimpleNamespace(codex_home=self.codex, opencodex_home=self.ocx,
            state_dir=root / 'private-state', oauth_auth=self.oauth, route_provider=None,
            combo_id='hybrid-test', port=None)
        self.api = FakeManagement()
        patcher = mock.patch.object(w, 'Management', return_value=self.api)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.output = io.StringIO()

    def apply(self):
        with contextlib.redirect_stdout(self.output):
            w.apply(self.args)
        return next(self.args.state_dir.glob('backup-*'))

    def test_apply_is_idempotent_and_rollback_preserves_unrelated_state(self):
        backup = self.apply()
        parsed = tomllib.loads((self.codex / 'config.toml').read_text())
        self.assertEqual(parsed['model'], 'gpt-6-astra')
        self.assertEqual(parsed['model_reasoning_effort'], 'xhigh')
        self.assertEqual(parsed['model_providers']['gateway']['experimental_bearer_token'], FAKE_KEY)
        self.assertEqual(json.loads((self.codex / 'auth.json').read_text()), OAUTH)
        self.assertFalse(self.api.authless)
        self.assertEqual(self.api.combos['hybrid-test']['targets'], [{'provider': 'gateway-api', 'model': 'gpt-6-astra'}])
        writes = [x for x in self.api.calls if x[1] != 'GET']
        self.apply()
        self.assertEqual(writes, [x for x in self.api.calls if x[1] != 'GET'])
        self.api.unrelated['added-later'] = True
        self.args.backup = backup
        with contextlib.redirect_stdout(self.output):
            w.rollback(self.args)
        self.assertEqual((self.codex / 'config.toml').read_text(), CONFIG)
        self.assertEqual((self.codex / 'auth.json').read_text(), self.original_auth)
        self.assertEqual(self.api.combos, {})
        self.assertTrue(self.api.authless)
        self.assertTrue(self.api.unrelated['added-later'])
        self.assertNotIn(FAKE_KEY, self.output.getvalue())
        self.assertNotIn('synthetic-refresh', self.output.getvalue())

    def test_rollback_refuses_subsequent_user_changes_before_mutation(self):
        self.args.backup = self.apply()
        with (self.codex / 'config.toml').open('a') as file:
            file.write('\n# User edit after activation\n')
        calls = len(self.api.calls)
        with self.assertRaises(w.WorkflowError):
            w.rollback(self.args)
        self.assertEqual(len(self.api.calls), calls)
        self.assertIn('User edit after activation', (self.codex / 'config.toml').read_text())

    def test_partial_apply_can_be_reconciled_and_rolled_back(self):
        self.api.fail_settings_once = True
        with contextlib.redirect_stdout(self.output), self.assertRaises(w.WorkflowError):
            w.apply(self.args)
        self.assertIn('hybrid-test', self.api.combos)
        self.assertEqual((self.codex / 'config.toml').read_text(), CONFIG)
        self.args.backup = next(self.args.state_dir.glob('backup-*'))
        with contextlib.redirect_stdout(self.output):
            w.rollback(self.args)
        self.assertEqual(self.api.combos, {})
        self.assertEqual(json.loads((self.args.backup / 'manifest.json').read_text())['stage'], 'rolled_back')

    def test_rollback_refuses_refreshed_oauth(self):
        self.args.backup = self.apply()
        refreshed = copy.deepcopy(OAUTH)
        refreshed['tokens']['access_token'] = 'synthetic-refreshed-access'
        (self.codex / 'auth.json').write_text(json.dumps(refreshed))
        calls = len(self.api.calls)
        with self.assertRaises(w.WorkflowError):
            w.rollback(self.args)
        self.assertEqual(len(self.api.calls), calls)
        self.assertEqual(json.loads((self.codex / 'auth.json').read_text()), refreshed)

    def test_invalid_oauth_does_not_modify_anything(self):
        self.oauth.write_text('{}')
        with self.assertRaises(w.WorkflowError):
            w.apply(self.args)
        self.assertFalse(self.args.state_dir.exists())
        self.assertFalse([x for x in self.api.calls if x[1] != 'GET'])

    def test_existing_alias_collision_does_not_modify_anything(self):
        self.api.combos['other'] = {'alias': 'gpt-6-astra', 'targets': []}
        with self.assertRaises(w.WorkflowError):
            w.prepare(self.args)
        self.assertEqual((self.codex / 'config.toml').read_text(), CONFIG)

    def test_forward_provider_is_not_accepted_as_key_gateway(self):
        path = self.ocx / 'config.json'
        config = json.loads(path.read_text())
        config['providers']['gateway-api']['authMode'] = 'forward'
        path.write_text(json.dumps(config))
        with self.assertRaises(w.WorkflowError):
            w.prepare(self.args)


if __name__ == '__main__':
    unittest.main()
