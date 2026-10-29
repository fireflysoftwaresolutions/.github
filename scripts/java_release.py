#!/usr/bin/env python3
"""Cold validation and immutable artifact publication. No dependency source installs."""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile

VERSION = re.compile(r'[0-9]{2}\.(?:0[1-9]|1[0-2])\.[0-9]+\Z')
COORDINATE = re.compile(r'[A-Za-z0-9_][A-Za-z0-9_.-]*\Z')
SUPPRESSION = re.compile(r'skipTests|maven\.test\.skip|skipOpenApiGen|skipITs', re.I)
GLOBAL_TEST_PROPERTIES = {'skiptests', 'maven.test.skip', 'skipits'}
NS = '{http://maven.apache.org/POM/4.0.0}'


def command(args, stream_console=False, **kwargs):
    if not stream_console:
        return subprocess.run(args, check=True, **kwargs)
    output = kwargs.pop('stdout')
    if kwargs.pop('stderr') != subprocess.STDOUT:
        raise ValueError('Merged validation diagnostics required')
    with subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **kwargs) as process:
        for line in process.stdout:
            output.write(line)
            output.flush()
            # GitHub applies its registered-secret masking to the step console stream.
            sys.stdout.write(line.decode('utf-8', errors='replace'))
            sys.stdout.flush()
        status = process.wait()
    if status:
        raise subprocess.CalledProcessError(status, args)
    return subprocess.CompletedProcess(args, status)


def validation_failure(evidence, phase, error, log, effective, proof):
    if phase not in ('COLD_BUILD', 'EFFECTIVE_POM', 'TEST_EVIDENCE', 'ORIGINAL_SDK', 'FREEZE'):
        raise ValueError('Closed validation phase required')
    def retained_hash(path):
        return digest(path) if path.is_file() and not path.is_symlink() else None
    result = {'schema': 1, 'phase': phase,
              'exit_code': error.returncode if isinstance(error, subprocess.CalledProcessError) else None,
              'error_kind': type(error).__name__,
              'error_sha256': hashlib.sha256(str(error).encode()).hexdigest(),
              'normal_build_log_sha256': retained_hash(log),
              'effective_pom_sha256': retained_hash(effective),
              'original_sdk_proof_sha256': retained_hash(proof)}
    with os.fdopen(os.open(evidence / 'validation-failure.json', os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600), 'w') as output:
        output.write(json.dumps(result, sort_keys=True) + '\n')


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def owned_file(root, relative):
    p = Path(relative)
    if p.is_absolute() or '..' in p.parts or not p.parts:
        raise ValueError('Path must remain inside its source root')
    root = root.resolve()
    candidate = root / p
    components = [candidate, *list(candidate.parents)[:len(p.parts) - 1]]
    if any(part.is_symlink() for part in components):
        raise ValueError('Symlink file paths are not accepted')
    if not candidate.is_file() or not candidate.resolve().is_relative_to(root.resolve()):
        raise ValueError('Required regular file missing')
    return candidate


def version(text):
    if not isinstance(text, str) or not VERSION.fullmatch(text):
        raise ValueError('Version must be YY.MM.Patch with a nonnegative patch counter')
    return text


def check_tag(tag, effective_version):
    if tag != 'v' + version(effective_version):
        raise ValueError('Tag differs from the effective project version')


def no_suppression(text):
    if SUPPRESSION.search(text):
        raise ValueError('Test or original producer/generator suppression is forbidden')


def effective_projects(path):
    tree = ET.parse(path).getroot()
    projects = [tree] if tree.tag in ('project', NS + 'project') else list(tree)
    for node in tree.iter():
        if node.tag.split('}')[-1] == 'version':
            text = (node.text or '').strip()
            if not text or 'SNAPSHOT' in text.upper() or text.upper() in ('LATEST', 'RELEASE') or any(c in text for c in '${}[],()'):
                raise ValueError('Only fixed released dependency and plugin versions are accepted')
    result = []
    for project in projects:
        ns = NS if project.tag.startswith(NS) else ''
        row = {key: (project.findtext(ns + key) or '').strip() for key in ('groupId', 'artifactId', 'version')}
        if not all(COORDINATE.fullmatch(row[key]) for key in ('groupId', 'artifactId')):
            raise ValueError('Unresolved effective project coordinates')
        version(row['version'])
        row['packaging'] = (project.findtext(ns + 'packaging') or 'jar').strip()
        props = project.find(ns + 'properties')
        if props is not None:
            for prop in props:
                name = prop.tag.split('}')[-1]
                if name.casefold() == 'skipopenapigen' or (name.casefold() in GLOBAL_TEST_PROPERTIES and (prop.text or '').strip().lower() != 'false'):
                    raise ValueError('Effective POM suppresses required validation')
        result.append(row)
    if not result or len({(p['groupId'], p['artifactId']) for p in result}) != len(result):
        raise ValueError('Missing or duplicate effective reactor projects')
    return result


