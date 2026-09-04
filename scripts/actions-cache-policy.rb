#!/usr/bin/env ruby
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Firelock, LLC

require "pathname"
require "psych"
require "yaml"

workflow_root = File.realpath(File.expand_path(ARGV.fetch(0)))
action_root_candidate = File.expand_path(ARGV.fetch(1))
action_root = Dir.exist?(action_root_candidate) ? File.realpath(action_root_candidate) : action_root_candidate
repository_root = File.realpath(File.expand_path("../..", workflow_root))
abort("FAIL: workflow directory does not exist: #{workflow_root}") unless Dir.exist?(workflow_root)

workflow_name = "cargo-registry-release.yml"
cache_sha = "55cc8345863c7cc4c66a329aec7e433d2d1c52a9"
checkout_sha = "d23441a48e516b6c34aea4fa41551a30e30af803"
setup_python_sha = "a26af69be951a213d495a4c3e4e4022e16d87065"
app_token_sha = "bcd2ba49218906704ab6c1aa796996da409d3eb1"
create_pr_sha = "c5a7806660adbe173f04e3e038b0ccdcd758773c"
restore_action = "actions/cache/restore@#{cache_sha}"
save_action = "actions/cache/save@#{cache_sha}"
checkout_action = "actions/checkout@#{checkout_sha}"
allowed_remote_actions = [
  checkout_action,
  restore_action,
  save_action,
  "actions/setup-python@#{setup_python_sha}",
  "actions/create-github-app-token@#{app_token_sha}",
  "peter-evans/create-pull-request@#{create_pr_sha}",
].freeze
protected_jobs = {
  "registry_smoke" => [1, 1],
  "test" => [1, 0],
  "publish" => [1, 0],
  "consumer_smoke" => [1, 0],
}.freeze
protected_job_keys = {
  "registry_smoke" => %w[name runs-on steps],
  "test" => %w[name runs-on steps],
  "publish" => %w[environment if name needs outputs runs-on steps],
  "consumer_smoke" => %w[if name needs runs-on steps],
}.freeze
protected_step_names = {
  "registry_smoke" => [
    nil,
    nil,
    "Restore bounded cargo source cache",
    "Install Rust toolchain",
    "Force registry-only Kin config",
    "Fetch complete cargo source graph",
    "Save bounded cargo source cache from main",
    "Build without local patches",
  ],
  "test" => [nil, "Restore bounded cargo source cache", "Install Rust toolchain", "Run verification"],
  "publish" => [
    nil,
    nil,
    "Restore bounded cargo source cache",
    "Install Rust toolchain",
    "Resolve version",
    "Publish",
  ],
  "consumer_smoke" => [
    nil,
    "Restore bounded cargo source cache",
    "Install Rust toolchain",
    "Build exact published version from registry",
  ],
}.freeze
protected_step_keys = {
  "registry_smoke" => [
    %w[uses],
    %w[uses with],
    %w[id name uses with],
    %w[name run],
    %w[name run],
    %w[env id name run],
    %w[if name uses with],
    %w[env name run],
  ],
  "test" => [%w[uses], %w[id name uses with], %w[name run], %w[name run]],
  "publish" => [
    %w[uses],
    %w[uses with],
    %w[id name uses with],
    %w[name run],
    %w[id name run],
    %w[env name run],
  ],
  "consumer_smoke" => [
    %w[uses with],
    %w[id name uses with],
    %w[name run],
    %w[env name run],
  ],
}.freeze
allowed_paths = ["~/.cargo/registry", "~/.cargo/git"].freeze
restore_key = 'cargo-source-v2-${{ runner.os }}-${{ runner.arch }}'
save_key = '${{ steps.cargo_source_cache.outputs.cache-primary-key }}'
save_condition = (
  "success() && steps.cargo_source_fetch.outcome == 'success' && " \
  "github.event_name == 'push' && github.ref == 'refs/heads/main' && " \
  "steps.cargo_source_cache.outputs.cache-hit != 'true'"
).freeze
guard_run = "./scripts/check-actions-cache-policy.sh\n./scripts/test-actions-cache-policy.sh\n"
helper_checkout = {
  "repository" => '${{ job.workflow_repository }}',
  "ref" => '${{ job.workflow_sha }}',
  "path" => ".kin-actions",
}.freeze
install_rust_run = <<~'RUN'
  set -euo pipefail
  rustup toolchain install 1.96.0 --profile minimal --no-self-update
  # Tolerated for the reason upstream tolerated it: on a runner whose rustup
  # already carries a default, re-defaulting can fail while the toolchain is
  # installed and usable.
  rustup default 1.96.0 || true
  echo "CARGO_INCREMENTAL=${CARGO_INCREMENTAL:-0}" >> "$GITHUB_ENV"
  echo "CARGO_TERM_COLOR=${CARGO_TERM_COLOR:-always}" >> "$GITHUB_ENV"
