# tiptoi-linux

An unofficial manager for the Ravensburger **tiptoi** audio pen on Linux.

Ravensburger's official "tiptoi Manager" only runs on Windows and macOS. tiptoi-linux gives you
the same everyday functions on Linux, both as a desktop app and on the command line:

- Browse and search the live Ravensburger product catalog
- Download `.gme` title files (with resume support)
- Copy titles onto the pen and verify them on the device afterwards
- See which installed titles have a newer version
- Delete titles and empty the pen's trash so the space is actually freed
- Mount and unmount the pen safely

> tiptoi-linux is not affiliated with or endorsed by Ravensburger. "tiptoi" is a trademark of
> Ravensburger AG.

## Installation

### AppImage (recommended)

Download `tiptoi-<version>-x86_64.AppImage` from the
[Releases](https://github.com/tokajer/tiptoi-linux/releases) page, then:

```sh
sha256sum -c SHA256SUMS          # optional: verify the download
chmod +x tiptoi-*-x86_64.AppImage
./tiptoi-*-x86_64.AppImage
```

The AppImage bundles Python and Qt, so it needs nothing else installed. Each release is
smoke-tested on Ubuntu 22.04 and Debian 12.

### From source

Requires Python 3.11 or newer.

```sh
git clone https://github.com/tokajer/tiptoi-linux.git
cd tiptoi-linux
pip install .          # command line only, no dependencies
pip install '.[gui]'   # also installs PySide6 for the desktop app
```

This installs two commands: `tiptoi` (command line) and `tiptoi-gui` (desktop app).

### System requirements

- `findmnt` (util-linux) to detect the pen
- `udisksctl` (udisks2) to mount and unmount it. Without it, you can mount the pen in your file
  manager instead.

The pen is detected by its filesystem label `tiptoi`.

## Usage

### Desktop app

Start the AppImage or run `tiptoi-gui`. The interface follows your system language. English
and German are included.

### Command line

```sh
tiptoi update                  # refresh the product catalog
tiptoi list                    # list all products
tiptoi search "bauernhof"      # search by name (case-insensitive)
tiptoi download "<name>"       # download a title into the local cache

tiptoi pen status              # mountpoint, free space, installed titles
tiptoi pen mount
tiptoi pen install "<name>"    # download if needed, copy to the pen, verify
tiptoi pen install "<name>" --dry-run
tiptoi pen outdated            # installed titles with a newer catalog version
tiptoi pen delete WN.gme       # delete by on-pen file name (asks for confirmation)
tiptoi pen unmount             # safe to unplug afterwards
```

Product names must match exactly as shown by `tiptoi list`. Use quotes around names that
contain spaces. Add `--pen /path/to/mountpoint` after `pen` to skip auto-detection.

### Where files are stored

The catalog and downloaded titles are cached in `$XDG_CACHE_HOME/tiptoi-linux/`, which is
`~/.cache/tiptoi-linux/` by default. The catalog is refreshed automatically once a day.

## Development

Run the tests:

```sh
pip install PySide6-Essentials
QT_QPA_PLATFORM=offscreen python -m unittest discover -s tests -t .
```

Translations are in `tiptoi_linux/locale/`. After editing a `.po` file, recompile it:

```sh
msgfmt -o tiptoi_linux/locale/de/LC_MESSAGES/tiptoi.mo tiptoi_linux/locale/de/LC_MESSAGES/tiptoi.po
```

Build the AppImage (needs ImageMagick or Inkscape to render the icon), then smoke-test it in
containers (needs podman):

```sh
bash packaging/build-appimage.sh
bash packaging/smoke-test.sh
```

### Releasing

1. Update the version in both `pyproject.toml` and `tiptoi_linux/__init__.py`.
2. On GitHub, go to **Actions → Release → Run workflow**.

The workflow runs the tests, builds and smoke-tests the AppImage, and publishes a GitHub
release with the AppImage and a `SHA256SUMS` file.

## License

tiptoi-linux is free software: you can redistribute it and/or modify it under the terms of the
GNU General Public License as published by the Free Software Foundation, either version 3 of the
License, or (at your option) any later version. See [LICENSE](LICENSE) for the full text.

## If you like my work you can

[![Buy me a coffee](https://img.buymeacoffee.com/button-api/?text=Buy%20me%20a%20coffee&emoji=☕&slug=tokajer&button_colour=1e4c7a&font_colour=ffffff&font_family=Inter&outline_colour=ffffff&coffee_colour=FFDD00)](https://www.buymeacoffee.com/tokajer)