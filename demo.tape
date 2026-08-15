# Generate the README GIF with charm's `vhs`:  vhs demo.tape  ->  docs/demo.gif
# Install vhs:  brew install vhs   (or see https://github.com/charmbracelet/vhs)
#
# This records the terminal half (the "one line to run it" moment). The web UI
# half is a separate screen capture — see the launch kit.

Output docs/demo.gif
Set FontSize 18
Set Width 1100
Set Height 480
Set Theme "Dracula"
Set Padding 18

# --port/--no-browser are recording-only: the dogfood instance already owns
# 8765 on this machine, and a render shouldn't pop a browser window.
Type "uvx agent-usage-manager --port 51534 --no-browser"
Sleep 600ms
Enter
Sleep 4.5s
Ctrl+C
Sleep 1s
Type "# that's it — one command, dashboard live: every agent's CPU/RAM/GPU + a guarded kill switch"
Sleep 2.5s
