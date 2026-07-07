#!/usr/bin/env bash
# Double-click this file in Finder to set up this machine for one Meta ad account, working
# through Claude Cowork/Desktop. No terminal commands to type — just answer the questions that
# appear in this window.
#
# If macOS refuses to open it the first time ("cannot be opened because it is from an
# unidentified developer"): right-click (or Control-click) this file -> Open, then confirm once.
# That only happens for files downloaded from a browser — shouldn't come up if this was cloned
# with git, but macOS is occasionally inconsistent about it.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
if ./onboard_specialist.sh; then
  echo
  echo "Setup finished. Read the steps above, then you can close this window."
else
  echo
  echo "Setup stopped early (see the message above) — nothing was changed that needs undoing."
  echo "Fix whatever it mentioned and double-click this file again."
fi
echo
read -r -p "Press Enter to close this window..."
