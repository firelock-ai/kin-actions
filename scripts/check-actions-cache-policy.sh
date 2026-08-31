#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Firelock, LLC

set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
workflow_root="${1:-${root}/.github/workflows}"
action_root="${2:-$(dirname "${workflow_root}")/actions}"

ruby - "${workflow_root}" "${action_root}" <<'RUBY'
require "psych"
require "yaml"

workflow_root = File.expand_path(ARGV.fetch(0))
action_root = File.expand_path(ARGV.fetch(1))
abort("FAIL: workflow directory does not exist: #{workflow_root}") unless Dir.exist?(workflow_root)

workflow_name = "cargo-registry-release.yml"
cache_sha = "55cc8345863c7cc4c66a329aec7e433d2d1c52a9"
checkout_sha = "d23441a48e516b6c34aea4fa41551a30e30af803"
restore_action = "actions/cache/restore@#{cache_sha}"
save_action = "actions/cache/save@#{cache_sha}"
checkout_action = "actions/checkout@#{checkout_sha}"
protected_jobs = {
  "registry_smoke" => [1, 1],
  "test" => [1, 0],
  "publish" => [1, 0],
  "consumer_smoke" => [1, 0],
}.freeze
allowed_paths = ["~/.cargo/registry", "~/.cargo/git"].freeze
restore_key = 'cargo-source-v1-${{ runner.os }}-${{ runner.arch }}'
save_key = '${{ steps.cargo_source_cache.outputs.cache-primary-key }}'
save_condition = (
  "success() && github.event_name == 'push' && github.ref == 'refs/heads/main' && " \
  "steps.cargo_source_cache.outputs.cache-hit != 'true'"
).freeze
guard_run = "./scripts/check-actions-cache-policy.sh\n./scripts/test-actions-cache-policy.sh\n"

errors = []
counts = Hash.new { |hash, key| hash[key] = [0, 0] }
workflows = Dir[File.join(workflow_root, "*.{yml,yaml}")].sort
abort("FAIL: no workflow files found under #{workflow_root}") if workflows.empty?

def inspect_yaml_node(node, file_name, errors)
  case node
  when Psych::Nodes::Alias
    errors << "#{file_name}:#{node.start_line + 1}: YAML aliases are forbidden in workflow policy"
  when Psych::Nodes::Mapping
    seen = {}
    node.children.each_slice(2) do |key_node, value_node|
      unless key_node.is_a?(Psych::Nodes::Scalar)
        errors << "#{file_name}:#{key_node.start_line + 1}: complex YAML mapping keys are forbidden"
        inspect_yaml_node(value_node, file_name, errors)
        next
      end

      key = key_node.value
      if seen.key?(key)
        errors << (
          "#{file_name}:#{key_node.start_line + 1}: duplicate YAML mapping key #{key.inspect}; " \
          "first declared at line #{seen.fetch(key)}"
        )
      else
        seen[key] = key_node.start_line + 1
      end
      inspect_yaml_node(value_node, file_name, errors)
    end
  else
    Array(node.children).each { |child| inspect_yaml_node(child, file_name, errors) }
  end
end

def lines(value)
  return nil unless value.is_a?(String)

  value.lines.map(&:strip).reject(&:empty?)
end

def each_mapping(value, &block)
  case value
  when Hash
    yield(value)
    value.each_value { |child| each_mapping(child, &block) }
  when Array
    value.each { |child| each_mapping(child, &block) }
  end
end

def parse_yaml(path, display_name, errors)
  content = File.read(path, encoding: "UTF-8")
  syntax_tree = Psych.parse_stream(content, filename: path)
  inspect_yaml_node(syntax_tree, display_name, errors)
  YAML.safe_load(
    content,
    permitted_classes: [],
    permitted_symbols: [],
    aliases: false,
    filename: path,
  )
rescue Psych::Exception => error
  errors << "#{display_name}: YAML parse failed: #{error.message}"
  nil
end

