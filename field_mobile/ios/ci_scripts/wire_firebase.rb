#!/usr/bin/env ruby
# frozen_string_literal: true

# CI-only: wire FCM push into the Runner target.
#
# GoogleService-Info.plist and the Push Notifications capability are
# per-deployment and kept out of the repo (white-label; see docs/FCM_SETUP.md).
# On Xcode Cloud, ci_post_clone.sh materializes GoogleService-Info.plist from
# the GOOGLE_SERVICE_INFO_PLIST_B64 secret and then runs this script to:
#   1. add GoogleService-Info.plist to the Runner target as a bundled resource
#   2. point the Runner target's build configs at Runner.entitlements
#      (which carries aps-environment) so the archive requests the push
#      entitlement and Xcode Cloud's managed signing provisions it.
# Both steps are idempotent.

require 'xcodeproj'

project_path = File.expand_path('../Runner.xcodeproj', __dir__)
project = Xcodeproj::Project.open(project_path)
target = project.targets.find { |t| t.name == 'Runner' }
abort 'Runner target not found' unless target

PLIST = 'GoogleService-Info.plist'

already_bundled = target.resources_build_phase.files_references.any? do |r|
  r.respond_to?(:display_name) && r.display_name == PLIST
end

if already_bundled
  puts "#{PLIST} already bundled in Runner"
else
  runner_group = project.main_group.find_subpath('Runner', true)
  ref = runner_group.files.find { |f| f.display_name == PLIST } ||
        runner_group.new_reference(PLIST)
  target.resources_build_phase.add_file_reference(ref, true)
  puts "Added #{PLIST} to Runner resources"
end

target.build_configurations.each do |config|
  config.build_settings['CODE_SIGN_ENTITLEMENTS'] = 'Runner/Runner.entitlements'
end
puts "Set CODE_SIGN_ENTITLEMENTS on #{target.build_configurations.size} Runner configs"

project.save
puts 'Firebase push wiring complete.'