def test_evidence(source, policy):
    reports = sorted(set(source.glob('**/target/surefire-reports/TEST-*.xml')) |
                     set(source.glob('**/target/failsafe-reports/TEST-*.xml')))
    suites, total, hashes = [], 0, []
    for path in reports:
        root = ET.parse(path).getroot()
        if root.tag != 'testsuite' or any(int(root.get(k, '0')) for k in ('failures', 'errors', 'skipped')):
            raise ValueError('Test report failure, error, skip, or unsupported report shape')
        cases = root.findall('testcase')
        if int(root.get('tests', '-1')) != len(cases) or any(c.find('failure') is not None or c.find('error') is not None or c.find('skipped') is not None for c in cases):
            raise ValueError('Testcase evidence differs from suite totals')
        suites.append(root.get('name'))
        total += len(cases)
        hashes.append({'path': str(path.relative_to(source)), 'sha256': digest(path)})
    if sorted(suites) != sorted(policy['suites']) or len(set(suites)) != len(suites):
        raise ValueError('Actual test suite inventory differs from reviewed repository policy')
    if total < policy['minimum_tests'] or (policy['kind'] == 'service' and not total):
        raise ValueError('Required ordinary tests did not execute')
    return {'suites': len(suites), 'tests': total, 'failures': 0, 'errors': 0, 'skipped': 0, 'reports': hashes}


def check_files(root, rows):
    for row in rows:
        if digest(owned_file(root, row['path'])) != row['sha256']:
            raise ValueError('Frozen input or artifact changed')


def sdk_evidence(source, path, sha):
    proof = json.loads(path.read_text())
    if set(proof) != {'source_sha', 'original_producer', 'original_generator', 'producer_executions', 'generator_executions', 'files'}:
        raise ValueError('SDK proof must use the bounded public provenance schema')
    if proof.get('source_sha') != sha or proof.get('original_producer') is not True or proof.get('original_generator') is not True:
        raise ValueError('Original producer and Generator evidence required')
    if not proof.get('files') or not proof.get('producer_executions') or not proof.get('generator_executions'):
        raise ValueError('Generated SDK proof is incomplete')
    for execution in proof['producer_executions'] + proof['generator_executions']:
        if not isinstance(execution, str) or not re.fullmatch('[A-Za-z0-9_.:()/-]{1,240}', execution):
            raise ValueError('SDK execution provenance must contain identifiers only')
    for row in proof['files']:
        if set(row) != {'path', 'sha256'} or not re.fullmatch('[0-9a-f]{64}', row['sha256']):
            raise ValueError('SDK file proof must contain relative paths and SHA256 only')
    check_files(source, proof['files'])
    return proof


def require_status(status, allowed):
    if status not in allowed:
        raise ValueError('Remote operation refused; HTTP status ' + str(status))


def require_absent(status):
    require_status(status, (404,))


class CredentialRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is not None and urllib.parse.urlsplit(req.full_url).netloc != urllib.parse.urlsplit(newurl).netloc:
            redirected.remove_header('Authorization')
        return redirected


def request(url, token=None, data=None, method='GET', content_type=None, absent=False):
    headers = {'User-Agent': 'firefly-immutable-java-release', 'Accept': 'application/json'}
    if token:
        headers['Authorization'] = token
    if content_type:
        headers['Content-Type'] = content_type
    try:
        with urllib.request.build_opener(CredentialRedirect()).open(urllib.request.Request(url, data=data, headers=headers, method=method), timeout=60) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        if absent and error.code == 404:
            return 404, b''
        raise ValueError('Remote operation refused; HTTP status ' + str(error.code)) from None


