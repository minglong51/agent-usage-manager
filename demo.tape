# Generate the README GIF with charm's `vhs`:  vhs demo.tape  ->  demo.gif
# Install vhs:  brew install vhs   (or see https://github.com/charmbracelet/vhs)
#
# This records a terminal launching the tool, then you screen-capture the
# browser separately — OR point vhs at a headless browser. Simplest path:
# run the tool, open the page, and use a screen recorder for the web UI.
# This tape covers the terminal half (the "one line to run it" moment).

Output demo.gif
Set FontSize 18
Set Width 1100
Set Height 420
Set Theme "Dracula"
Set Padding 18

Type "uvx agent-usage-manager"
Sleep 600ms
Enter
Sleep 2.5s
Type "# open http://127.0.0.1:8765  →  see every agent's CPU/RAM/GPU, click kill"
Sleep 3s
