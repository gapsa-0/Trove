# Install Archive on Windows

Download the NSIS installer from the approved release page. Confirm its SHA-256
against `SHA256SUMS.txt`; Windows should show the recorded Archive publisher in
the signature details. Install as your normal user, then choose the folder that
contains your media. Archive only catalogues it in place.

Uninstall removes the application and shortcuts. It never changes source media.
Catalogue data is retained in `%LOCALAPPDATA%\organize_archive` unless you remove
that folder yourself after making any backup you need.
