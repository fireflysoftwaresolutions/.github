#!/usr/bin/env ruby
require 'yaml'
require 'json'
require 'tmpdir'
require 'open3'

path = File.expand_path('../../.github/workflows/java-release.yml', __dir__)
workflow = YAML.load_file(path)
trigger = workflow['on'] || workflow[true]
inputs = trigger.fetch('workflow_call').fetch('inputs')
raise 'dry run is not default' unless inputs.fetch('dry-run').fetch('default') == true
%w[release-tag release-policy].each { |name| raise 'required policy input missing' unless inputs.fetch(name).fetch('required') }
job = workflow.fetch('jobs').fetch('release')
raise 'parallel cancellation could interrupt publication' unless job.fetch('concurrency').fetch('cancel-in-progress') == false
steps = job.fetch('steps')
steps.each do |step|
  next unless step['uses']
  raise 'mutable action dependency' unless /@[0-9a-f]{40}\z/.match?(step['uses'])
  raise 'dependency cache restored' if step.fetch('with', {}).key?('cache')
end
upload = steps.find { |step| step.fetch('uses', '').start_with?('actions/upload-artifact@') }
expected = %w[validation-state.json validation-failure.json release-manifest.json publication-receipts.json release-created.json]
paths = upload.fetch('with').fetch('path').lines.map(&:strip)
raise 'raw or unbounded evidence upload path' unless paths == expected.map { |name| '${{ runner.temp }}/java-release-evidence/' + name }
Dir.mktmpdir('release-shell-lint-') do |dir|
  steps.each_with_index do |step, index|
    next unless step['run']
    shell = File.join(dir, "step-#{index}.sh")
    File.write(shell, "#!/usr/bin/env bash\n" + step['run'])
    output, result = Open3.capture2e('shellcheck', shell)
    raise output unless result.success?
  end
end
puts JSON.generate({structured_yaml: 'PASS', immutable_action_steps: steps.count { |step| step["uses"] }, immutable_action_kinds: steps.map { |step| step["uses"] }.compact.uniq.size, required_inputs: 2, shellcheck: 'PASS', default_dry_run: true})
