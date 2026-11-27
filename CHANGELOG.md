# Changelog

Newest first.

## Unreleased

- Fix: the radio link no longer drops when a message contains the byte `7c`. The bridge and `jvc-probe`
  now escape it. Before, while Bluetooth audio played (the radio sends about one message a second), the
  link dropped every couple of minutes with "Connection reset by peer".
  After merging: on the Odroid, update the checkout and run `jvc2mqtt-install`, which updates the code
  and restarts the bridge.
- CLAUDE.md spells out the coworker workflow (branch + PR, Adam merges, Home Assistant changes arrive as files).
  After merging: nothing.
- No Claude Code `.claude/settings.json` in this public repo. CLAUDE.md now tells Claude to ask, in each session that needs to push, whether to add the git-push rule. Every PR now carries a changelog entry (see CLAUDE.md).
  After merging: nothing.
