#!/usr/bin/env ruby
# frozen_string_literal: true

# Extract every semantic YAML mapping whose key resolves to "uses".
# Psych's syntax tree preserves duplicate keys and decodes quoted, tagged,
# explicit, and folded scalars. A small anchor table also resolves scalar
# aliases used as mapping keys or values.

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

collect_anchors = lambda do |node|
  if node.is_a?(Psych::Nodes::Scalar) && node.anchor
    anchors[node.anchor] = node.value
  end
  children = node.respond_to?(:children) ? node.children : nil
  children&.each { |child| collect_anchors.call(child) }
end
collect_anchors.call(stream)

scalar_value = lambda do |node|
  case node
  when Psych::Nodes::Scalar
    node.value
  when Psych::Nodes::Alias
    anchors[node.anchor]
  end
end

uses = []
walk = lambda do |node|
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
      walk.call(value)
    end
    next
  end
  children = node.respond_to?(:children) ? node.children : nil
  children&.each { |child| walk.call(child) }
end
walk.call(stream)

puts(JSON.generate(uses))