def source_snapshot(source):
    tracked = command(['git', 'ls-files', '-z'], cwd=source, capture_output=True).stdout.decode().split('\0')
    return [{'path': p, 'sha256': digest(owned_file(source, p))} for p in tracked if p]


def assert_tag(source, tag, sha, repo, token):
    actual = command(['git', 'rev-parse', 'refs/tags/' + tag + '^{commit}'], cwd=source, capture_output=True, text=True).stdout.strip()
    if actual != sha:
        raise ValueError('Local immutable tag does not point at the exact source SHA')
    _, raw = request('https://api.github.com/repos/' + repo + '/git/ref/tags/' + urllib.parse.quote(tag, safe=''), token)
    obj = json.loads(raw)['object']
    for _ in range(5):
        if obj['type'] != 'tag':
            break
        _, raw = request(obj['url'], token)
        obj = json.loads(raw)['object']
    if obj['type'] != 'commit' or obj['sha'] != sha:
        raise ValueError('Remote tag/source identity differs')


def validate_policy(policy):
    if policy.get('schema') != 1 or policy.get('kind') not in ('pom', 'service'):
        raise ValueError('Explicit reviewed repository release policy required')
    if not isinstance(policy.get('suites'), list) or type(policy.get('minimum_tests')) is not int or policy['minimum_tests'] < 0:
        raise ValueError('Reviewed suite inventory and minimum test count required')
    if policy['kind'] == 'service' and (not policy['suites'] or policy['minimum_tests'] < 1):
        raise ValueError('Service releases require ordinary test evidence')
    if not policy.get('artifacts') or not isinstance(policy.get('profiles'), list):
        raise ValueError('Complete artifact set and explicit profile list required')
    for profile in policy['profiles']:
        if not re.fullmatch('[A-Za-z0-9_-]+', profile):
            raise ValueError('Invalid profile')
        no_suppression(profile)
    if policy['kind'] == 'service' and not policy.get('verifier'):
        raise ValueError('Service-specific original producer/SDK verifier required')


def freeze(source, directory, policy, projects):
    expected = {(p['groupId'], p['artifactId'], p['version'], p['packaging']) for p in projects}
    if [tuple(a[k] for k in ('groupId', 'artifactId', 'version', 'packaging')) for a in policy['artifacts']] != [tuple(p[k] for k in ('groupId', 'artifactId', 'version', 'packaging')) for p in projects]:
        raise ValueError('Artifact publication order must match the effective reactor')
    actual, rows = set(), []
    for artifact in policy['artifacts']:
        gav = tuple(artifact[k] for k in ('groupId', 'artifactId', 'version', 'packaging'))
        if gav not in expected or gav in actual:
            raise ValueError('Artifact set differs from effective reactor')
        actual.add(gav)
        group, name, ver, packaging = gav
        if packaging not in ('pom', 'jar'):
            raise ValueError('Packaging requires a reviewed publisher adapter')
        prefix = group.replace('.', '/') + '/' + name + '/' + ver + '/' + name + '-' + ver
        files = artifact['files']
        required = {'.pom'} if packaging == 'pom' else {'.pom', '.jar', '-sources.jar', '-javadoc.jar'}
        if set(files) != required:
            raise ValueError('Exact POM, primary, source and Javadoc artifact inventory required')
        for suffix, relative in files.items():
            original = owned_file(source, relative)
            if suffix != '.pom' and 'target' not in Path(relative).parts:
                raise ValueError('Packaged artifacts must come from the fresh target tree')
            if suffix == '.jar':
                embedded = 'META-INF/maven/' + group + '/' + name + '/pom.properties'
                with zipfile.ZipFile(original) as jar:
                    props = dict(line.split('=', 1) for line in jar.read(embedded).decode().splitlines() if '=' in line and not line.startswith('#'))
                    if any(props.get(k) != v for k, v in zip(('groupId', 'artifactId', 'version'), gav[:3])):
                        raise ValueError('Packaged JAR effective coordinates differ')
            target = directory / (prefix + suffix)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(original, target)
            # POM bytes must remain the effective project's actual publish POM, never generated here.
            if suffix == '.pom':
                pom = ET.parse(original).getroot()
                ns = NS if pom.tag.startswith(NS) else ''
                local = [(pom.findtext(ns + k) or '').strip() for k in ('groupId', 'artifactId', 'version')]
                if local[0] and local[0] != group or local[1] != name or local[2] and local[2] != ver:
                    raise ValueError('Artifact POM differs from effective GAV')
            rows.append({'path': str(target.relative_to(directory)), 'sha256': digest(target), 'source_path': relative})
    if actual != expected or (policy['kind'] == 'pom' and any(p['packaging'] != 'pom' for p in projects)):
        raise ValueError('Policy must cover the complete reactor; POM-only exemption is explicit')
    return sorted(rows, key=lambda row: row['path'])


