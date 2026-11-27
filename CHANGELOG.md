# Changelog

Newest first.

## Unreleased

- `BT_ADAPTER` can now be the adapter's address (`AA:BB:CC:DD:EE:FF`) as well as a name like `hci0`.
  Linux can renumber adapters after a replug, a USB drop or a reboot; an address keeps pointing at the
  same one. The installer and `jvc2mqtt-check` also find the adapter on kernels where they wrongly
  reported none.
  After merging: on the Odroid, update the checkout and run `jvc2mqtt-install`. With two adapters
  plugged in, set `BT_ADAPTER` to the address of the one paired with the radio.
- Fix: the bridge now really uses `BT_ADAPTER`. It read the adapter's address from a file some kernels
  (including the Odroid's) don't have, then used whichever adapter Linux picked; with a second adapter
  plugged in, the controls went through the wrong one. If the adapter is missing it now says so and waits.
- Fix: the radio's media player is registered again after the adapter disappears and comes back
  (replug, driver reload), so the display and buttons keep working.
- Fix: `radio connected` and the Connected sensor only come on when the radio answers. A link it ignores
  is closed and opened again.
  After merging: on the Odroid, update the checkout and run `jvc2mqtt-install`.
- Fix: the radio link no longer drops when a message contains the byte `7c`. The bridge and `jvc-probe`
  now escape it. Before, while Bluetooth audio played (the radio sends about one message a second), the
  link dropped every couple of minutes with "Connection reset by peer".
  After merging: on the Odroid, update the checkout and run `jvc2mqtt-install`, which updates the code
  and restarts the bridge.
- CLAUDE.md spells out the coworker workflow (branch + PR, Adam merges, Home Assistant changes arrive as files).
  After merging: nothing.
- No Claude Code `.claude/settings.json` in this public repo. CLAUDE.md now tells Claude to ask, in each session that needs to push, whether to add the git-push rule. Every PR now carries a changelog entry (see CLAUDE.md).
  After merging: nothing.
