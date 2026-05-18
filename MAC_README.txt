RapidGet Mac Version
====================

This folder is prepared on Windows for macOS.

Important:
- A real macOS .app or .dmg must be built on a Mac.
- Windows cannot build a native Mac app bundle.

To run on Mac:
1. Copy this whole folder to the Mac.
2. Open Terminal in this folder.
3. Run:
   chmod +x Run_RapidGet_Mac.command build_mac_app.command
4. Double-click Run_RapidGet_Mac.command, or run it from Terminal.

To build RapidGet.app on Mac:
1. Open Terminal in this folder.
2. Run:
   chmod +x build_mac_app.command
   ./build_mac_app.command
3. The app will be created at:
   dist/RapidGet.app

Notes:
- Torrent support may require libtorrent separately on macOS.
- Browser extension files are included in this folder.
