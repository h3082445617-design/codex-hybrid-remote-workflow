#!/usr/bin/env python3
"""macOS ChatGPT OAuth + existing API gateway workflow (Python 3.11+)."""
from __future__ import annotations

import argparse
import copy
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request


class WorkflowError(Exception):
    """Messages in this class are deliberately safe for public output."""


def require(condition, message):
    if not condition:
        raise WorkflowError(message)


def emit(**fields):
    print(json.dumps(fields, ensure_ascii=False), flush=True)


def private_dir(path):
    path = Path(path)
    require(not path.is_symlink(), 'Private state directory must not be a symlink.')
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def atomic_write(path, content):
    path = Path(path)
    require(not path.is_symlink(), 'Refusing to replace a symlink.')
    payload = content.encode() if isinstance(content, str) else content
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name + '-', dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_json(path):
    return json.loads(Path(path).read_text())


def digest(data):
    return hashlib.sha256(data).hexdigest()


def oauth_valid(value):
    tokens = value.get('tokens') or {}
    return value.get('auth_mode') == 'chatgpt' and all(tokens.get(k) for k in ('access_token', 'refresh_token', 'id_token'))


def clean_env(home):
    env = dict(os.environ, CODEX_HOME=str(home))
    for name in ('OPENAI_API_KEY', 'OPENAI_BASE_URL', 'CODEX_ACCESS_TOKEN'):
        env.pop(name, None)
    return env


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class Management:
    def __init__(self, port, token_path):
        require(1 <= port <= 65535, 'Invalid loopback port.')
        self.base = f'http://127.0.0.1:{port}'
        self.token_path = Path(token_path)

    def request(self, path, method='GET', body=None):
        require(path.startswith('/') and not path.startswith('//'), 'Invalid management path.')
        headers = {'Accept': 'application/json', 'Origin': self.base}
        if path.startswith('/api/'):
            token = self.token_path.read_text().strip()
            require(bool(token), 'OpenCodex admin token is empty.')
            headers['Authorization'] = 'Bearer ' + token
        data = None if body is None else json.dumps(body).encode()
        if data is not None:
            headers['Content-Type'] = 'application/json'
        request = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.build_opener(NoRedirect).open(request, timeout=45) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            raise WorkflowError(f'OpenCodex returned HTTP {error.code}; response omitted to protect credentials.') from None
        except (OSError, ValueError):
            raise WorkflowError('OpenCodex request failed; response omitted. Check its local health and version.') from None


def semantic_combo(value):
    if value is None:
        return None
    return {
        'alias': value.get('alias'), 'nativeAlias': value.get('nativeAlias', False),
        'displayName': value.get('displayName'), 'strategy': value.get('strategy', 'failover'),
        'targets': [{**t, 'weight': t.get('weight', 1)} for t in value.get('targets', [])],
        'stickyLimit': value.get('stickyLimit', 1), 'defaultEffort': value.get('defaultEffort'),
    }


def current_combo(api, combo_id):
    item = next((x for x in api.request('/api/combos').get('combos', []) if x['id'] == combo_id), None)
    return None if item is None else {k: v for k, v in item.items() if k not in ('id', 'model')}


def patch_config(text, provider, api_key, base_url):
    """Change two settings and prove all other parsed TOML values stayed identical."""
    before = tomllib.loads(text)
    require(re.fullmatch(r'[A-Za-z0-9_-]+', provider), 'Provider name must be a simple TOML identifier.')
    old_base = before.get('openai_base_url')
    require(old_base in (None, base_url), 'An unrelated openai_base_url is configured; resolve it before applying.')
    section = re.search(r'(?m)^\[model_providers\.' + re.escape(provider) + r'\]\s*(?:#.*)?$', text)
    require(section is not None, 'Expected an explicit [model_providers.NAME] table; no file was changed.')
    end = re.search(r'(?m)^\s*\[', text[section.end():])
    stop = section.end() + end.start() if end else len(text)
    block = text[section.start():stop]
    token_line = 'experimental_bearer_token = ' + json.dumps(api_key)
    if 'experimental_bearer_token' in before['model_providers'][provider]:
        block, count = re.subn(r'(?m)^[ \t]*experimental_bearer_token[ \t]*=.*$', token_line, block)
        require(count == 1, 'Bearer token uses unsupported TOML formatting; no file was changed.')
    else:
        block = block.rstrip() + '\n' + token_line + '\n\n'
    result = text[:section.start()] + block + text[stop:]
    if old_base is None:
        result = 'openai_base_url = ' + json.dumps(base_url) + '\n' + result
    expected = copy.deepcopy(before)
    expected['openai_base_url'] = base_url
    expected['model_providers'][provider]['experimental_bearer_token'] = api_key
    require(tomllib.loads(result) == expected, 'TOML preservation check failed; no file was changed.')
    return result


