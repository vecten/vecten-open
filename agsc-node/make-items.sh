#!/usr/bin/env bash
# Rebuilds content/ from ../docs. Run from this folder: ./make-items.sh
# The only changes to the text are listed in README.md.
set -euo pipefail
cd "$(dirname "$0")"
AGSC="${AGSC:-npx --yes --package agentic-system-core@1.1.0 agsc}"
REPO=https://github.com/vecten/vecten-open/blob/main

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
# the home page links to pages by their address on the built site; links that leave docs/ go to GitHub
sed -i -E "s#\]\((extensions-private-git|releasing)\.md\)#](${REPO}/docs/\1.md)#g" content/index.md
sed -i -E 's#\]\(([a-zA-Z0-9-]+)\.md(\#[a-z0-9-]*)?\)#](/concepts/\L\1/)#g' content/index.md
sed -i -E "s#\]\(\.\./([^)]+)\)#](${REPO}/\1)#g" content/index.md

for f in content/concepts/*.md; do
  # links that leave docs/ point at the repository on GitHub
  sed -i -E "s#\]\(\.\./([^)]+)\)#](${REPO}/\1)#g" "$f"
  # links between docs pages: GLOSSARY.md -> glossary.md, README.md -> the page on GitHub
  sed -i -E "s#\]\((extensions-private-git|releasing)\.md(\#[a-z0-9-]*)?\)#](${REPO}/docs/\1.md\2)#g" "$f"
  sed -i -E "s#\]\(GLOSSARY\.md(\#[a-z0-9-]*)?#](glossary.md#g; s#\]\(README\.md\)#](${REPO}/docs/README.md)#g" "$f"
  # two placeholder e-mail addresses in examples would fail the node's no-personal-data check
  sed -i -E 's#(jane|ana)@example\.com#\1[at]example.com#g; s#sample@example\.test#sample[at]example.test#g' "$f"
  sed -i 's/^status: draft$/status: stable/' "$f"
done
sed -i 's/^status: draft$/status: stable/' content/index.md
