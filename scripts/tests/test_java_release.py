import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
import unittest.mock
import urllib.request
import os

SCRIPT = Path(__file__).resolve().parents[1] / 'java_release.py'
WORKFLOW = SCRIPT.parents[1] / '.github/workflows/java-release.yml'


class WorkflowPolicyTests(unittest.TestCase):
    def test_executable_policy_gate_exists(self):
        self.assertTrue(SCRIPT.is_file(), "Missing fail-closed policy gate")

    def test_no_dependency_source_bootstrap(self):
        text = WORKFLOW.read_text()
        self.assertNotIn('install-upstreams', text)
        self.assertNotIn('Install platform POMs', text)

    def test_no_skip_flags_or_conflict_success(self):
        text = WORKFLOW.read_text()
        self.assertNotIn('-Dmaven.test.skip', text)
        self.assertNotIn('-DskipOpenApiGen', text)
        self.assertNotIn('Artifact already exists (409)', text)

    def test_never_replace_release_or_reparse_raw_version(self):
        text = WORKFLOW.read_text()
        self.assertNotIn('gh release delete', text)
        self.assertNotIn('awk', text)
        self.assertIn('release-tag:', text)
        self.assertIn('dry-run:', text)

    def test_cold_cache_and_immutable_tool_binding(self):
        text = WORKFLOW.read_text()
        self.assertNotIn('cache: maven', text)
        self.assertIn('toJSON(job)', text)
        self.assertNotIn('ref: main', text)