def prepare(args):
    codex = args.codex_home
    config_text = (codex / 'config.toml').read_text()
    config = tomllib.loads(config_text)
    auth = read_json(codex / 'auth.json')
    ocx = read_json(args.opencodex_home / 'config.json')
    model = config.get('model')
    name = config.get('model_provider')
    require(isinstance(model, str) and bool(model), 'Configure an explicit model first.')
    require(name not in (None, 'openai'), 'This workflow expects an existing custom API provider.')
    provider = config.get('model_providers', {}).get(name, {})
    require(provider.get('wire_api') == 'responses' and provider.get('requires_openai_auth') is True,
            'Expected a Responses provider with requires_openai_auth=true; see compatibility notes.')
    endpoint = urllib.parse.urlsplit(provider.get('base_url', ''))
    require(endpoint.scheme == 'https' and endpoint.hostname and not endpoint.username and not endpoint.password and not endpoint.query,
            'The existing gateway must use an HTTPS URL without embedded credentials or a query.')
    key = provider.get('experimental_bearer_token') or auth.get('OPENAI_API_KEY')
    require(isinstance(key, str) and bool(key), 'No existing API credential found; this workflow does not request or print keys.')
    require(not provider.get('env_key'), 'Provider env_key needs manual reconciliation before using this workflow.')
    matches = [n for n, p in ocx.get('providers', {}).items()
               if p.get('baseUrl', '').rstrip('/') == provider['base_url'].rstrip('/')
               and p.get('apiKey') == key and p.get('authMode', 'key') == 'key'
               and not p.get('disabled') and p.get('adapter') == 'openai-responses']
    route = args.route_provider
    if route:
        require(route in matches, 'Selected OpenCodex provider must match the existing gateway and API key in key mode.')
    else:
        require(len(matches) == 1, 'Choose --route-provider from your existing key-authenticated OpenCodex providers.')
        route = matches[0]
    port = args.port or ocx.get('port', 10100)
    require(ocx.get('hostname', '127.0.0.1') in ('127.0.0.1', 'localhost', None), 'This workflow supports loopback-only OpenCodex.')
    combo_id = args.combo_id
    require(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}', combo_id), 'Invalid combo id.')
    combo = {'alias': model, 'nativeAlias': True, 'displayName': model + ' · API gateway',
             'strategy': 'failover', 'targets': [{'provider': route, 'model': model}]}
    api = Management(port, args.opencodex_home / 'admin-api-token')
    require(api.request('/readyz').get('status') == 'ready', 'OpenCodex is not ready.')
    old_combo = current_combo(api, combo_id)
    if old_combo:
        # A prior run or a manually configured equivalent can retain its label.
        combo['displayName'] = old_combo.get('displayName') or combo['displayName']
        require(semantic_combo(old_combo) == semantic_combo(combo), 'The chosen combo id is owned by a different route.')
        combo = copy.deepcopy(old_combo)
    for other in api.request('/api/combos').get('combos', []):
        require(other['id'] == combo_id or other.get('alias') != model,
                'Another combo already owns this model alias; select its --combo-id instead.')
    oauth_path = args.oauth_auth
    if oauth_path is None:
        candidate = args.state_dir / 'login-home/auth.json'
        oauth_path = candidate if candidate.exists() else codex / 'auth.json'
    oauth = read_json(oauth_path) if oauth_path.exists() else {}
    new_config = patch_config(config_text, name, key, api.base + '/v1')
    return dict(config=config, auth=auth, oauth=oauth, ocx=ocx, api=api, combo=combo,
                old_combo=old_combo, model=model, provider=name, route=route,
                new_config=new_config, config_text=config_text)


