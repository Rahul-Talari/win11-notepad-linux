# Notepad

A Linux text editor modelled on the Windows 11 Notepad, written in Python with
GTK 4 and libadwaita.

## Quick start

Installing takes three steps. `install.sh` does **not** install the required
packages, so do step 1 first. Without it, Notepad installs but won't open.

1. **Install the dependencies** for your distribution (Ubuntu shown; see
   [Requirements](#requirements) for Fedora, Arch and openSUSE):

   ```
   sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 git
   ```

2. **Get the code:**

   ```
   git clone <repository-url> ~/notepad
   ```

3. **Install:**

   ```
   cd ~/notepad
   ./install.sh
   ```

Then open **Notepad** from the apps menu, or run `notepad`.

## Features

- Tabs (drag a tab out to make a new window), unsaved-changes dot, untitled
  tabs named after their first line
- Session restore: unsaved notes come back after closing the app
- Markdown formatting: headings, bold, italic, strikethrough, lists, links,
  code, quotes. Syntax characters are hidden except on the line being edited.
  Toolbar, Ctrl+B / Ctrl+I / Ctrl+K, and a "Markdown syntax" toggle
- Reads and writes Windows 11 Notepad Markdown files the same way Windows
  does: one blank line per paragraph break, `&#x20;` and `\` escapes
- Find & replace, Go to line, Time/Date (F5), Search with Bing
- Zoom, word wrap, status bar (line, column, characters, zoom, line ending,
  encoding)
- UTF-8, UTF-8 with BOM, UTF-16 LE/BE and ANSI; CRLF, LF and CR line endings
- Print and page setup, recent files, light/dark theme, font choice

## Requirements

- Linux with Python 3.10+
- GTK 4.12 or newer and libadwaita 1.5 or newer, with their Python bindings

Ubuntu 24.04, Fedora 40, and newer releases of either are recent enough.
Older releases (e.g. Ubuntu 22.04) ship versions that are too old.

Install the dependencies for your distribution. Many GNOME desktops already
have them, in which case the command just reports they are installed.

| Distribution            | Command                                                         |
|-------------------------|-----------------------------------------------------------------|
| Ubuntu / Debian / Mint  | `sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 git`   |
| Fedora                  | `sudo dnf install python3-gobject gtk4 libadwaita git`          |
| Arch / Manjaro          | `sudo pacman -S python-gobject gtk4 libadwaita git`             |
| openSUSE                | `sudo zypper install python3-gobject-Gdk typelib-1_0-Gtk-4_0 typelib-1_0-Adw-1 git` |

To check that everything is in place:

```
python3 -c "import gi; gi.require_version('Gtk','4.0'); gi.require_version('Adw','1'); from gi.repository import Gtk, Adw; print(f'OK: GTK 4.{Gtk.get_minor_version()}, libadwaita 1.{Adw.get_minor_version()}')"
```

## Install

```
git clone <repository-url> ~/notepad
cd ~/notepad
./install.sh
```

This adds **Notepad** to the apps menu and a `notepad` command in
`~/.local/bin`. It installs for your user only and needs no root access.

The launcher points at the folder you cloned into, so keep that folder where
it is. If you move it, run `./install.sh` again from the new location.

If the `notepad` command is not found, log out and back in so that
`~/.local/bin` is added to your `PATH`. The apps-menu entry works either way.

To make Notepad the default app for text and Markdown files:

```
xdg-mime default io.local.Notepad.desktop text/plain text/markdown
```

## Update

```
cd ~/notepad
git pull
```

Close Notepad and open it again to use the new version.

## Uninstall

```
rm ~/.local/bin/notepad ~/.local/share/applications/io.local.Notepad.desktop
rm -rf ~/notepad
```

To also remove your settings and saved session (including unsaved notes):

```
rm -rf ~/.config/notepad-app ~/.local/share/notepad-app
```

## Run

```
notepad [FILE...]
```

If Notepad is already open, files are added to the existing window as new tabs.

## Tests

```
python3 tests/test_markdown.py   # text logic, no display needed
python3 tests/test_gui.py        # real editing in the app on a hidden display
```

`test_gui.py` needs `gtk4-broadwayd` (Ubuntu package `libgtk-4-bin`) and uses
throwaway settings, so your own Notepad session is not touched.

## Where things are stored

- Settings: `~/.config/notepad-app/settings.json`
- Session (open tabs, unsaved text): `~/.local/share/notepad-app/session.json`
