# Published session data

`spa/` is the post-race review of one real race, exported by `scripts/export_session.py`
for the review's static mode. It is the REST payloads the review reads: lap list, race
summary, the recorded race journal (race events, agent runs, radio, race-state
snapshots), lap traces, the track map, and the deterministic Layer-1 lap findings.
Nothing in it is generated.

- Telemetry: Audi R8 LMS EVO II GT3, Circuit de Spa-Francorchamps (GP Pit), official
  iRacing race, subsession 65487918. The `.ibt` file comes from
  [SVappsLAB/iRacingTelemetrySDK](https://github.com/SVappsLAB/iRacingTelemetrySDK)
  (`Sdk/tests/SmokeTests/data/ibt/audir8lmsevo2gt3_spa up.ibt`, commit 9ae151b8), the
  repository author's own race, published under the
  [Apache License 2.0](https://www.apache.org/licenses/LICENSE-2.0). The data here is
  derived from that file (resampled channels, lap times, findings) and is redistributed
  under the same license with this attribution. No other driver's name or id is included.
- Pitwall run: `20261031T043329Z-01-replay`, the file replayed at 2x through RTV's
  race-state engine with all four agents on Kimi (`kimi-k3`), recorded on 2026-10-31.

Refresh it with the `publish-pages` skill (`.claude/skills/publish-pages/SKILL.md`).
