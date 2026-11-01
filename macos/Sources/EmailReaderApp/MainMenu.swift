import AppKit

/// Build the application main menu.
///
/// Without an explicit Edit menu, the standard Cut/Copy/Paste/Select-All key
/// equivalents (Cmd+X/C/V/A) are never routed to the first responder — so they
/// don't work in WKWebView text fields. Wiring the standard selectors with a
/// nil target sends them down the responder chain to whatever is focused.
func buildMainMenu() -> NSMenu {
    let mainMenu = NSMenu()

    // App menu (Quit).
    let appItem = NSMenuItem()
    mainMenu.addItem(appItem)
    let appMenu = NSMenu()
    appItem.submenu = appMenu
    appMenu.addItem(
        withTitle: "Скрыть Niti", action: #selector(NSApplication.hide(_:)), keyEquivalent: "h"
    )
    appMenu.addItem(.separator())
    appMenu.addItem(withTitle: "Выйти", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")

    // Edit menu (the part that fixes copy/paste).
    let editItem = NSMenuItem()
    mainMenu.addItem(editItem)
    let editMenu = NSMenu(title: "Правка")
    editItem.submenu = editMenu
    editMenu.addItem(withTitle: "Отменить", action: Selector(("undo:")), keyEquivalent: "z")
    let redo = editMenu.addItem(withTitle: "Повторить", action: Selector(("redo:")), keyEquivalent: "z")
    redo.keyEquivalentModifierMask = [.command, .shift]
    editMenu.addItem(.separator())
    editMenu.addItem(withTitle: "Вырезать", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
    editMenu.addItem(withTitle: "Копировать", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
    editMenu.addItem(withTitle: "Вставить", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
    editMenu.addItem(
        withTitle: "Выбрать всё", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a"
    )

    return mainMenu
}