def publish_github(frozen, policy, settings, cache, repo, env):
    neutral = frozen.parent / 'publisher.xml'
    neutral.write_text('<project xmlns="http://maven.apache.org/POM/4.0.0"><modelVersion>4.0.0</modelVersion><groupId>local.publisher</groupId><artifactId>frozen-artifacts</artifactId><version>1</version></project>')
    for a in policy['artifacts']:
        prefix = a['groupId'].replace('.', '/') + '/' + a['artifactId'] + '/' + a['version'] + '/' + a['artifactId'] + '-' + a['version']
        args = ['mvn', '-B', '-ntp', '-s', str(settings), '-gs', str(settings), '-f', str(neutral), '-Dmaven.repo.local=' + str(cache), 'org.apache.maven.plugins:maven-deploy-plugin:3.1.4:deploy-file', '-DretryFailedDeploymentCount=1', '-DgeneratePom=false', '-DrepositoryId=github-firefly-oss', '-Durl=https://maven.pkg.github.com/' + repo, '-DgroupId=' + a['groupId'], '-DartifactId=' + a['artifactId'], '-Dversion=' + a['version'], '-Dpackaging=' + a['packaging'], '-DpomFile=' + str(frozen / (prefix + '.pom')), '-Dfile=' + str(frozen / (prefix + ('.pom' if a['packaging'] == 'pom' else '.jar')))]
        if a['packaging'] == 'jar':
            args += ['-Dsources=' + str(frozen / (prefix + '-sources.jar')), '-Djavadoc=' + str(frozen / (prefix + '-javadoc.jar'))]
        command(args, cwd=frozen.parent, env=env)


