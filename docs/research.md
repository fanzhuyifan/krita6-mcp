# References and acknowledgments

Background reading for the initial design, September 2026. Implementation decisions are documented in the [design](design.md); reproduced behavior and remaining limits are in the [validation record](validation.md).

## Community projects

These projects informed our exploration of Krita automation, MCP bridges, native painting, and canvas feedback:

- [nanayax3/krita-mcp](https://github.com/nanayax3/krita-mcp)
- [edithatogo/krita-cli](https://github.com/edithatogo/krita-cli)
- [SanSaSane/krita-mcp](https://github.com/SanSaSane/krita-mcp)
- [buttonscodes/painter](https://github.com/buttonscodes/painter)
- [dcc-mcp/dcc-mcp-krita](https://github.com/dcc-mcp/dcc-mcp-krita)
- [halby24/KritaMCP](https://github.com/halby24/KritaMCP)

Thanks to their authors for making their work available. No third-party implementation source was copied into this repository. Each project retains its own license.

## Krita and Qt references

- [Krita release notes](https://krita.org/en/release-notes/krita-5-3-release-notes/) and [PyQt6 migration announcement](https://krita.org/en/posts/2025/monthly-update-25/): context for targeting Krita 6 and its Qt6 runtime.
- [Krita 6.0.3 Node implementation](https://raw.githubusercontent.com/KDE/krita/v6.0.3/libs/libkis/Node.cpp): native painting methods and their use of active-view resources.
- [Document API](https://api.kde.org/legacy/krita/html/classDocument.html) and [View API](https://api.kde.org/legacy/krita/html/classView.html): document access, previews, persistence, and brush settings.
- [Qt threading documentation](https://doc.qt.io/qt-6/threads-qobject.html): GUI-thread ownership and cross-thread communication.

These references informed the bridge's explicit targets, GUI-thread dispatch, native painting, and completion checks. The [live probes](testing.md) establish behavior on the tested build.

## Local reference build

Initial source inspection used the installed Linux packages `krita 6.0.3-2` and `python-pyqt6 6.11.0-3`:

- `/usr/lib/krita-python-libs/krita/__init__.py`: Krita's Python bootstrap and matching Qt-binding import.
- `/usr/lib/krita-python-libs/PyKrita/krita.pyi`: Python signatures for the installed bindings.

Full runtime versions and test results are retained in the [validation record](validation.md).