def plan(args):
    p = prepare(args)
    emit(ready=True, oauth_ready=oauth_valid(p['oauth']), model=p['model'],
         reasoning=p['config'].get('model_reasoning_effort'), local_provider=p['provider'],
         gateway_host=urllib.parse.urlsplit(p['config']['model_providers'][p['provider']]['base_url']).hostname,
         builtin_route_provider=p['route'], combo_id=args.combo_id,
         loopback_base_url=p['api'].base + '/v1', credentials='values never displayed')


def write_manifest(directory, manifest):
    atomic_write(directory / 'manifest.json', json.dumps(manifest, indent=2) + '\n')


def apply(args):
    p = prepare(args)
    require(oauth_valid(p['oauth']), 'Complete the official login command before applying.')
    settings = p['api'].request('/api/settings')
    if (p['new_config'] == p['config_text'] and p['auth'] == p['oauth']
            and p['old_combo'] and settings.get('codexDesktopAuthless') is False):
        emit(changed=False, status='already_configured')
        return
    state = private_dir(args.state_dir)
    directory = private_dir(state / ('backup-' + dt.datetime.now().strftime('%Y%m%d-%H%M%S-%f')))
    paths = {'config': args.codex_home / 'config.toml', 'auth': args.codex_home / 'auth.json',
             'opencodex': args.opencodex_home / 'config.json'}
    manifest = {'stage': 'prepared', 'files': {}, 'combo_id': args.combo_id,
                'before_combo': p['old_combo'], 'desired_combo': p['combo'],
                'before_authless': bool(settings.get('codexDesktopAuthless')),
                'management_base': p['api'].base, 'management_token_path': str(p['api'].token_path)}
    for name, path in paths.items():
        data = path.read_bytes()
        atomic_write(directory / (name + '.before'), data)
        manifest['files'][name] = {'path': str(path), 'before_hash': digest(data)}
    write_manifest(directory, manifest)
    try:
        manifest['stage'] = 'setting_combo'
        write_manifest(directory, manifest)
        if p['old_combo'] is None:
            p['api'].request('/api/combos', 'PUT', {'id': args.combo_id, 'combo': p['combo']})
        manifest['stage'] = 'setting_auth_mode'
        write_manifest(directory, manifest)
        p['api'].request('/api/settings', 'PUT', {'codexDesktopAuthless': False})
        require((args.codex_home / 'config.toml').read_text() == p['config_text'],
                'OpenCodex or another process changed Codex config during activation; backup retained.')
        require(read_json(args.codex_home / 'auth.json') == p['auth'],
                'The live authentication changed during activation; newer credentials were preserved.')
        atomic_write(args.codex_home / 'config.toml', p['new_config'])
        manifest['files']['config']['after_hash'] = digest((args.codex_home / 'config.toml').read_bytes())
        manifest['stage'] = 'config_written'
        write_manifest(directory, manifest)
        atomic_write(args.codex_home / 'auth.json', json.dumps(p['oauth'], indent=2) + '\n')
        manifest['files']['auth']['after_hash'] = digest((args.codex_home / 'auth.json').read_bytes())
        manifest['stage'] = 'applied'
        write_manifest(directory, manifest)
        emit(changed=True, auth_mode='chatgpt', model=p['model'], backup=str(directory),
             next='Run verify, then restart the desktop app and pair the phone.')
    except Exception:
        # The manifest identifies each completed stage, even for ambiguous API timeouts.
        emit(status='activation_incomplete', backup=str(directory), next='Run rollback with this backup; no raw error data is printed.')
        raise


