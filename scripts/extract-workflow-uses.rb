#!/usr/bin/env ruby
# frozen_string_literal: true

# Extract every semantic YAML mapping whose key resolves to "uses".
# Psych's syntax tree preserves duplicate keys and decodes quoted, tagged,
# explicit, and folded scalars. A small ordered anchor table also resolves
# scalar aliases used as mapping keys or values. Duplicate anchor names are
# rejected: YAML aliases bind to the most recent preceding definition, while
# a global last-definition-wins table can misreport an earlier mutable value
# as a later immutable one.

require "json"
require "psych"

abort("usage: extract-workflow-uses.rb WORKFLOW") unless ARGV.length == 1

begin
  stream = Psych.parse_file(ARGV.fetch(0))
rescue Psych::SyntaxError => e
  warn("workflow YAML parse failed: #{e.message}")
  exit(2)
end

if stream.nil? || stream.children.length != 1
  warn("workflow must contain exactly one YAML document")
  exit(2)
end

anchors = {}
anchor_nodes = {}

register_anchor = lambda do |node|
  next if node.is_a?(Psych::Nodes::Alias)

  anchor = node.respond_to?(:anchor) ? node.anchor : nil
  next unless anchor

  object_id = node.object_id
  if anchor_nodes.key?(anchor) && anchor_nodes.fetch(anchor) != object_id
    line = node.respond_to?(:start_line) ? node.start_line + 1 : "unknown"
    warn("line #{line}: duplicate YAML anchor #{anchor.inspect} is not allowed")
    exit(2)
  end
  anchor_nodes[anchor] = object_id
  anchors[anchor] = node.value if node.is_a?(Psych::Nodes::Scalar)
end

scalar_value = lambda do |node|
  register_anchor.call(node)
  case node
  when Psych::Nodes::Scalar
    node.value
  when Psych::Nodes::Alias
    unless anchors.key?(node.anchor)
      line = node.respond_to?(:start_line) ? node.start_line + 1 : "unknown"
      warn(
        "line #{line}: YAML alias #{node.anchor.inspect} does not resolve " \
        "to one preceding scalar anchor"
      )
      exit(2)
    end
    anchors.fetch(node.anchor)
  end
end

uses = []
walk = lambda do |node|
  register_anchor.call(node)
  if node.is_a?(Psych::Nodes::Mapping)
    node.children.each_slice(2) do |key, value|
      if scalar_value.call(key) == "uses"
        action = scalar_value.call(value)
        unless action.is_a?(String)
          line = key.respond_to?(:start_line) ? key.start_line + 1 : "unknown"
          warn("line #{line}: uses value must resolve to one scalar")
          exit(2)
        end
        uses << {
          "line" => key.start_line + 1,
          "value" => action
        }
      end
      walk.call(key)
      walk.call(value)
    end
    next
  end
  children = node.respond_to?(:children) ? node.children : nil
  children&.each { |child| walk.call(child) }
end
walk.call(stream)

puts(JSON.generate(uses))