if SCRIPT.exists():
    spec = importlib.util.spec_from_file_location('java_release', SCRIPT)
    release = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(release)

    class ReleaseTests(unittest.TestCase):
        def test_patch_is_counter_not_day(self):
            for version in ('26.10.6', '26.10.32', '26.10.100', '26.10.0', '26.10.06'):
                self.assertEqual(release.version(version), version)
            for version in ('26.00.1', '26.13.1', '2026.10.6', '26.10.-1', '26.10.6-SNAPSHOT'):
                with self.assertRaises(ValueError):
                    release.version(version)

        def test_effective_project_identity_not_dependency_or_parent(self):
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / 'effective.xml'
                path.write_text('<project xmlns="http://maven.apache.org/POM/4.0.0"><parent><version>26.10.1</version></parent><groupId>org.example</groupId><artifactId>owner</artifactId><version>26.10.100</version><packaging>pom</packaging><dependencies><dependency><version>99.01.99</version></dependency></dependencies></project>')
                self.assertEqual(release.effective_projects(path)[0]['version'], '26.10.100')
                path.write_text('<project><parent><version>26.10.6</version></parent><groupId>org.example</groupId><artifactId>owner</artifactId></project>')
                with self.assertRaises(ValueError):
                    release.effective_projects(path)

        def test_suppression_presence_false_is_rejected(self):
            for text in ('-DskipOpenApiGen=false', '-Dmaven.test.skip=true', '-DskipTests=false'):
                with self.assertRaises(ValueError):
                    release.no_suppression(text)
            release.no_suppression('-Xmx2g')

        def test_service_zero_tests_refuses_but_explicit_pom_only_allows(self):
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                with self.assertRaises(ValueError):
                    release.test_evidence(root, {'kind': 'service', 'suites': ['example.Test'], 'minimum_tests': 1})
                self.assertEqual(release.test_evidence(root, {'kind': 'pom', 'suites': [], 'minimum_tests': 0})['tests'], 0)

        def test_exact_suites_and_zero_failures_skips(self):
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                report = root / 'target/surefire-reports/TEST-one.xml'
                report.parent.mkdir(parents=True)
                policy = {'kind': 'service', 'suites': ['example.Test'], 'minimum_tests': 1}
                report.write_text('<testsuite name="example.Test" tests="1" failures="0" errors="0" skipped="0"><testcase name="ordinary"/></testsuite>')
                self.assertEqual(release.test_evidence(root, policy)['tests'], 1)
                report.write_text('<testsuite name="example.Test" tests="1" failures="0" errors="0" skipped="1"><testcase name="ordinary"><skipped/></testcase></testsuite>')
                with self.assertRaises(ValueError):
                    release.test_evidence(root, policy)

        def test_manifest_rehash_rejects_changed_artifact(self):
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                (root / 'artifact.jar').write_bytes(b'ordinary packaged bytes')
                row = {'path': 'artifact.jar', 'sha256': release.digest(root / 'artifact.jar')}
                release.check_files(root, [row])
                (root / 'artifact.jar').write_bytes(b'changed bytes')
                with self.assertRaises(ValueError):
                    release.check_files(root, [row])

        def test_path_must_be_owned_regular_file(self):
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                for path in ('../outside', '/absolute', 'missing'):
                    with self.assertRaises(ValueError):
                        release.owned_file(root, path)

        def test_missing_or_false_original_generator_proof_refuses(self):
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                path = root / 'proof.json'
                path.write_text(json.dumps({'original_producer': False, 'original_generator': True, 'source_sha': 'a' * 40, 'files': []}))
                with self.assertRaises(ValueError):
                    release.sdk_evidence(root, path, 'a' * 40)

        def test_http_conflict_is_failure_not_success(self):
            with self.assertRaises(ValueError):
                release.require_status(409, (200, 201))
            release.require_status(201, (200, 201))

        def test_ambiguous_release_lookup_is_failure(self):
            for status in (200, 401, 403, 409, 500):
                with self.assertRaises(ValueError):
                    release.require_absent(status)
            release.require_absent(404)

        def test_tag_is_exact_effective_version(self):
            release.check_tag('v26.10.06', '26.10.06')
            with self.assertRaises(ValueError):
                release.check_tag('v26.10.6', '26.10.06')

        def test_deploy_failure_exit_is_preserved(self):
            with self.assertRaises(subprocess.CalledProcessError):
                release.command(['sh', '-c', 'exit 9'])

        def test_released_dependencies_only(self):
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / 'effective.xml'
                path.write_text('<project><groupId>org.example</groupId><artifactId>owner</artifactId><version>26.10.6</version><dependencies><dependency><version>1.0-SNAPSHOT</version></dependency></dependencies></project>')
                with self.assertRaises(ValueError):
                    release.effective_projects(path)

        def test_complete_pom_artifact_set(self):
            with tempfile.TemporaryDirectory() as tmp:
                source = Path(tmp) / 'source'
                frozen = Path(tmp) / 'frozen'
                source.mkdir()
                frozen.mkdir()
                (source / 'pom.xml').write_text('<project><groupId>org.example</groupId><artifactId>owner</artifactId><version>26.10.100</version><packaging>pom</packaging></project>')
                project = {'groupId': 'org.example', 'artifactId': 'owner', 'version': '26.10.100', 'packaging': 'pom'}
                policy = {'kind': 'pom', 'artifacts': [dict(project, files={'.pom': 'pom.xml'})]}
                rows = release.freeze(source, frozen, policy, [project])
                self.assertEqual(len(rows), 1)
                release.check_files(frozen, rows)
                with self.assertRaises(ValueError):
                    release.freeze(source, frozen, {'kind': 'pom', 'artifacts': []}, [project])

        def test_valid_generated_proof_preserves_exact_file_bytes(self):
            with tempfile.TemporaryDirectory() as tmp:
                source = Path(tmp)
                (source / 'generated.java').write_text('ordinary generated output')
                proof = {'source_sha': 'a'*40, 'original_producer': True, 'original_generator': True,
                         'producer_executions': ['org.springdoc:springdoc-openapi-maven-plugin:generate:integration-test'],
                         'generator_executions': ['org.openapitools:openapi-generator-maven-plugin:generate'],
                         'files': [{'path': 'generated.java', 'sha256': release.digest(source / 'generated.java')}]}
                (source / 'proof.json').write_text(json.dumps(proof))
                self.assertEqual(release.sdk_evidence(source, source / 'proof.json', 'a'*40), proof)

        def test_github_publisher_conflict_propagates_from_actual_command(self):
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                executable = root / 'mvn'
                executable.write_text('#!/bin/sh\necho "HTTP Status: 409"\nexit 9\n')
                executable.chmod(0o700)
                frozen = root / 'frozen'
                frozen.mkdir()
                policy = {'artifacts': [{'groupId':'org.example', 'artifactId':'owner', 'version':'26.10.6', 'packaging':'pom'}]}
                env = dict(os.environ, PATH=str(root) + os.pathsep + os.environ['PATH'])
                with self.assertRaises(subprocess.CalledProcessError) as refused:
                    release.publish_github(frozen, policy, root/'settings.xml', root/'cache', 'example/owner', env)
                self.assertEqual(refused.exception.returncode, 9)

        def test_authorization_not_forwarded_to_artifact_cdn(self):
            original = urllib.request.Request('https://api.github.com/assets/1', headers={'Authorization': 'Bearer ordinary-test-value'})
            redirected = release.CredentialRedirect().redirect_request(original, None, 302, '', {}, 'https://objects.githubusercontent.com/ordinary')
            self.assertIsNone(redirected.get_header('Authorization'))

        def test_dry_run_cold_orchestration_freezes_without_publishing(self):
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                source = root / 'source'
                source.mkdir()
                runner = root / 'runner'
                runner.mkdir()
                pom = '<project><groupId>org.example</groupId><artifactId>owner</artifactId><version>26.10.100</version><packaging>pom</packaging></project>'
                (source / 'pom.xml').write_text(pom)
                policy = {'schema':1, 'kind':'pom', 'profiles':[], 'suites':[], 'minimum_tests':0,
                          'artifacts':[{'groupId':'org.example','artifactId':'owner','version':'26.10.100','packaging':'pom','files':{'.pom':'pom.xml'}}]}
                (source / 'release-policy.json').write_text(json.dumps(policy))
                calls = []
                def ordinary_command(args, **kwargs):
                    calls.append(args)
                    if args[:3] == ['git','rev-parse','HEAD']:
                        return subprocess.CompletedProcess(args, 0, stdout='a'*40+'\n')
                    if args[:3] == ['git','status','--porcelain']:
                        return subprocess.CompletedProcess(args, 0, stdout=b'')
                    if args[:3] == ['git','ls-files','-z']:
                        return subprocess.CompletedProcess(args, 0, stdout=b'pom.xml\0release-policy.json\0')
                    if args[0] == 'mvn':
                        cache = Path(next(a.split('=',1)[1] for a in args if a.startswith('-Dmaven.repo.local=')))
                        self.assertEqual(list(cache.iterdir()), [])
                        self.assertIn('verify', args)
                        self.assertNotIn('install', args)
                        effective = Path(next(a.split('=',1)[1] for a in args if a.startswith('-Doutput=')))
                        effective.write_text(pom)
                        return subprocess.CompletedProcess(args, 0)
                    self.fail('Unexpected external command')
                env = {'RELEASE_REPOSITORY':'example/owner','RELEASE_SOURCE_SHA':'a'*40,'RELEASE_WORKFLOW_SHA':'b'*40,
                       'RELEASE_TAG':'v26.10.100','RELEASE_DESTINATION':'github','RELEASE_DRY_RUN':'true',
                       'RELEASE_POLICY':'release-policy.json','GH_TOKEN':'ordinary-test-token','RUNNER_TEMP':str(runner)}
                with unittest.mock.patch.dict(os.environ, env, clear=True), \
                     unittest.mock.patch('sys.argv', [str(SCRIPT), '--source', str(source)]), \
                     unittest.mock.patch.object(release, 'command', side_effect=ordinary_command), \
                     unittest.mock.patch.object(release, 'assert_tag'), \
                     unittest.mock.patch.object(release, 'request', return_value=(404,b'')), \
                     unittest.mock.patch.object(release, 'publish_github') as publisher:
                    release.main()
                    publisher.assert_not_called()
                manifest = json.loads((runner/'java-release-evidence/release-manifest.json').read_text())
                self.assertTrue(manifest['dry_run'])
                self.assertEqual(manifest['version'], '26.10.100')
                self.assertEqual(manifest['artifacts'][0]['sha256'], release.digest(source/'pom.xml'))
                self.assertEqual(len([c for c in calls if c[0] == 'mvn']), 1)


if __name__ == '__main__':
    unittest.main()
