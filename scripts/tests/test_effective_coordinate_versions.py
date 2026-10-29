"""Enforcer tooling requirements are not Maven artifact coordinates."""
from pathlib import Path
import tempfile
import unittest
from test_java_release import release


class EffectiveCoordinateVersions(unittest.TestCase):
    def parse(self, body, namespace=True):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'effective.xml'
            declaration = ' xmlns="http://maven.apache.org/POM/4.0.0"' if namespace else ''
            path.write_text('<project' + declaration + '><groupId>io.fireflysoftware</groupId><artifactId>parent</artifactId><version>26.10.6</version><packaging>pom</packaging>' + body + '</project>')
            return release.effective_projects(path)

    def test_java_requirement_range_accepts_released_parent(self):
        body = '<build><pluginManagement><plugins><plugin><executions><execution><configuration><rules><requireJavaVersion><version>[21,)</version></requireJavaVersion></rules></configuration></execution></executions></plugin></plugins></pluginManagement></build>'
        try:
            projects = self.parse(body)
        except ValueError as error:
            self.fail('Java requirement is not an artifact coordinate: ' + str(error))
        self.assertEqual('26.10.6', projects[0]['version'])

    def test_maven_requirement_range_accepts_plugin_configuration(self):
        body = '<build><plugins><plugin><executions><execution><configuration><rules><requireMavenVersion><version>[3.9.11,)</version></requireMavenVersion></rules></configuration></execution></executions></plugin></plugins></build>'
        for namespace in [True, False]:
            with self.subTest(namespace=namespace):
                try:
                    projects = self.parse(body, namespace)
                except ValueError as error:
                    self.fail('Maven requirement is not an artifact coordinate: ' + str(error))
                self.assertEqual('parent', projects[0]['artifactId'])

    def test_dynamic_or_unresolved_artifact_coordinates_still_refuse(self):
        paths = ['parent/version', 'dependencies/dependency/version',
                 'dependencyManagement/dependencies/dependency/version',
                 'build/extensions/extension/version', 'build/plugins/plugin/version',
                 'build/plugins/plugin/dependencies/dependency/version',
                 'build/pluginManagement/plugins/plugin/version',
                 'build/pluginManagement/plugins/plugin/dependencies/dependency/version',
                 'reporting/plugins/plugin/version',
                 'profiles/profile/dependencies/dependency/version',
                 'profiles/profile/dependencyManagement/dependencies/dependency/version',
                 'profiles/profile/build/plugins/plugin/version']
        for path in paths:
            for value in ['1.0-SNAPSHOT', '[1,2)', 'LATEST', 'RELEASE', '${missing}', '']:
                tags = path.split('/')
                body = ''.join('<' + tag + '>' for tag in tags) + value + ''.join('</' + tag + '>' for tag in reversed(tags))
                with self.subTest(path=path, value=value), self.assertRaises(ValueError):
                    self.parse(body)

    def test_fixed_external_versions_are_not_project_patch_identifiers(self):
        for body in ['<dependencies><dependency><version>3.5.1</version></dependency></dependencies>',
                     '<build><plugins><plugin><version>3.5.1</version></plugin></plugins></build>']:
            self.assertEqual('26.10.6', self.parse(body)[0]['version'])

    def test_relocation_target_is_a_released_artifact_coordinate(self):
        for value in ['1.0-SNAPSHOT', '[1,2)', '${missing}', 'LATEST', 'RELEASE', '']:
            body = '<distributionManagement><relocation><groupId>io.fireflysoftware</groupId><artifactId>parent-successor</artifactId><version>' + value + '</version></relocation></distributionManagement>'
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.parse(body)


if __name__ == '__main__':
    unittest.main()