RUN
registry_config_run = <<~'RUN'
  set -euo pipefail
  if [[ -L .cargo || -L .cargo/config.toml ]]; then
    echo "Cargo config path must not be a symlink" >&2
    exit 1
  fi
  mkdir -p .cargo
  cat > .cargo/config.toml <<'EOF'
  [registries.kin]
  index = "sparse+https://kinlab.ai/registry/cargo/"
  EOF
RUN
fetch_run = <<~'RUN'
  set -euo pipefail
  if [[ ! -f "$MANIFEST" || -L "$MANIFEST" ]]; then
    echo "Cargo manifest must be a regular non-symlink file" >&2
    exit 1
  fi
  workspace_root="$(realpath "$GITHUB_WORKSPACE")"
  manifest_path="$(realpath "$MANIFEST")"
  case "$manifest_path" in
    "$workspace_root"/*) ;;
    *) echo "Cargo manifest escapes the checked-out workspace" >&2; exit 1 ;;
  esac
  workspace_manifest="$(
    cargo locate-project --workspace --message-format plain \
      --manifest-path "$MANIFEST"
  )"
  case "$(realpath "$workspace_manifest")" in
    "$workspace_root"/*) ;;
    *) echo "Cargo workspace manifest escapes the checkout" >&2; exit 1 ;;
  esac
  fetch_args=(fetch --manifest-path "$MANIFEST")
  if [[ -f "${workspace_manifest%/*}/Cargo.lock" ]]; then
    fetch_args+=(--locked)
  fi
  cargo "${fetch_args[@]}"
RUN
fetch_env = {
  "MANIFEST" => '${{ inputs.manifest }}',
  "CARGO_TARGET_DIR" => '${{ runner.temp }}/kin-actions-target',
}.freeze
build_env = {
  "PACKAGE" => '${{ inputs.package }}',
  "MANIFEST" => '${{ inputs.manifest }}',
  "REGISTRY_BUILD_COMMAND" => '${{ inputs.registry-build-command }}',
  "CARGO_TARGET_DIR" => '${{ runner.temp }}/kin-actions-target',
}.freeze
cargo_workflow_env = {
  "CARGO_TERM_COLOR" => "always",
  "KINLAB_CARGO_REGISTRY_URL" => "https://kinlab.ai",
}.freeze

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

def lines(value)
  return nil unless value.is_a?(String)

  value.lines.map(&:strip).reject(&:empty?)
end

def exact_keys(mapping, expected, location, errors)
  actual = mapping.is_a?(Hash) ? mapping.keys.map(&:to_s).sort : []
  wanted = expected.sort
  return if actual == wanted

  errors << "#{location}: keys must be exactly #{wanted.inspect}; found #{actual.inspect}"
end

def inside?(path, root)
  path == root || path.start_with?("#{root}/")
end

def local_reference?(reference)
  reference.is_a?(String) && reference.start_with?("./")
end

local_scan = nil
scanned_local_actions = {}
scanning_local_actions = {}
local_scan = lambda do |reference, source_name|
  raw_path = File.expand_path(reference.delete_prefix("./"), repository_root)
  begin
    action_dir = File.realpath(raw_path)
  rescue Errno::ENOENT
    errors << "#{source_name}: local action #{reference.inspect} does not exist"
    next
  end
  unless inside?(action_dir, repository_root)
    errors << "#{source_name}: local action #{reference.inspect} escapes the repository"
    next
  end
  candidates = %w[action.yml action.yaml].map { |name| File.join(action_dir, name) }.select { |path| File.file?(path) }
  if candidates.length != 1
    errors << "#{source_name}: local action #{reference.inspect} must resolve to exactly one action.yml or action.yaml"
    next
  end
  metadata = File.realpath(candidates.fetch(0))
  next if scanned_local_actions[metadata]
  if scanning_local_actions[metadata]
    errors << "#{source_name}: local action cycle reaches #{metadata.delete_prefix("#{repository_root}/")}"
    next
  end
  scanning_local_actions[metadata] = true
  display = metadata.delete_prefix("#{repository_root}/")
  document = parse_yaml(metadata, display, errors)
  runs = document.is_a?(Hash) ? document["runs"] : nil
  unless runs.is_a?(Hash) && runs["using"] == "composite" && runs["steps"].is_a?(Array)
    errors << "#{display}: local actions on the audited surface must be transparent composites"
  else
    runs["steps"].each_with_index do |step, index|
      next unless step.is_a?(Hash)

      action = step["uses"]
      location = "#{display}: composite step #{index + 1}"
      inspect_action = Thread.current[:inspect_cache_action]
      inspect_action.call(action, step, location, true) if action.is_a?(String)
      local_scan.call(action, location) if local_reference?(action)
    end
  end
  scanning_local_actions.delete(metadata)
  scanned_local_actions[metadata] = true
end

inspect_action = lambda do |action, step, location, local_action|
  downcase = action.downcase
  inputs = step["with"].is_a?(Hash) ? step["with"] : {}

  if downcase.start_with?("actions/setup-node@")
    unless action.match?(/\Aactions\/setup-node@[0-9a-f]{40}\z/)
      errors << "#{location}: setup-node must use an exact immutable 40-character commit"
    end
    unless inputs["package-manager-cache"] == false
      errors << "#{location}: setup-node must set package-manager-cache to boolean false"
    end
    explicit = inputs.keys.map(&:to_s).select { |key| %w[cache cache-dependency-path].include?(key.downcase) }
    unless explicit.empty?
      errors << "#{location}: setup-node cache inputs #{explicit.inspect} are forbidden"
    end
    next
  end

  if downcase.start_with?("actions/setup-go@")
    unless action.match?(/\Aactions\/setup-go@[0-9a-f]{40}\z/)
      errors << "#{location}: setup-go must use an exact immutable 40-character commit"
    end
    unless inputs["cache"] == false
      errors << "#{location}: setup-go must set cache to boolean false"
    end
    if inputs.key?("cache-dependency-path")
      errors << "#{location}: setup-go cache-dependency-path is forbidden"
    end
    next
  end

  if downcase.start_with?("actions/setup-")
    cache_inputs = inputs.keys.map(&:to_s).select { |key| key.downcase.start_with?("cache") }
    unless cache_inputs.empty?
      errors << (
        "#{location}: setup-action cache inputs #{cache_inputs.inspect} are forbidden; " \
        "all caches must remain visible to the audited policy"
      )
    end
  end

  if local_reference?(action)
    next
  end

  if downcase.start_with?("actions/cache")
    if local_action
      errors << (
        "#{location}: repo-local composite actions must not invoke actions/cache; " \
        "declare bounded caches in the audited reusable workflow"
      )
    end
    next
  end

  unless allowed_remote_actions.include?(action)
    errors << "#{location}: unaudited remote action #{action.inspect} is forbidden"
  end
end
Thread.current[:inspect_cache_action] = inspect_action

documents = {}
workflows.each do |workflow|
  file_name = File.basename(workflow)
  document = parse_yaml(workflow, file_name, errors)
  documents[file_name] = document
  next unless document.is_a?(Hash)

  jobs = document["jobs"]
  unless jobs.is_a?(Hash)
    errors << "#{file_name}: jobs must be a YAML mapping"
    next
  end

  if [workflow_name, "self-test.yml"].include?(file_name) && document.key?("defaults")
    errors << "#{file_name}: workflow defaults are forbidden on an authoritative policy surface"
  end

  if file_name == workflow_name
    unless document["env"] == cargo_workflow_env
      errors << (
        "#{file_name}: workflow env must remain exactly the two inert release values; " \
        "BASH_ENV, PATH, RUBYOPT, Cargo, and home redirection are forbidden"
      )
    end

    protected_jobs.each_key do |job_name|
      job = jobs[job_name]
      unless job.is_a?(Hash)
        errors << "#{file_name}: required job #{job_name.inspect} is missing"
        next
      end
      exact_keys(job, protected_job_keys.fetch(job_name), "#{file_name}: job #{job_name.inspect}", errors)
      errors << "#{file_name}: job #{job_name.inspect} must remain on ubuntu-latest" unless job["runs-on"] == "ubuntu-latest"
      steps = job["steps"]
      unless steps.is_a?(Array)
        errors << "#{file_name}: job #{job_name.inspect} steps must be a YAML sequence"
        next
      end
      names = steps.map { |step| step.is_a?(Hash) ? step["name"] : :invalid }
      unless names == protected_step_names.fetch(job_name)
        errors << "#{file_name}: job #{job_name.inspect} must preserve every release proof step in exact order"
      end
      expected_keys = protected_step_keys.fetch(job_name)
      steps.each_with_index do |step, index|
        next unless step.is_a?(Hash) && expected_keys[index]

        exact_keys(step, expected_keys.fetch(index), "#{file_name}: job #{job_name.inspect} step #{index + 1}", errors)
      end
      if job.fetch("env", {}).keys.map(&:to_s).any? { |key| %w[HOME CARGO_HOME CARGO_TARGET_DIR].include?(key) }
        errors << "#{file_name}: job #{job_name.inspect} must not redirect Cargo or home paths"
      end
    end

    publish = jobs["publish"]
    if publish.is_a?(Hash)
      errors << "#{file_name}: publish needs must preserve all three prerequisite jobs" unless publish["needs"] == %w[version_gate registry_smoke test]
      expected_if = "github.event_name == 'push' && github.ref == 'refs/heads/main' && needs.version_gate.outputs.release_candidate == 'true'"
      errors << "#{file_name}: publish condition must preserve exact release admission" unless publish["if"] == expected_if
      errors << "#{file_name}: publish environment must remain caller-selected" unless publish["environment"] == '${{ inputs.publish-environment }}'
      unless publish["outputs"] == {"version" => '${{ steps.version.outputs.version }}'}
        errors << "#{file_name}: publish version output must remain bound to the version step"
      end
    end
    consumer = jobs["consumer_smoke"]
    if consumer.is_a?(Hash)
      errors << "#{file_name}: consumer_smoke must depend on publish" unless consumer["needs"] == "publish"
      expected_if = "github.event_name == 'push' && github.ref == 'refs/heads/main'"
      errors << "#{file_name}: consumer_smoke condition must preserve exact main-push proof" unless consumer["if"] == expected_if
    end

    registry = jobs["registry_smoke"]
    if registry.is_a?(Hash) && registry["steps"].is_a?(Array)
      steps = registry["steps"]
      candidate, helper, _restore, install, config, fetch, _save, build = steps
      unless candidate == {"uses" => checkout_action}
        errors << "#{file_name}: registry_smoke candidate checkout must use the exact workflow commit"
      end
      unless helper == {"uses" => checkout_action, "with" => helper_checkout}
        errors << "#{file_name}: registry_smoke helper checkout must bind job.workflow_repository and job.workflow_sha"
      end
      unless install.is_a?(Hash) && install["run"] == install_rust_run
        errors << "#{file_name}: registry_smoke Rust setup before cache save must remain exact"
      end
      unless config.is_a?(Hash) && config["run"] == registry_config_run
        errors << "#{file_name}: registry_smoke registry config before cache save must remain exact"
      end
      unless fetch.is_a?(Hash) && fetch["id"] == "cargo_source_fetch" && fetch["env"] == fetch_env && fetch["run"] == fetch_run
        errors << "#{file_name}: cache save authority must be the exact fail-closed Cargo fetch in runner.temp"
      end
      unless build.is_a?(Hash) && build["env"] == build_env && build["run"] == "python3 .kin-actions/scripts/run-registry-build.py"
        errors << "#{file_name}: registry build must use the fail-closed typed helper after cache save"
      end
    end

    {
      "test" => [[0, nil]],
      "publish" => [[0, nil], [1, helper_checkout]],
      "consumer_smoke" => [[0, helper_checkout]],
    }.each do |job_name, checkout_specs|
      steps = jobs.dig(job_name, "steps")
      next unless steps.is_a?(Array)

      checkout_specs.each do |index, expected_with|
        expected = {"uses" => checkout_action}
        expected["with"] = expected_with if expected_with
        unless steps[index] == expected
          errors << "#{file_name}: job #{job_name.inspect} checkout #{index + 1} must remain exact"
        end
      end
    end
  end

  if file_name == "self-test.yml"
    if document.key?("env")
      errors << "self-test.yml: workflow env is forbidden before policy enforcement"
    end
    triggers = document[true] || document["on"]
    expected_triggers = {"pull_request" => nil, "push" => {"branches" => ["main"]}, "merge_group" => nil}
    errors << "self-test.yml: pull_request, push main, and merge_group triggers must remain exact" unless triggers == expected_triggers
    policy_job = jobs["python-unit"]
    unless policy_job.is_a?(Hash)
      errors << "self-test.yml: python-unit policy job is missing"
    else
      exact_keys(policy_job, %w[name runs-on steps], "self-test.yml: python-unit", errors)
      errors << "self-test.yml: python-unit must remain on ubuntu-latest" unless policy_job["runs-on"] == "ubuntu-latest"
      policy_steps = policy_job["steps"].is_a?(Array) ? policy_job["steps"] : []
      expected_checkout = {"uses" => checkout_action, "with" => {"fetch-depth" => 0}}
      unless policy_steps.fetch(0, nil) == expected_checkout
        errors << "self-test.yml: python-unit must begin with the exact candidate checkout and no ref redirection"
      end
      enforcement = policy_steps.select { |step| step.is_a?(Hash) && step["id"] == "actions_cache_policy" }
      if enforcement.length != 1 || policy_steps.fetch(1, nil) != enforcement.fetch(0, nil)
        errors << "self-test.yml: python-unit must run exactly one actions_cache_policy step immediately after checkout"
      else
        step = enforcement.fetch(0)
        exact_keys(step, %w[id name run], "self-test.yml: actions_cache_policy", errors)
        unless step["name"] == "Enforce bounded Actions cache authority" && step["run"] == guard_run
          errors << "self-test.yml: actions_cache_policy must invoke both exact guard and falsifier commands"
        end
      end
    end
  end

  jobs.each do |job_name, job|
    next unless job.is_a?(Hash)

    if job.key?("uses")
      reference = job["uses"]
      if file_name == workflow_name && protected_jobs.key?(job_name)
        errors << "#{file_name}: job #{job_name.inspect} must not delegate to a reusable workflow"
      elsif !reference.is_a?(String) || !reference.start_with?("./.github/workflows/")
        errors << "#{file_name}: job #{job_name.inspect} delegates to an unaudited remote reusable workflow"
      else
        target = File.expand_path(reference.delete_prefix("./"), repository_root)
        begin
          real_target = File.realpath(target)
        rescue Errno::ENOENT
          real_target = target
        end
        unless inside?(real_target, workflow_root) && File.file?(real_target) && !File.symlink?(target)
          errors << "#{file_name}: job #{job_name.inspect} local reusable workflow target is missing or escapes .github/workflows"
        end
      end
    end

    steps = job["steps"]
    next if steps.nil?
    unless steps.is_a?(Array)
      errors << "#{file_name}: job #{job_name.inspect} steps must be a YAML sequence"
      next
    end

    steps.each_with_index do |step, index|
      next unless step.is_a?(Hash)

      action = step["uses"]
      location = "#{file_name}: job #{job_name.inspect} step #{index + 1}"
      if action.is_a?(String)
        inspect_action.call(action, step, location, false)
        local_scan.call(action, location) if local_reference?(action)
      end

      protected = file_name == workflow_name && protected_jobs.key?(job_name)
      if protected && step.key?("continue-on-error")
        errors << "#{location}: protected release proof steps must not mask failure"
      end
      env_keys = step.fetch("env", {}).is_a?(Hash) ? step.fetch("env", {}).keys.map(&:to_s) : []
      redirected = env_keys & %w[HOME CARGO_HOME CARGO_TARGET_DIR]
      authorized_target = (
        file_name == workflow_name && job_name == "registry_smoke" &&
        ["Fetch complete cargo source graph", "Build without local patches"].include?(step["name"]) &&
        redirected == ["CARGO_TARGET_DIR"]
      )
      if protected && !redirected.empty? && !authorized_target
        errors << "#{location}: protected release proof step redirects Cargo or home paths: #{redirected.inspect}"
      end

      next unless action.is_a?(String) && action.downcase.start_with?("actions/cache")

      cache_paths = lines(step.dig("with", "path")) if step["with"].is_a?(Hash)
      key = step.dig("with", "key") if step["with"].is_a?(Hash)
      cache_inputs = step["with"].is_a?(Hash) ? step["with"].keys.map(&:to_s) : []
      if cache_inputs.sort != %w[key path]
        errors << (
          "#{location}: cache action inputs must be exactly path and key; " \
          "lookup-only, fail-on-cache-miss, restore prefixes, and semantic drift are forbidden"
        )
      end

      case action
      when restore_action
        counts[[file_name, job_name]][0] += 1
        exact_keys(step, %w[id name uses with], location, errors)
        errors << "#{location}: restore id must be cargo_source_cache" unless step["id"] == "cargo_source_cache"
        if steps.take(index).any? { |prior| prior.is_a?(Hash) && prior.key?("run") }
          errors << "#{location}: cache restore must precede every run step"
        end
        errors << "#{location}: restore key must be the bounded OS and architecture epoch #{restore_key}" unless key == restore_key
      when save_action
        counts[[file_name, job_name]][1] += 1
        exact_keys(step, %w[if name uses with], location, errors)
        errors << "#{location}: cache save must be restricted to a successful main-push fetch on a cache miss" unless step["if"] == save_condition
        errors << "#{location}: save key must come from the restore primary key" unless key == save_key
        unless steps.fetch(index - 1, nil).is_a?(Hash) && steps.fetch(index - 1)["id"] == "cargo_source_fetch"
          errors << "#{location}: cache save must immediately follow the authoritative Cargo source fetch"
        end
        unless steps.fetch(index + 1, nil).is_a?(Hash) && steps.fetch(index + 1)["name"] == "Build without local patches"
          errors << "#{location}: cache save must precede the authoritative registry build"
        end
      else
        errors << "#{location}: use exact pinned #{restore_action} or #{save_action}, not #{action}"
      end

      if key.to_s.match?(/hashFiles\(|github\.(?:sha|ref|head_ref|base_ref|run_id|run_number)|matrix\./)
        errors << "#{location}: cache keys must not expand per dependency hash, ref, SHA, run, or matrix"
      end
      if cache_paths != allowed_paths
        errors << "#{location}: cache paths must be exactly #{allowed_paths.inspect}; target output is forbidden"
      end
      if cache_paths&.any? { |path| path.split("/").include?("target") }
        errors << "#{location}: target output is forbidden in Actions caches"
      end
    end
  end
end

# Audit every declared action under the conventional local-action root, even if
# no current workflow invokes it yet. Invoked local actions outside this root
# are resolved and inspected by the workflow scan above.
if Dir.exist?(action_root)
  Dir[File.join(action_root, "**", "action.{yml,yaml}")].sort.each do |metadata|
    directory = File.dirname(metadata).delete_prefix("#{repository_root}/")
    local_scan.call("./#{directory}", "#{metadata.delete_prefix("#{repository_root}/")}")
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

  errors << "#{file_name}: job #{job_name.inspect} has an unexpected cache action"
end

Thread.current[:inspect_cache_action] = nil
unless errors.empty?
  warn("FAIL: GitHub Actions cache policy is not bounded:")
  errors.each { |error| warn("  - #{error}") }
  exit(1)
end

puts(
  "OK: reusable Cargo caches restore source-only state on every ref, share one OS/arch epoch, " \
  "and save one fail-closed fetch from a main push (4 restores, 1 save)."
)
