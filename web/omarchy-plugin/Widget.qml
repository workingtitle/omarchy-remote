// Omarchy 4 bar widget for Omarchy Remote: shows whether a device controls this machine or waits
// for pairing approval, and opens pairing on click. Reads only the local service's loopback status
// endpoint (web/backend/server.mjs, /api/local/status). Installed by native/install.sh.
import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui

BarWidget {
  id: root
  moduleName: "omarchy-remote.status"

  property string state: "off"      // off | idle | active | pending
  property string transport: ""

  visible: state !== "off"
  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  Process {
    id: statusProc
    command: ["curl", "-fsS", "-m", "2", "-H", "Host: localhost:8099", "http://127.0.0.1:8099/api/local/status"]
    stdout: StdioCollector {
      onStreamFinished: {
        try {
          const status = JSON.parse(text)
          root.transport = status.transport || ""
          root.state = status.pending > 0 ? "pending" : status.busy ? "active" : "idle"
        } catch (error) {
          root.state = "off"
        }
      }
    }
    onExited: function(exitCode) { if (exitCode !== 0) root.state = "off" }
  }

  Timer {
    interval: 3000
    running: true
    repeat: true
    triggeredOnStart: true
    onTriggered: if (!statusProc.running) statusProc.running = true
  }

  BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: root.state === "active" ? "" : root.state === "pending" ? "" : ""
    slotSize: Style.bar.statusSlot
    fontSize: Style.font.caption
    tooltipText: root.state === "active" ? "Remote control active (" + root.transport + ")"
      : root.state === "pending" ? "A device is waiting for pairing approval"
      : "Omarchy Remote · click to pair a device"
    onPressed: Quickshell.execDetached(["omarchy-launch-floating-terminal-with-presentation", "omarchy-remote-pair"])
  }
}
