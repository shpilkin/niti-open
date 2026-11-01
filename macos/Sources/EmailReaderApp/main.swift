import AppKit

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
// Menu bar with a standard Edit menu so Cmd+C/V/X/A work in web text fields.
app.mainMenu = buildMainMenu()
// Start as a regular app (dock icon + window); the menubar item is always present.
app.setActivationPolicy(.regular)
app.run()
