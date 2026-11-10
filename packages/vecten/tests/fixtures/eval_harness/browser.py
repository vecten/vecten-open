"""The person's browser for sign-ins: `browser.py <step>...`, where a step is `goto:<url>`,
`fill:<field name>=<value>`, `click:<button text>` or `show` (the page's URL and HTML). Prints
the URL and text of the page after the last step. Needs the `browser` dependency group's
Chromium; the harness CA is not in it, so certificate errors are ignored.
"""

import sys

KINDS = ("goto", "fill", "click", "show")


def parse(steps: list[str]) -> list[tuple[str, str]]:
    parsed = []
    for step in steps:
        kind, _, arg = step.partition(":")
        if kind not in KINDS:
            raise ValueError(f"unknown step {step!r}; steps are {', '.join(KINDS)}")
        parsed.append((kind, arg))
    return parsed


def main(argv: list[str]) -> int:
    from playwright.sync_api import sync_playwright

    steps = parse(argv)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        tab = browser.new_context(ignore_https_errors=True).new_page()
        for kind, arg in steps:
            if kind == "goto":
                tab.goto(arg)
            elif kind == "fill":
                name, _, value = arg.partition("=")
                tab.fill(f'[name="{name}"]', value)
            elif kind == "click":
                with tab.expect_navigation(wait_until="load", timeout=30000):
                    tab.get_by_role("button", name=arg).click()
            else:
                print(tab.url)
                print(tab.content())
        print("URL:", tab.url)
        print(tab.inner_text("body")[:2000])
        browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