def rollback(args):
    directory = args.backup.expanduser().resolve()
    manifest = read_json(directory / 'manifest.json')
    parsed = urllib.parse.urlsplit(manifest['management_base'])
    require(parsed.scheme == 'http' and parsed.hostname == '127.0.0.1', 'Rollback management endpoint must remain loopback.')
    api = Management(parsed.port, manifest['management_token_path'])
    # Guard before any mutation: preserve later edits and token refreshes.
    for name in ('config', 'auth'):
        item = manifest['files'][name]
        require(digest(Path(item['path']).read_bytes()) in {item['before_hash'], item.get('after_hash')},
                'Files changed after activation (possibly an OAuth refresh); rollback stopped without overwriting them.')
    live_combo = current_combo(api, manifest['combo_id'])
    require(semantic_combo(live_combo) in (semantic_combo(manifest['before_combo']), semantic_combo(manifest['desired_combo']), None),
            'The routing combo was changed later; automatic rollback stopped.')
    if manifest['before_combo'] is None and live_combo:
        api.request('/api/combos?id=' + urllib.parse.quote(manifest['combo_id'], safe=''), 'DELETE')
    elif manifest['before_combo']:
        api.request('/api/combos', 'PUT', {'id': manifest['combo_id'], 'combo': manifest['before_combo']})
    api.request('/api/settings', 'PUT', {'codexDesktopAuthless': manifest['before_authless']})
    for name in ('config', 'auth'):
        atomic_write(manifest['files'][name]['path'], (directory / (name + '.before')).read_bytes())
    manifest['stage'] = 'rolled_back'
    write_manifest(directory, manifest)
    emit(rolled_back=True, unrelated_opencodex_settings='preserved', restart_required=True)


def cli_path(args):
    if args.cli:
        require(args.cli.is_file(), 'Specified Codex CLI was not found.')
        return str(args.cli)
    for app in (Path('/Applications/ChatGPT.app'), Path('/Applications/Codex.app')):
        for suffix in ('Contents/Resources/codex-cli/bin/codex', 'Contents/Resources/codex'):
            candidate = app / suffix
            if candidate.is_file():
                return str(candidate)
    found = shutil.which('codex')
    require(found is not None, 'Codex CLI not found; pass --cli PATH.')
    return found


