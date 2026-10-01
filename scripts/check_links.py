# SPDX-License-Identifier: Apache-2.0
"""Check every relative link in the repository's Markdown and YAML files.

    python scripts/check_links.py [--max-issue N]

A link must point at a file or directory that exists, and a link with an
anchor must point at a heading that exists, using GitHub's anchor rules.
Links to issue templates must name a template that exists. With
`--max-issue`, links to issues of this repository above N are reported too.
Exits 1 if anything is broken.
"""

from __future__ import annotations

import argparse
import re
import sys
import unicodedata
from functools import cache
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REPO = 'https://github.com/mem2a/mem2a'
SKIP = {
    '.git',
    '.venv',
    'venv',
    'node_modules',
    'site',
    '__pycache__',
    '.pytest_cache',
    '.mypy_cache',
    '.ruff_cache',
}
LINK = re.compile(
    r'(?<!!)\[[^\]]*\]\(([^)\s]+)(?:\s+"[^"]*")?\)'  # [text](target)
    r'|<(https?://[^>]+)>'  # <https://...>
    r'|(?<![(\w])(' + re.escape(REPO) + r'/[^\s)\]"\'>`]+)'  # bare URLs to this repo
)
FENCE = re.compile(r'```.*?```', re.S)


def files() -> list[Path]:
    return sorted(
        path
        for path in ROOT.rglob('*')
        if path.is_file()
        and path.suffix in {'.md', '.yml', '.yaml'}
        and not SKIP & set(path.relative_to(ROOT).parts)
    )


def slug(heading: str) -> str:
    """GitHub's anchor for a heading."""
    text = re.sub(r'`([^`]*)`', r'\1', heading)
    text = re.sub(r'\[([^\]]*)\]\([^)]*\)', r'\1', text).strip().lower()
    kept = (ch for ch in text if ch in ' -_' or unicodedata.category(ch)[0] in 'LN')
    return ''.join(kept).replace(' ', '-')


@cache
def anchors(path: Path) -> frozenset[str]:
    found: set[str] = set()
    counts: dict[str, int] = {}
    in_fence = False
    for line in path.read_text().splitlines():
        if re.match(r'^\s*(```|~~~)', line):
            in_fence = not in_fence
            continue
        heading = None if in_fence else re.match(r'^#{1,6}\s+(.*?)\s*#*\s*$', line)
        if heading:
            base = slug(heading.group(1))
            n = counts.get(base, 0)
            found.add(base if n == 0 else f'{base}-{n}')
            counts[base] = n + 1
    found.update(re.findall(r'<a (?:id|name)="([^"]+)"', path.read_text()))
    return frozenset(found)


def check(path: Path, max_issue: int | None) -> list[str]:
    text = path.read_text()
    if path.suffix == '.md':
        text = FENCE.sub('', text)
    problems = []
    templates = {p.name for p in (ROOT / '.github' / 'ISSUE_TEMPLATE').glob('*.yml')}
    where = path.relative_to(ROOT)
    for match in LINK.finditer(text):
        target = (match.group(1) or match.group(2) or match.group(3)).rstrip('.,;:')
        if target.startswith(REPO):
            rest = target[len(REPO) :]
            issue = re.match(r'/issues/(\d+)', rest)
            if issue and max_issue is not None and int(issue.group(1)) > max_issue:
                problems.append(f'{where}: no issue #{issue.group(1)} yet: {target}')
            template = re.search(r'template=([^&#]+)', rest)
            if template and template.group(1) not in templates:
                problems.append(f'{where}: no issue template {template.group(1)}')
            tree = re.match(r'/(?:blob|tree)/main/([^#?]+)', rest)
            if tree and not (ROOT / tree.group(1)).exists():
                problems.append(f'{where}: no {tree.group(1)} in the repository')
            continue
        if re.match(r'^[a-z][a-z0-9+.-]*:', target) or target.startswith('//'):
            continue  # another site
        file_part, _, anchor = target.partition('#')
        dest = (path.parent / file_part).resolve() if file_part else path
        if not dest.exists():
            problems.append(f'{where}: broken link {target}')
            continue
        page = dest / 'README.md' if dest.is_dir() else dest
        if anchor and page.suffix == '.md' and anchor not in anchors(page):
            problems.append(f'{where}: no heading for #{anchor} in {page.relative_to(ROOT)}')
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--max-issue', type=int, help='the highest issue number that exists')
    args = parser.parse_args()
    checked = files()
    problems = [problem for path in checked for problem in check(path, args.max_issue)]
    print('\n'.join(problems) if problems else f'All links in {len(checked)} files resolve.')
    return 1 if problems else 0


if __name__ == '__main__':
    sys.exit(main())
