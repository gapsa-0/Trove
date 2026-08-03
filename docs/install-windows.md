# Install Trove on Windows

Download the installer from the [releases page](https://github.com/gapsa-0/Trove/releases).
Trove supports 64-bit Windows 10 and 11.

## The SmartScreen warning

Trove is **not code-signed**. Windows SmartScreen therefore shows a blue
*"Windows protected your PC"* panel, where the only obvious button is *Don't run*.

Click **More info**, then **Run anyway**.

This warning appears for any application from an independent developer who has not
bought a code-signing certificate (a few hundred US dollars per year, renewed
annually). It reflects the absence of a purchased certificate, not an inspection that
found anything wrong with the file.

To confirm you have the file the build produced, compare its checksum against the
entry in `SHA256SUMS.txt` on the release page.

In **PowerShell** (Start menu → type "PowerShell"):

```powershell
Get-FileHash 'Trove.Setup.0.1.2.exe' -Algorithm SHA256
```

Or in **Command Prompt**, where `Get-FileHash` does not exist:

```
certutil -hashfile "Trove.Setup.0.1.2.exe" SHA256
```

Both print the hash in uppercase while `SHA256SUMS.txt` records it in lowercase.
Compare them ignoring case.

## Installing

The installer runs as your normal user and needs no administrator rights. It creates
a desktop shortcut and a Start-menu entry.

On first launch, choose the folder that contains your media. Trove catalogues it in
place and never moves, renames, edits, or deletes any file inside it.

The first time People or Pets detection runs, Trove downloads their model weights
(about 550 MB) once, then works offline. Everything else — including all media
processing — is local from the start.

## Uninstalling

Uninstall from *Settings → Apps* or the Start-menu entry. It removes the application
and its shortcuts, and never changes your media.

Your catalogue is deliberately left behind in `%LOCALAPPDATA%\organize_archive`, so a
reinstall resumes where you left off. Delete that folder yourself if you want the
catalogue gone — after taking any backup you want, since it holds all the naming and
review work you have done.