workflows.each do |workflow|
  file_name = File.basename(workflow)
  document = parse_yaml(workflow, file_name, errors)
  next unless document.is_a?(Hash)

  jobs = document["jobs"]
  unless jobs.is_a?(Hash)
    errors << "#{file_name}: jobs must be a YAML mapping"
    next
  end

  if file_name == workflow_name
    protected_jobs.each_key do |job_name|
      job = jobs[job_name]
      unless job.is_a?(Hash)
        errors << "#{file_name}: required job #{job_name.inspect} is missing"
        next
      end
      if job.key?("uses")
        errors << "#{file_name}: job #{job_name.inspect} must not delegate to a reusable workflow"
      end
      unless job["runs-on"] == "ubuntu-latest"
        errors << "#{file_name}: job #{job_name.inspect} must remain on ubuntu-latest"
      end
      unless job["steps"].is_a?(Array)
        errors << "#{file_name}: job #{job_name.inspect} steps must be a YAML sequence"
      end
      if job_name == "registry_smoke" && job.key?("continue-on-error")
        errors << "#{file_name}: registry_smoke must not mask job failure before saving a cache"
      end
    end
  end

  if file_name == "self-test.yml"
    policy_job = jobs["python-unit"]
    policy_steps = policy_job.is_a?(Hash) && policy_job["steps"].is_a?(Array) ? policy_job["steps"] : []
    enforcement = policy_steps.select do |step|
      step.is_a?(Hash) && step["id"] == "actions_cache_policy"
    end
    if enforcement.length != 1
      errors << "self-test.yml: python-unit must contain exactly one actions_cache_policy enforcement step"
    else
      step = enforcement.fetch(0)
      unless step["name"] == "Enforce bounded Actions cache authority" && step["run"] == guard_run
        errors << "self-test.yml: actions_cache_policy must invoke both exact guard and falsifier commands"
      end
      if step.key?("if") || step.key?("continue-on-error")
        errors << "self-test.yml: actions_cache_policy must be unconditional and failure-authoritative"
      end
    end
  end

  jobs.each do |job_name, job|
    next unless job.is_a?(Hash)

    steps = job["steps"]
    next if steps.nil?
    unless steps.is_a?(Array)
      errors << "#{file_name}: job #{job_name.inspect} steps must be a YAML sequence"
      next
    end

    protected = file_name == workflow_name && protected_jobs.key?(job_name)
    steps.each_with_index do |step, index|
      next unless step.is_a?(Hash)

      action = step["uses"]
      location = "#{file_name}: job #{job_name.inspect} step #{index + 1}"

      if action.is_a?(String) && action.downcase.start_with?("actions/setup-") && step["with"].is_a?(Hash)
        cache_inputs = step["with"].keys.select { |key| key.to_s.downcase.start_with?("cache") }
        unless cache_inputs.empty?
          errors << (
            "#{location}: setup-action cache inputs #{cache_inputs.inspect} are forbidden; " \
            "all caches must remain visible to the audited policy"
          )
        end
      end

      if action.is_a?(String) && action.downcase.include?("cache") &&
         !action.downcase.start_with?("actions/cache")
        errors << (
          "#{location}: unaudited cache-capable action #{action.inspect} is forbidden; " \
          "declare caching through the pinned bounded policy"
        )
      end

      if protected && action.is_a?(String) && ![checkout_action, restore_action, save_action].include?(action)
        errors << (
          "#{location}: protected release jobs may use only pinned checkout and audited cache actions; " \
          "remote or repo-local composite actions are forbidden"
        )
      end

      next unless action.is_a?(String) && action.downcase.start_with?("actions/cache")

      cache_paths = lines(step.dig("with", "path")) if step["with"].is_a?(Hash)
      key = step.dig("with", "key") if step["with"].is_a?(Hash)
      cache_inputs = step["with"].is_a?(Hash) ? step["with"].keys : []

      if cache_inputs.sort != ["key", "path"]
        errors << (
          "#{location}: cache action inputs must be exactly path and key; " \
          "lookup-only, fail-on-cache-miss, restore prefixes, and semantic drift are forbidden"
        )
      end

      case action
      when restore_action
        counts[[file_name, job_name]][0] += 1
        errors << "#{location}: restore id must be cargo_source_cache" unless step["id"] == "cargo_source_cache"
        if step.key?("if")
          errors << "#{location}: cache restore must run on every workflow ref"
        end
        if steps.take(index).any? { |prior| prior.is_a?(Hash) && prior.key?("run") }
          errors << "#{location}: cache restore must precede every run step"
        end
        if key != restore_key
          errors << "#{location}: restore key must be the bounded OS and architecture epoch #{restore_key}"
        end
        if step.fetch("with", {}).key?("restore-keys")
          errors << "#{location}: restore-keys are forbidden because they cross the bounded cache epoch"
        end
      when save_action
        counts[[file_name, job_name]][1] += 1
        if step["if"] != save_condition
          errors << "#{location}: cache save must be restricted to a main push cache miss"
        end
        if key != save_key
          errors << "#{location}: save key must come from the restore primary key"
        end
        if index != steps.length - 1
          errors << "#{location}: cache save must be the last declared job step"
        end
        unless steps.take(index).any? do |prior|
                 prior.is_a?(Hash) && prior["id"] == "cargo_source_cache" &&
                   prior["uses"] == restore_action
               end
          errors << "#{location}: cache save must follow cargo_source_cache restore in the same job"
        end
        prior_steps = steps.take(index)
        if prior_steps.any? { |prior| prior.is_a?(Hash) && prior.key?("continue-on-error") }
          errors << "#{location}: cache save must not follow a continue-on-error step"
        end
        build_step = prior_steps.last
        unless build_step.is_a?(Hash) && build_step["name"] == "Build without local patches" &&
               build_step["run"].is_a?(String)
          errors << "#{location}: cache save must immediately follow the authoritative registry build"
        else
          build_lines = build_step["run"].lines.map(&:strip).reject(&:empty?)
          unless build_lines.first == "set -euo pipefail"
            errors << "#{location}: authoritative registry build must fail closed with set -euo pipefail"
          end
        end
      else
        errors << (
          "#{location}: use exact pinned #{restore_action} or #{save_action}, not #{action}"
        )
      end

      if key.to_s.match?(/hashFiles\(|github\.(?:sha|ref|head_ref|base_ref|run_id|run_number)|matrix\./)
        errors << "#{location}: cache keys must not expand per dependency hash, ref, SHA, run, or matrix"
      end
      if cache_paths != allowed_paths
        errors << (
          "#{location}: cache paths must be exactly #{allowed_paths.inspect}; " \
          "target output is forbidden"
        )
      end
      if cache_paths&.any? { |path| path.split("/").include?("target") }
        errors << "#{location}: target output is forbidden in Actions caches"
      end
    end
  end
end

Dir[File.join(action_root, "**", "*.{yml,yaml}")].sort.each do |action_file|
  relative_name = action_file.delete_prefix("#{action_root}/")
  document = parse_yaml(action_file, relative_name, errors)
  next unless document

  each_mapping(document) do |mapping|
    action = mapping["uses"]
    next unless action.is_a?(String) && action.downcase.start_with?("actions/cache")

    errors << (
      "#{relative_name}: repo-local composite actions must not invoke actions/cache; " \
      "declare bounded caches in the audited reusable workflow"
    )
  end
end

protected_jobs.each do |job_name, expected|
  actual = counts.fetch([workflow_name, job_name], [0, 0])
  next if actual == expected

  errors << (
    "#{workflow_name}: job #{job_name.inspect} expected #{expected[0]} restore and " \
    "#{expected[1]} save steps; found #{actual[0]} restore and #{actual[1]} save steps"
  )
end

counts.each do |(file_name, job_name), actual|
  next if file_name == workflow_name && protected_jobs.key?(job_name)
  next if actual == [0, 0]

  errors << (
    "#{file_name}: job #{job_name.inspect} has an unexpected cache action; " \
    "add it to the bounded policy deliberately"
  )
end

unless errors.empty?
  warn("FAIL: GitHub Actions cache policy is not bounded:")
  errors.each { |error| warn("  - #{error}") }
  exit(1)
end

puts(
  "OK: reusable Cargo caches restore source-only state on every ref, share one OS/arch epoch, " \
  "and save only once from a main push (4 restores, 1 save)."
)
RUBY
