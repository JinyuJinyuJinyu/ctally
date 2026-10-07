"""The touches that make CTally feel native on macOS, through PyObjC: no Dock icon, on every
Space and beside full-screen apps, and never taking focus from the terminal. Each is best
effort: without PyObjC the indicator still works, just with a Dock icon and on one Space."""
from __future__ import annotations


def _appkit():
    try:
        import AppKit
        return AppKit
    except Exception:
        return None


def _window(widget):
    """The NSWindow behind a Qt window: Qt's winId() on macOS is its NSView. Only under Qt's
    Cocoa platform, though; under any other (offscreen, in tests) it's no Objective-C object,
    and wrapping it would crash."""
    try:
        import ctypes

        import objc
        from PySide6.QtGui import QGuiApplication
        if QGuiApplication.platformName() != "cocoa":
            return None
        view = objc.objc_object(c_void_p=ctypes.c_void_p(int(widget.winId())))
        return view.window()
    except Exception:
        return None


def become_accessory() -> None:
    """An accessory, like a menu bar app: no Dock icon, no app menu, no place in Cmd-Tab."""
    appkit = _appkit()
    if appkit is None:
        return
    try:
        appkit.NSApplication.sharedApplication().setActivationPolicy_(
            appkit.NSApplicationActivationPolicyAccessory)
    except Exception:
        pass


def float_everywhere(widget) -> None:
    """Floats the indicator on every Space and over full-screen apps, keeps it there when the
    app is hidden, and lets a click land without pulling focus from the terminal. Qt may reset
    some of this when it shows the window again, so call it after every show()."""
    appkit, window = _appkit(), _window(widget)
    if appkit is None or window is None:
        return
    try:
        window.setCollectionBehavior_(
            appkit.NSWindowCollectionBehaviorCanJoinAllSpaces
            | appkit.NSWindowCollectionBehaviorStationary
            | appkit.NSWindowCollectionBehaviorFullScreenAuxiliary
            | appkit.NSWindowCollectionBehaviorIgnoresCycle)
        window.setLevel_(appkit.NSFloatingWindowLevel)
        window.setHidesOnDeactivate_(False)
        # Closing the settings window hides the app to hand focus back; the indicator stays put.
        window.setCanHide_(False)
        window.setHasShadow_(False)
        if window.isKindOfClass_(appkit.NSPanel):
            window.setStyleMask_(window.styleMask() | appkit.NSWindowStyleMaskNonactivatingPanel)
            window.setBecomesKeyOnlyIfNeeded_(True)
            # A panel made non-activating after it exists still tells the window server it
            # activates; this private call is how AppKit itself keeps the two in step.
            if window.respondsToSelector_(b"_setPreventsActivation:"):
                window._setPreventsActivation_(True)
    except Exception:
        pass


def follow_active_space(widget) -> None:
    """Opens the settings window on the Space I'm looking at, even beside a full-screen
    terminal, instead of yanking me back to the Space it was last on."""
    appkit, window = _appkit(), _window(widget)
    if appkit is None or window is None:
        return
    try:
        window.setCollectionBehavior_(appkit.NSWindowCollectionBehaviorMoveToActiveSpace
                                      | appkit.NSWindowCollectionBehaviorFullScreenAuxiliary)
    except Exception:
        pass


def activate() -> None:
    """An accessory app has to ask to come forward before its settings window can."""
    appkit = _appkit()
    if appkit is None:
        return
    try:
        app = appkit.NSApplication.sharedApplication()
        if app.isHidden():
            app.unhide_(None)
        app.activateIgnoringOtherApps_(True)
    except Exception:
        pass


def hide_app() -> None:
    """Hands the keyboard back to whatever I was using; the indicator ignores hiding."""
    appkit = _appkit()
    if appkit is None:
        return
    try:
        appkit.NSApplication.sharedApplication().hide_(None)
    except Exception:
        pass
