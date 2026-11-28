# agsc-node: the docs as a knowledge node (optional)

This folder turns the Markdown files in `../docs/` into a static site plus machine-readable files
(`llms.txt`, `llms-full.txt`, a typed graph, and a passage file with a SHA-256 digest per passage),
using AgenticSystemCore (`agsc`, Apache-2.0). Nothing else in the repository uses it.

Build (Node.js 22.13 or later; the output goes to `www/`, which git ignores):

    ./build.sh

Rebuild the items after `docs/` changes: `./make-items.sh`, then commit `content/`.

Delete it: remove this folder. Nothing outside it refers to it.

The items are copies of the docs pages under the repository's Apache-2.0 licence. The text is the
same; only these changes were made: links between pages and to files outside `docs/` were
rewritten (`GLOSSARY.md` to `glossary.md`, `../x` to the file on GitHub), the anchors of three
`GLOSSARY.md#...` links were dropped because the glossary has no such headings, and three example
addresses (`jane@example.com`, `ana@example.com`, `sample@example.test`) were written
`jane[at]example.com` because the tool refuses e-mail addresses in a page. Two pages are left out, and
links to them go to the file on GitHub: `extensions-private-git.md`, whose install commands
(`git+https://...git@v1.2.0`) match that same check and must stay as they are; and `releasing.md`,
whose page is larger than the tool's limit of 100 KB for one HTML page.
`README.md` of `docs/` is the home page.

`site.base` is the placeholder `http://localhost/`; set the real address before publishing.
`.well-known/security.txt` points to the repository's private vulnerability reporting.
The tool is young and has very few users; please judge the result for yourselves.
