"""The Windows installer script stays in step with the app it installs.

CI builds the installer on Windows and installs, starts and uninstalls it
(scripts/build_app.py); this catches the script drifting from the code.
"""

from __future__ import annotations

from pathlib import Path

from open_transfer import background

ISS = (Path(__file__).parents[1] / "packaging" / "windows-installer.iss").read_text()


def test_installs_the_desktop_app_for_this_user() -> None:
    assert 'Source: "..\\dist\\Open Transfer.exe"' in ISS  # what build_app.py --desktop makes
    assert "PrivilegesRequired=lowest" in ISS  # no admin prompt; updates replace it in place
    assert r"DefaultDirName={localappdata}\Programs\Open Transfer" in ISS
    assert "OutputBaseFilename=open-transfer-windows-x64-setup" in ISS


def test_uninstalling_stops_start_at_login() -> None:
    # The app's own Run entry would point at a deleted .exe.
    assert f"'{background.RUN_KEY}'" in ISS
    assert f"'{background.RUN_VALUE}'" in ISS


def test_uninstalling_keeps_pairings_and_files() -> None:
    # They live in the received-files folder, not the install folder.
    assert "[UninstallDelete]" not in ISS
