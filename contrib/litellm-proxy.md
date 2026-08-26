# Keeping the litellm proxy up

Every worker in every repo routes its API traffic through `ANTHROPIC_BASE_URL`,
which defaults to `http://host.docker.internal:4000` — a local litellm proxy on
the host. `sandbox` refuses to dispatch if nothing is listening there, because
otherwise a run fails one bead at a time and reads as hard work rather than a
dead proxy.

Running it as a foreground job in a terminal is the fragile way. It was found
19 days into an uptime, held by a single shell, one accidental `exit` away from
taking the whole fleet down — and `zsh` only warns once.

## As a launchd agent

Write this to `~/Library/LaunchAgents/local.litellm.plist`, adjusting the three
paths for your machine:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>local.litellm</string>
  <key>ProgramArguments</key>
  <array>
    <string>/Users/YOU/miniconda/bin/uv</string>
    <string>run</string><string>litellm</string>
    <string>--config</string><string>litellm_config.yaml</string>
    <string>--host</string><string>127.0.0.1</string>
    <string>--port</string><string>4000</string>
    <string>--telemetry</string><string>False</string>
  </array>
  <key>WorkingDirectory</key><string>/path/to/the/config/dir</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>30</integer>
  <key>StandardOutPath</key><string>/Users/YOU/Library/Logs/litellm.log</string>
  <key>StandardErrorPath</key><string>/Users/YOU/Library/Logs/litellm.err.log</string>
</dict>
</plist>
```

Two things that will otherwise bite:

- **`uv` needs its absolute path.** launchd starts with a minimal `PATH` and
  will not find a `uv` that exists only via conda or a shell profile.
- **`WorkingDirectory` must be where the config actually lives**, which is not
  necessarily where you launched the proxy from. The `--config` argument is a
  relative path.

`KeepAlive` restarts it if it dies; `ThrottleInterval` makes a bad config a slow
loop in the log rather than a hot one.

## Installing

Stop the existing copy first or port 4000 will conflict:

```bash
kill <pid-of-the-running-proxy>
launchctl load ~/Library/LaunchAgents/local.litellm.plist
```

Check it came up the way `sandbox` will see it:

```bash
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:4000/health/liveliness
```

## Not using a proxy at all

Set `ANTHROPIC_BASE_URL = ""` in `[tool.sandbox.env]` to drop the flag and talk
to the real API. The preflight check skips itself when there is no local URL to
verify.