def central_bundle(frozen, rows, directory, policy, env):
    fingerprint = policy.get('signing_fingerprint', '')
    if not re.fullmatch('[0-9A-Fa-f]{40}', fingerprint):
        raise ValueError('Reviewed full signing fingerprint required')
    gpg_home = directory / 'gnupg'
    gpg_home.mkdir(mode=0o700)
    env = dict(env, GNUPGHOME=str(gpg_home))
    command(['gpg', '--batch', '--import'], input=base64.b64decode(env['GPG_PRIVATE_KEY'], validate=True), env=env, capture_output=True)
    extra = []
    for row in rows:
        path = frozen / row['path']
        signature = Path(str(path) + '.asc')
        command(['gpg', '--batch', '--pinentry-mode', 'loopback', '--passphrase-fd', '0', '--local-user', fingerprint, '--armor', '--detach-sign', '--output', str(signature), str(path)], input=env['MAVEN_GPG_PASSPHRASE'].encode(), env=env, capture_output=True)
        command(['gpg', '--batch', '--verify', str(signature), str(path)], env=env, capture_output=True)
        extra.append({'path': str(signature.relative_to(frozen)), 'sha256': digest(signature)})
        for algorithm in ('md5', 'sha1', 'sha256', 'sha512'):
            target = Path(str(path) + '.' + algorithm)
            target.write_text(hashlib.new(algorithm, path.read_bytes()).hexdigest())
            extra.append({'path': str(target.relative_to(frozen)), 'sha256': digest(target)})
    bundle = directory / 'central-bundle.zip'
    with zipfile.ZipFile(bundle, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for row in rows + extra:
            archive.write(frozen / row['path'], row['path'])
    if bundle.stat().st_size > 250_000_000:
        raise ValueError('Bundle exceeds bounded publisher size')
    return bundle, extra


def publish_central(bundle, env):
    token = 'Bearer ' + base64.b64encode((env['SONATYPE_USERNAME'] + ':' + env['SONATYPE_PASSWORD']).encode()).decode()
    boundary = 'firefly-' + digest(bundle)
    body = ('--' + boundary + '\r\nContent-Disposition: form-data; name="bundle"; filename="central-bundle.zip"\r\nContent-Type: application/octet-stream\r\n\r\n').encode() + bundle.read_bytes() + ('\r\n--' + boundary + '--\r\n').encode()
    status, raw = request('https://central.sonatype.com/api/v1/publisher/upload?publishingType=AUTOMATIC', token, body, 'POST', 'multipart/form-data; boundary=' + boundary)
    require_status(status, (201,))
    deployment = raw.decode().strip()
    if not re.fullmatch('[0-9a-f-]{36}', deployment):
        raise ValueError('Invalid Central deployment receipt')
    for _ in range(180):
        _, raw = request('https://central.sonatype.com/api/v1/publisher/status?id=' + deployment, token, b'', 'POST')
        proof = json.loads(raw)
        if proof.get('deploymentId') != deployment:
            raise ValueError('Central deployment identity differs')
        state = proof.get('deploymentState')
        if state == 'PUBLISHED':
            return {'deploymentId': deployment, 'state': state}
        if state not in ('PENDING', 'VALIDATING', 'PUBLISHING'):
            raise ValueError('Central publication did not complete')
        time.sleep(10)
    raise ValueError('Central publication deadline exceeded; retain partial state')


def verify_remote(base, rows, token=None):
    for row in rows:
        for attempt in range(30):
            status, raw = request(base + '/' + row['path'], token, absent=True)
            if status == 200:
                if hashlib.sha256(raw).hexdigest() != row['sha256']:
                    raise ValueError('Published artifact bytes differ from frozen manifest')
                break
            if attempt == 29:
                raise ValueError('Published artifact absent after bounded readback')
            time.sleep(10)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', type=Path, required=True)
    args = parser.parse_args()
    source = args.source.resolve()
    env = dict(os.environ)
    for key in ('MAVEN_OPTS', 'MAVEN_ARGS', 'JAVA_TOOL_OPTIONS', 'JDK_JAVA_OPTIONS', '_JAVA_OPTIONS', 'MAVEN_EXT_CLASS_PATH', 'MAVEN_USER_HOME', 'CLASSPATH'):
        no_suppression(env.get(key, ''))
        env.pop(key, None)
    repo, sha, tag = env['RELEASE_REPOSITORY'], env['RELEASE_SOURCE_SHA'], env['RELEASE_TAG']
    if not re.fullmatch('[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repo) or not re.fullmatch('[0-9a-f]{40}', sha) or not re.fullmatch('[0-9a-f]{40}', env['RELEASE_WORKFLOW_SHA']):
        raise ValueError('Invalid repository or immutable source identity')
    if not tag.startswith('v'):
        raise ValueError('Versioned immutable release tag required')
    version(tag[1:])
    destination = env['RELEASE_DESTINATION']
    if destination not in ('github', 'central', 'both') or env['RELEASE_DRY_RUN'] not in ('true', 'false'):
        raise ValueError('Invalid destination or dry-run input')
    token = 'Bearer ' + env['GH_TOKEN']
    assert_tag(source, tag, sha, repo, token)
    status, _ = request('https://api.github.com/repos/' + repo + '/releases/tags/' + tag, token, absent=True)
    require_absent(status)
    if command(['git', 'rev-parse', 'HEAD'], cwd=source, capture_output=True, text=True).stdout.strip() != sha or command(['git', 'status', '--porcelain'], cwd=source, capture_output=True).stdout:
        raise ValueError('Caller checkout is not the exact clean source')
    policy_path = owned_file(source, env['RELEASE_POLICY'])
    policy = json.loads(policy_path.read_text())
    validate_policy(policy)
    tracked = source_snapshot(source)
    tracked_paths = {row['path'] for row in tracked}
    if env['RELEASE_POLICY'] not in tracked_paths:
        raise ValueError('Release policy must be tracked at the exact source tag')
    for relative in ('.mvn/maven.config', '.mvn/jvm.config', '.mvn/extensions.xml'):
        path = source / relative
        if path.exists() and path.read_text().strip():
            raise ValueError('Nonempty Maven launcher/extension config needs separate release review')
    if any(source.glob('**/target')):
        raise ValueError('Release source contains preexisting build outputs')
    evidence = Path(env['RUNNER_TEMP']) / 'java-release-evidence'
    evidence.mkdir(mode=0o700)
    (evidence / 'validation-state.json').write_text(json.dumps({'source_sha': sha, 'workflow_sha': env['RELEASE_WORKFLOW_SHA'], 'tag': tag, 'phase': 'COLD_VALIDATION_PENDING', 'publication_observed': False}) + '\n')
    work = Path(tempfile.mkdtemp(prefix='java-release-', dir=env['RUNNER_TEMP']))
    cache = work / 'cold-maven'
    cache.mkdir(mode=0o700)
    settings = work / 'settings.xml'
    settings.write_text('<settings xmlns="http://maven.apache.org/SETTINGS/1.0.0"><servers><server><id>github-firefly-oss</id><username>x-access-token</username><password>${env.GITHUB_TOKEN}</password></server></servers></settings>')
    settings.chmod(0o600)
    effective = work / 'effective-pom.xml'
    log = work / 'normal-build.log'
    maven = ['mvn', '-B', '-ntp', '-s', str(settings), '-gs', str(settings), '-Dmaven.repo.local=' + str(cache)]
    if policy['profiles']:
        maven += ['-P' + ','.join(policy['profiles'])]
    proof = work / 'original-sdk-proof.json'
    phase = 'COLD_BUILD'
    try:
        with os.fdopen(os.open(log, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb') as output:
            command(maven + ['clean', 'verify', 'org.apache.maven.plugins:maven-help-plugin:3.5.1:effective-pom', '-Doutput=' + str(effective)], cwd=source, env=env, stdout=output, stderr=subprocess.STDOUT, stream_console=True)
        phase = 'EFFECTIVE_POM'
        projects = effective_projects(effective)
        for project in projects:
            check_tag(tag, project['version'])
        phase = 'TEST_EVIDENCE'
        check_files(source, tracked)
        tests = test_evidence(source, policy)
        (evidence / 'validation-state.json').write_text(json.dumps({'source_sha': sha, 'workflow_sha': env['RELEASE_WORKFLOW_SHA'], 'tag': tag, 'phase': 'COLD_BUILD_AND_TESTS_VALIDATED', 'tests': tests['tests'], 'suites': tests['suites'], 'publication_observed': False}) + '\n')
        phase = 'ORIGINAL_SDK'
        sdk = None
        if policy['kind'] == 'service':
            verifier = owned_file(source, policy['verifier'])
            if policy['verifier'] not in tracked_paths:
                raise ValueError('Original producer verifier must be tracked and reviewed')
            command(['python3', str(verifier), '--source-sha', sha, '--build-log', str(log), '--output', str(proof)], cwd=source, env=env)
            sdk = sdk_evidence(source, proof, sha)
        check_files(source, tracked)
        phase = 'FREEZE'
        frozen = work / 'frozen'
        frozen.mkdir(mode=0o700)
        rows = freeze(source, frozen, policy, projects)
    except (ValueError, KeyError, OSError, subprocess.CalledProcessError, ET.ParseError) as error:
        try:
            validation_failure(evidence, phase, error, log, effective, proof)
        except OSError:
            print('Failure metadata could not be retained; original validation failure preserved', file=sys.stderr)
        raise
    manifest = {'schema': 1, 'repository': repo, 'source_sha': sha, 'tag': tag, 'workflow_sha': env['RELEASE_WORKFLOW_SHA'], 'version': projects[0]['version'], 'projects': projects, 'policy_sha256': digest(policy_path), 'source_files': tracked, 'tests': tests, 'effective_pom_sha256': digest(effective), 'normal_build_log_sha256': digest(log), 'original_sdk': sdk, 'artifacts': rows, 'destination': destination, 'dry_run': env['RELEASE_DRY_RUN'] == 'true'}
    bundle, extra = (None, [])
    if destination in ('central', 'both'):
        bundle, extra = central_bundle(frozen, rows, work, policy, env)
        manifest['central_extra_files'] = extra
        manifest['central_bundle_sha256'] = digest(bundle)
    manifest_path = evidence / 'release-manifest.json'
    manifest_path.write_text(json.dumps(manifest, sort_keys=True, indent=2) + '\n')
    receipts = {'manifest_sha256': digest(manifest_path), 'published': [], 'github_release_created': False}
    receipt_path = evidence / 'publication-receipts.json'
    receipt_path.write_text(json.dumps(receipts, indent=2) + '\n')
    if manifest['dry_run']:
        print('Dry-run validation complete; no publication or release creation')
        return
    targets = ['github', 'central'] if destination == 'both' else [destination]
    # Any existing artifact is an explicit refusal, before either destination is mutated.
    for target in targets:
        base = 'https://maven.pkg.github.com/' + repo if target == 'github' else 'https://repo.maven.apache.org/maven2'
        auth = 'Basic ' + base64.b64encode(('x-access-token:' + env['GH_TOKEN']).encode()).decode() if target == 'github' else None
        for row in rows:
            status, _ = request(base + '/' + row['path'], auth, absent=True)
            require_absent(status)
    for target in targets:
        assert_tag(source, tag, sha, repo, token)
        check_files(source, tracked)
        check_files(frozen, rows + extra)
        if target == 'github':
            publish_github(frozen, policy, settings, cache, repo, env)
            verify_remote('https://maven.pkg.github.com/' + repo, rows, 'Basic ' + base64.b64encode(('x-access-token:' + env['GH_TOKEN']).encode()).decode())
            receipt = {'destination': target, 'verified_files': len(rows)}
        else:
            if digest(bundle) != manifest['central_bundle_sha256']:
                raise ValueError('Frozen Central bundle changed')
            receipt = dict(publish_central(bundle, env), destination=target)
            verify_remote('https://repo.maven.apache.org/maven2', rows + extra)
            receipt['verified_files'] = len(rows + extra)
        receipts['published'].append(receipt)
        receipt_path.write_text(json.dumps(receipts, indent=2) + '\n')
    assert_tag(source, tag, sha, repo, token)
    check_files(frozen, rows + extra)
    check_files(source, tracked)
    status, _ = request('https://api.github.com/repos/' + repo + '/releases/tags/' + tag, token, absent=True)
    require_absent(status)
    command(['gh', 'release', 'create', tag, str(manifest_path), str(receipt_path), '--repo', repo, '--verify-tag', '--title', tag, '--generate-notes'], cwd=work, env=env)
    _, raw = request('https://api.github.com/repos/' + repo + '/releases/tags/' + tag, token)
    release = json.loads(raw)
    assets = {asset['name']: asset for asset in release['assets']}
    if release['tag_name'] != tag or release['draft'] or release['prerelease'] or set(assets) != {manifest_path.name, receipt_path.name}:
        raise ValueError('Created release does not match exact evidence inventory')
    for path in (manifest_path, receipt_path):
        headers = {'Authorization': token, 'Accept': 'application/octet-stream', 'User-Agent': 'firefly-immutable-java-release'}
        with urllib.request.build_opener(CredentialRedirect()).open(urllib.request.Request(assets[path.name]['url'], headers=headers), timeout=60) as response:
            if hashlib.sha256(response.read()).hexdigest() != digest(path):
                raise ValueError('Release evidence asset bytes differ')
    assert_tag(source, tag, sha, repo, token)
    # This local final receipt is distinct from the immutable pre-creation receipt attached above.
    (evidence / 'release-created.json').write_text(json.dumps({'manifest_sha256': digest(manifest_path), 'tag': tag, 'source_sha': sha, 'release_id': release['id'], 'assets_verified': 2}) + '\n')
    print('Exact artifact set and immutable release evidence verified')


def cli():
    try:
        main()
    except subprocess.CalledProcessError as error:
        print('Release command failed; preserve retained evidence and any partial publication. Do not retry as success.', file=sys.stderr)
        return error.returncode if error.returncode > 0 else 1
    except (ValueError, KeyError, OSError, ET.ParseError):
        print('Release gate failed; preserve retained evidence and any partial publication. Do not overwrite or retry as success.', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(cli())
