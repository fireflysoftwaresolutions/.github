"""Ordinary source composition and failure-evidence controls; never Maven or a producer."""
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET
from test_java_release import release, SCRIPT, WORKFLOW

FIXTURES = Path(__file__).with_name('fixtures')
NS = {'m': 'http://maven.apache.org/POM/4.0.0'}


class ValidationCorrections(unittest.TestCase):
    def test_actual_parent_nonweb_defaults_and_web_override_are_module_local_source_contract(self):
        parent = ET.parse(FIXTURES / 'parent557-source.xml').getroot()
        core = ET.parse(FIXTURES / 'cards-core-source.xml').getroot()
        web = ET.parse(FIXTURES / 'cards-web-source.xml').getroot()
        default = parent.findtext('m:properties/m:openapi.gen.skip', namespaces=NS)
        override = web.findtext('m:properties/m:openapi.gen.skip', namespaces=NS)
        self.assertEqual('true', default.strip())
        self.assertIsNone(core.find('m:properties/m:openapi.gen.skip', NS))
        self.assertEqual('false', override.strip())
        projects = ET.Element('projects')
        for name, value in [('parent', default), ('nonweb', default), ('web', override)]:
            project = ET.SubElement(projects, 'project')
            for key, text in [('groupId', 'io.fireflysoftware'), ('artifactId', name), ('version', '26.10.6')]:
                ET.SubElement(project, key).text = text
            ET.SubElement(ET.SubElement(project, 'properties'), 'openapi.gen.skip').text = value.strip()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'source-contract-projection.xml'
            ET.ElementTree(projects).write(path)
            self.assertEqual(['parent', 'nonweb', 'web'], [p['artifactId'] for p in release.effective_projects(path)])

    def test_known_global_suppression_and_skipopenapigen_presence_still_refuse(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'projection.xml'
            for name, value in [('skipTests', 'true'), ('maven.test.skip', 'true'), ('skipITs', 'true'), ('skipOpenApiGen', 'false')]:
                path.write_text('<project><groupId>io.fireflysoftware</groupId><artifactId>owner</artifactId><version>26.10.6</version><properties><' + name + '>' + value + '</' + name + '></properties></project>')
                with self.subTest(name=name), self.assertRaises(ValueError):
                    release.effective_projects(path)

    def test_delegated_producer_properties_never_replace_required_service_verifier(self):
        release.no_suppression('-Dopenapi.gen.skip=true -Dopenapi.generator.skip=true')
        with self.assertRaisesRegex(ValueError, 'verifier'):
            release.validate_policy({'schema': 1, 'kind': 'service', 'profiles': [], 'suites': ['ordinary.Test'], 'minimum_tests': 1, 'artifacts': [{}]})

    def test_ordinary_child_failure_streams_diagnostics_and_preserves_exact_exit(self):
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()) as console:
            path = Path(directory) / 'private.log'
            with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb') as raw:
                with self.assertRaises(subprocess.CalledProcessError) as error:
                    release.command([sys.executable, '-c', 'import sys; print("ordinary compiler error", flush=True); print("ordinary stderr", file=sys.stderr); sys.exit(9)'], stdout=raw, stderr=subprocess.STDOUT, stream_console=True)
            self.assertEqual(9, error.exception.returncode)
            self.assertIn('ordinary compiler error', console.getvalue())
            self.assertIn('ordinary stderr', console.getvalue())
            self.assertIn(b'ordinary compiler error', path.read_bytes())
            self.assertEqual(0o600, path.stat().st_mode & 0o777)

    def test_failure_metadata_contains_only_closed_phase_exit_error_and_file_hashes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            evidence = root / 'evidence'
            evidence.mkdir()
            paths = [root / name for name in ['normal.log', 'effective.xml', 'sdk.json']]
            for path in paths:
                path.write_text('SYNTHETIC_PRIVATE_MATERIAL')
            release.validation_failure(evidence, 'ORIGINAL_SDK', subprocess.CalledProcessError(9, ['ordinary', 'SYNTHETIC_PRIVATE_MATERIAL']), *paths)
            path = evidence / 'validation-failure.json'
            text = path.read_text()
            result = json.loads(text)
            self.assertEqual({'schema', 'phase', 'exit_code', 'error_kind', 'error_sha256', 'normal_build_log_sha256', 'effective_pom_sha256', 'original_sdk_proof_sha256'}, set(result))
            self.assertEqual(9, result['exit_code'])
            self.assertEqual('ORIGINAL_SDK', result['phase'])
            self.assertEqual(hashlib.sha256(paths[0].read_bytes()).hexdigest(), result['normal_build_log_sha256'])
            self.assertNotIn('SYNTHETIC_PRIVATE_MATERIAL', text)
            self.assertLess(len(text.encode()), 4096)
            self.assertEqual(0o600, path.stat().st_mode & 0o777)

    def test_cli_preserves_original_failure_code_without_printing_command_or_credentials(self):
        with patch.object(release, 'main', side_effect=subprocess.CalledProcessError(9, ['SYNTHETIC_PRIVATE_COMMAND'])), contextlib.redirect_stderr(io.StringIO()) as console:
            self.assertEqual(9, release.cli())
        self.assertNotIn('SYNTHETIC_PRIVATE_COMMAND', console.getvalue())

    def test_failure_upload_is_explicit_json_only_not_raw_directory(self):
        text = WORKFLOW.read_text()
        self.assertIn('validation-failure.json', text)
        self.assertNotIn('path: ${{ runner.temp }}/java-release-evidence/\n', text)
        self.assertNotIn('normal-build.log', text)
        self.assertNotIn('effective-pom.xml', text)

    def test_actual_cold_orchestration_failure_retains_metadata_and_rethrows_original_exit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, runner = root / 'source', root / 'runner'
            source.mkdir()
            runner.mkdir()
            (source / 'pom.xml').write_text('<project/>')
            (source / 'policy.json').write_text(json.dumps({'schema': 1, 'kind': 'pom', 'profiles': [], 'suites': [], 'minimum_tests': 0, 'artifacts': [{}]}))
            def command(args, **kwargs):
                if args[:3] == ['git', 'rev-parse', 'HEAD']:
                    return subprocess.CompletedProcess(args, 0, stdout='a' * 40 + '\n')
                if args[:3] == ['git', 'status', '--porcelain']:
                    return subprocess.CompletedProcess(args, 0, stdout=b'')
                if args[:3] == ['git', 'ls-files', '-z']:
                    return subprocess.CompletedProcess(args, 0, stdout=b'pom.xml\0policy.json\0')
                if args[0] == 'mvn':
                    kwargs['stdout'].write(b'ordinary unmodified cold-build failure\n')
                    raise subprocess.CalledProcessError(9, args)
                self.fail('Unexpected command')
            env = {'RELEASE_REPOSITORY': 'example/owner', 'RELEASE_SOURCE_SHA': 'a' * 40, 'RELEASE_WORKFLOW_SHA': 'b' * 40, 'RELEASE_TAG': 'v26.10.6', 'RELEASE_DESTINATION': 'github', 'RELEASE_DRY_RUN': 'true', 'RELEASE_POLICY': 'policy.json', 'GH_TOKEN': 'SYNTHETIC_TEST_TOKEN', 'RUNNER_TEMP': str(runner)}
            with patch.dict(os.environ, env, clear=True), patch('sys.argv', [str(SCRIPT), '--source', str(source)]), patch.object(release, 'command', side_effect=command), patch.object(release, 'assert_tag'), patch.object(release, 'request', return_value=(404, b'')):
                with self.assertRaises(subprocess.CalledProcessError) as error:
                    release.main()
            self.assertEqual(9, error.exception.returncode)
            metadata = json.loads((runner / 'java-release-evidence/validation-failure.json').read_text())
            self.assertEqual('COLD_BUILD', metadata['phase'])
            self.assertEqual(9, metadata['exit_code'])
            self.assertEqual(hashlib.sha256(b'ordinary unmodified cold-build failure\n').hexdigest(), metadata['normal_build_log_sha256'])
            self.assertIsNone(metadata['effective_pom_sha256'])
            self.assertIsNone(metadata['original_sdk_proof_sha256'])