def login(args):
    home = private_dir(private_dir(args.state_dir) / 'login-home')
    auth_path = home / 'auth.json'
    if auth_path.exists() and oauth_valid(read_json(auth_path)):
        emit(oauth_saved=True, status='existing_isolated_login', path=str(auth_path))
        return
    command = [cli_path(args), 'app-server', '-c', 'cli_auth_credentials_store="file"']
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, env=clean_env(home), cwd=home)
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    buffer = b''
    completed = False

    def send(method, params, request_id=None):
        message = {'method': method, 'params': params}
        if request_id is not None:
            message['id'] = request_id
        process.stdin.write((json.dumps(message) + '\n').encode())
        process.stdin.flush()

    send('initialize', {'clientInfo': {'name': 'hybrid_remote_workflow', 'version': '1.0.0'}}, 1)
    try:
        deadline = time.monotonic() + args.timeout
        while time.monotonic() < deadline and process.poll() is None:
            if not selector.select(timeout=1):
                continue
            chunk = os.read(process.stdout.fileno(), 65536)
            if not chunk:
                break
            buffer += chunk
            while b'\n' in buffer:
                line, buffer = buffer.split(b'\n', 1)
                try:
                    message = json.loads(line)
                except ValueError:
                    continue
                require('error' not in message, 'Official login RPC failed; raw response omitted.')
                if message.get('id') == 1:
                    send('initialized', {})
                    send('account/login/start', {'type': 'chatgpt'}, 2)
                elif message.get('id') == 2:
                    url = message.get('result', {}).get('authUrl', '')
                    parts = urllib.parse.urlsplit(url)
                    require(parts.scheme == 'https' and parts.hostname == 'auth.openai.com', 'Unexpected official authorization destination.')
                    subprocess.run(['open', '-n', '-a', 'Google Chrome', '--args',
                                    '--profile-directory=' + args.chrome_profile, url],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
                    emit(status='waiting_for_official_browser_login', chrome_profile=args.chrome_profile,
                         note='Finish login in Chrome. Passwords and verification codes stay out of this terminal.')
                elif message.get('method') == 'account/login/completed':
                    require(message.get('params', {}).get('success') is True, 'Official login was not completed.')
                    completed = auth_path.exists() and oauth_valid(read_json(auth_path))
                    require(completed, 'Login completed but no reusable OAuth credentials were saved.')
                    os.chmod(auth_path, 0o600)
                    emit(oauth_saved=True, live_credentials_changed=False, path=str(auth_path))
                    return
        require(completed, 'Login timed out or was interrupted; live credentials remain unchanged.')
    finally:
        selector.close()
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def verify(args):
    p = prepare(args)
    require(oauth_valid(p['auth']), 'The live auth file does not contain ChatGPT OAuth.')
    require(p['old_combo'] is not None, 'The native model route has not been installed.')
    require(p['new_config'] == p['config_text'], 'The expected hybrid configuration has not been applied.')
    require(p['api'].request('/api/settings').get('codexDesktopAuthless') is False, 'OpenCodex authless mode is still enabled.')
    root = private_dir(args.state_dir)
    results = []
    # Only copy the minimal tested config; do not run the user's MCP servers, hooks,
    # notifications, project prompts or plugins in smoke tests.
    with tempfile.TemporaryDirectory(prefix='smoke-', dir=root) as temporary:
        home = Path(temporary)
        atomic_write(home / 'auth.json', json.dumps(p['auth']))
        provider = p['config']['model_providers'][p['provider']]
        config_text = '\n'.join([
            'model = ' + json.dumps(p['model']), 'model_provider = ' + json.dumps(p['provider']),
            'model_reasoning_effort = "low"', 'cli_auth_credentials_store = "file"',
            'openai_base_url = ' + json.dumps(p['api'].base + '/v1'),
            '[model_providers.' + p['provider'] + ']',
            'name = "Existing API gateway"', 'wire_api = "responses"', 'requires_openai_auth = true',
            'base_url = ' + json.dumps(provider['base_url']),
            'experimental_bearer_token = ' + json.dumps(provider['experimental_bearer_token']), '',
        ])
        atomic_write(home / 'config.toml', config_text)
        for selected in (p['provider'], 'openai'):
            marker = 'hybrid-remote-route-ok'
            command = [cli_path(args), 'exec', '--ephemeral', '--skip-git-repo-check', '--json',
                       '--sandbox', 'read-only', '-c', 'model_provider=' + json.dumps(selected),
                       'Reply with exactly ' + marker + '. Do not use tools or inspect files.']
            try:
                result = subprocess.run(command, cwd=home, env=clean_env(home), capture_output=True,
                                        text=True, timeout=120)
            except subprocess.TimeoutExpired:
                raise WorkflowError('Model smoke test timed out; raw output omitted.') from None
            replies = []
            for line in result.stdout.splitlines():
                try:
                    message = json.loads(line)
                except ValueError:
                    continue
                item = message.get('item', {})
                if message.get('type') == 'item.completed' and item.get('type') == 'agent_message':
                    replies.append(item.get('text', '').strip())
            ok = result.returncode == 0 and marker in replies
            results.append({'path': 'direct' if selected != 'openai' else 'builtin-via-opencodex', 'ok': ok})
            require(ok, 'A model smoke test failed; raw output omitted. Review gateway availability and model compatibility.')
    emit(verified=results, mobile_pairing_verified=False, note='These are CLI tests. Complete a mobile-originated task separately.')


def doctor(args):
    auth = read_json(args.codex_home / 'auth.json')
    cfg = tomllib.loads((args.codex_home / 'config.toml').read_text())
    ocx = read_json(args.opencodex_home / 'config.json')
    api = Management(args.port or ocx.get('port', 10100), args.opencodex_home / 'admin-api-token')
    statuses = {name: api.request('/' + name).get('status') for name in ('healthz', 'readyz')}
    emit(auth_mode=auth.get('auth_mode'), oauth_present=oauth_valid(auth),
         model=cfg.get('model'), reasoning=cfg.get('model_reasoning_effort'),
         provider=cfg.get('model_provider'), opencodex=statuses,
         note='This read-only check does not prove desktop auth cache, browser automation or phone pairing.')


def restart(args):
    require(sys.platform == 'darwin', 'Application restart is supported only on macOS.')
    require(args.app in ('ChatGPT', 'Codex'), 'Unsupported application name.')
    state = private_dir(args.state_dir)
    # This detached child survives the desktop app closing the caller's terminal/tool.
    command = [sys.executable, str(Path(__file__).resolve()), '--state-dir', str(state),
               '_restart-worker', '--app', args.app]
    subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)
    emit(restart_scheduled=True, delay_seconds=5, status_file=str(state / 'restart-status.json'))


