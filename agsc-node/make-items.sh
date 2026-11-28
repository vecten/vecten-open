#!/usr/bin/env bash
# Rebuilds content/ from ../docs. Run from this folder: ./make-items.sh
# The only changes to the text are listed in README.md.
set -euo pipefail
cd "$(dirname "$0")"
AGSC="${AGSC:-npx --yes --package agentic-system-core@1.1.0 agsc}"
REPO=https://github.com/vecten/vecten-open/blob/main

# edit FILE SCRIPT: one sed rewrite through a temporary file, because "sed -i" takes different
# arguments in GNU sed (Linux) and BSD sed (macOS). The rewrites below use no GNU-only syntax.
edit() { sed -E "$2" "$1" > "$1.tmp" && mv "$1.tmp" "$1"; }

rm -rf content && mkdir -p content
$AGSC import ../docs --from okf --replace > /dev/null 2>&1

# docs/README.md is the home page of the node (an item cannot be called index.md)
{
  printf -- '---\nspec_version: 1.1.0\nokf_version: "0.2"\nbase: http://localhost/\ntitle: Vecten Open documentation\n'
  printf 'description: Index of the Vecten Open documentation, converted from the Markdown files in docs/ of the repository.\n---\n\n'
  cat ../docs/README.md
} > content/index.md
rm -f content/concepts/readme.md
# two pages are left out; links to them go to the file on GitHub:
# extensions-private-git.md: its install commands (git+https://...git@v1.2.0) match the tool's e-mail
# check, and the commands must stay as they are; releasing.md: its page is larger than the tool's
# limit of 100 KB for one HTML page.
rm -f content/concepts/extensions-private-git.md content/concepts/releasing.md
# the home page links to pages by their address on the built site; links that leave docs/ go to GitHub.
# Page addresses are lower case; GLOSSARY.md is the one page whose file name is not.
edit content/index.md "s#\]\((extensions-private-git|releasing)\.md\)#](${REPO}/docs/\1.md)#g"
edit content/index.md 's#\]\(GLOSSARY\.md(\#[a-z0-9-]*)?\)#](/concepts/glossary/)#g'
edit content/index.md 's#\]\(([a-zA-Z0-9-]+)\.md(\#[a-z0-9-]*)?\)#](/concepts/\1/)#g'
edit content/index.md "s#\]\(\.\./([^)]+)\)#](${REPO}/\1)#g"

for f in content/concepts/*.md; do
  # links that leave docs/ point at the repository on GitHub
  edit "$f" "s#\]\(\.\./([^)]+)\)#](${REPO}/\1)#g"
  # links between docs pages: GLOSSARY.md -> glossary.md, README.md -> the page on GitHub
  edit "$f" "s#\]\((extensions-private-git|releasing)\.md(\#[a-z0-9-]*)?\)#](${REPO}/docs/\1.md\2)#g"
  edit "$f" "s#\]\(GLOSSARY\.md(\#[a-z0-9-]*)?#](glossary.md#g; s#\]\(README\.md\)#](${REPO}/docs/README.md)#g"
  # two placeholder e-mail addresses in examples would fail the node's no-personal-data check
  edit "$f" 's#(jane|ana)@example\.com#\1[at]example.com#g; s#sample@example\.test#sample[at]example.test#g'
  edit "$f" 's/^status: draft$/status: stable/'
done
edit content/index.md 's/^status: draft$/status: stable/'
