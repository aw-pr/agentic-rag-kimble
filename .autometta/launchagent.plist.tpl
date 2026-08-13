<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>{{LABEL}}</string>
  <key>ProgramArguments</key>
  <array>
    <string>{{AUTOMETTA_BIN}}</string>
    <string>tick</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>AbandonProcessGroup</key>
  <true/>
  <key>StartInterval</key>
  <integer>{{INTERVAL_SECONDS}}</integer>
  <key>WorkingDirectory</key>
  <string>{{REPO_PATH}}</string>
  <key>StandardOutPath</key>
  <string>{{LOG_DIR}}/launchagent.out.log</string>
  <key>StandardErrorPath</key>
  <string>{{LOG_DIR}}/launchagent.err.log</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>{{PATH}}</string>
  </dict>
</dict>
</plist>