def restart_worker(args):
    state = private_dir(args.state_dir)
    path = state / 'restart-status.json'
    app_path = Path('/Applications') / (args.app + '.app')
    executable = str(app_path / 'Contents/MacOS' / args.app)

    def record(stage):
        atomic_write(path, json.dumps({'stage': stage, 'time': dt.datetime.now().astimezone().isoformat()}))

    def pids():
        result = subprocess.run(['ps', '-axo', 'pid=,command='], capture_output=True, text=True, check=True)
        return {int(parts[0]) for line in result.stdout.splitlines()
                if len(parts := line.strip().split(None, 1)) == 2 and parts[1] == executable}

    record('scheduled')
    old = pids()
    time.sleep(5)
    try:
        result = subprocess.run(['osascript', '-'], input=f'tell application "{args.app}" to quit\n',
                                capture_output=True, text=True, timeout=25)
        require(result.returncode == 0, 'Graceful quit failed.')
        deadline = time.monotonic() + 35
        while pids() and time.monotonic() < deadline:
            time.sleep(1)
        require(not pids(), 'Application did not quit; no force kill was attempted.')
        subprocess.run(['open', '-a', str(app_path)], capture_output=True, check=True, timeout=20)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if pids() and not pids().intersection(old):
                record('reopened')
                return
            time.sleep(1)
        record('open_requested_unconfirmed')
    except Exception:
        record('failed_no_force_kill')


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--codex-home', type=Path, default=Path.home() / '.codex')
    p.add_argument('--opencodex-home', type=Path, default=Path.home() / '.opencodex')
    p.add_argument('--state-dir', type=Path, default=Path.home() / '.local/state/codex-hybrid-remote')
    p.add_argument('--cli', type=Path, help='Override the bundled Codex CLI path')
    p.add_argument('--port', type=int, help='OpenCodex loopback port, normally read from config')
    p.add_argument('--route-provider', help='Existing OpenCodex provider with a matching gateway key')
    p.add_argument('--combo-id', default='hybrid-api-route')
    p.add_argument('--oauth-auth', type=Path, help='Private auth.json from a completed official login')
    sub = p.add_subparsers(dest='command', required=True)
    for name in ('doctor', 'plan', 'apply', 'verify'):
        sub.add_parser(name)
    q = sub.add_parser('login')
    q.add_argument('--chrome-profile', default='Default', help='Chrome profile directory, not display name')
    q.add_argument('--timeout', type=int, default=900)
    q = sub.add_parser('rollback')
    q.add_argument('--backup', type=Path, required=True)
    for name in ('restart', '_restart-worker'):
        q = sub.add_parser(name)
        q.add_argument('--app', choices=('ChatGPT', 'Codex'), default='ChatGPT')
    return p


def main():
    args = parser().parse_args()
    for field in ('codex_home', 'opencodex_home', 'state_dir'):
        setattr(args, field, getattr(args, field).expanduser().resolve())
    for field in ('cli', 'oauth_auth'):
        if getattr(args, field):
            setattr(args, field, getattr(args, field).expanduser().resolve())
    repository = Path(__file__).resolve().parents[1]
    require(not args.state_dir.is_relative_to(repository), 'Keep private runtime state outside the repository.')
    actions = {'doctor': doctor, 'plan': plan, 'login': login, 'apply': apply,
               'verify': verify, 'rollback': rollback, 'restart': restart, '_restart-worker': restart_worker}
    actions[args.command](args)


if __name__ == '__main__':
    try:
        main()
    except WorkflowError as error:
        emit(ok=False, error=str(error))
        sys.exit(1)
    except KeyboardInterrupt:
        emit(ok=False, error='Interrupted; inspect the private state directory before retrying.')
        sys.exit(130)
    except Exception as error:
        # OS, JSON and TOML exceptions may include paths, response bodies or tokens.
        emit(ok=False, error_type=type(error).__name__, error='Operation failed; raw exception omitted to protect private data.')
        sys.exit(1)
