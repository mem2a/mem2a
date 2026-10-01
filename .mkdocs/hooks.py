"""Build the documentation site from the repository's own files.

The spec, its schemas and examples, the decision records and the community
files live where GitHub readers expect them. This hook adds them to the site
under friendlier paths, renders the JSON schemas and examples as pages, and
rewrites every relative link so that it works on both GitHub and the site:
a link to a file that has a page goes to the page, and a link to anything
else (code, a directory, a license) goes to the file on GitHub.
"""

from __future__ import annotations

import json
import posixpath
import re
from pathlib import Path
from typing import Any

from mkdocs.config.defaults import MkDocsConfig
from mkdocs.structure.files import File, Files
from mkdocs.structure.pages import Page


ROOT = Path(__file__).resolve().parents[1]
GITHUB = 'https://github.com/mem2a/mem2a'
SPEC = 'spec/v0.1'

# Repository file -> site page, for Markdown files outside docs/.
PAGES: dict[str, str] = {
    f'{SPEC}/mem2a.md': 'specification/index.md',
    f'{SPEC}/examples/README.md': 'specification/examples/index.md',
    'adrs/README.md': 'adrs/index.md',
    'docs/README.md': 'start-here.md',
    'python/README.md': 'guides/python.md',
    'python/ARCHITECTURE.md': 'guides/python-architecture.md',
    'examples/acme-quote/README.md': 'guides/acme-demo.md',
    'examples/sales-agent/README.md': 'guides/sales-agent.md',
    'examples/tool-gateway/README.md': 'guides/tool-gateway.md',
    'CONTRIBUTING.md': 'community/contributing.md',
    'GOVERNANCE.md': 'community/governance.md',
    'MAINTAINERS.md': 'community/maintainers.md',
    'IMPLEMENTATIONS.md': 'community/implementations.md',
    'CHANGELOG.md': 'community/changelog.md',
    'CODE_OF_CONDUCT.md': 'community/code-of-conduct.md',
    'SECURITY.md': 'community/security.md',
}
PAGES.update({f'adrs/{p.name}': f'adrs/{p.name}' for p in sorted((ROOT / 'adrs').glob('0*.md'))})

# Pages rendered from JSON.
EXAMPLES = {
    f'{SPEC}/examples/{p.name}': f'specification/examples/{p.stem}.md'
    for p in sorted((ROOT / SPEC / 'examples').glob('*.json'))
}
SCHEMAS = {
    f'{SPEC}/schemas/{p.name}': f'specification/schemas/{p.name.removesuffix(".schema.json")}.md'
    for p in sorted((ROOT / SPEC / 'schemas').glob('*.schema.json'))
}
SCHEMA_INDEX = 'specification/schemas/index.md'

# Link targets that aren't pages of their own.
TARGETS: dict[str, str] = {
    **PAGES,
    **EXAMPLES,
    **SCHEMAS,
    'README.md': 'index.md',
    SPEC: 'specification/index.md',
    f'{SPEC}/schemas': SCHEMA_INDEX,
    f'{SPEC}/examples': 'specification/examples/index.md',
    'adrs': 'adrs/index.md',
    'docs': 'start-here.md',
    'python': 'guides/python.md',
    'examples/acme-quote': 'guides/acme-demo.md',
    'examples/sales-agent': 'guides/sales-agent.md',
    'examples/tool-gateway': 'guides/tool-gateway.md',
}

_SOURCES: dict[str, str] = {}  # site page -> repository file


# ------------------------------------------------------------------ files
def on_files(files: Files, config: MkDocsConfig) -> Files:
    for file in files.documentation_pages():
        file.edit_uri = f'docs/{file.src_uri}'
        _SOURCES[file.src_uri] = f'docs/{file.src_uri}'

    def add(site: str, source: str, content: str) -> None:
        file = File.generated(config, site, content=content)
        file.edit_uri = source
        files.append(file)
        _SOURCES[site] = source

    for source, site in PAGES.items():
        if (ROOT / source).is_file():
            text = (ROOT / source).read_text()
            if source == f'{SPEC}/mem2a.md':  # the site shows its own table of contents
                text = re.sub(r'^## Contents\n.*?(?=^## )', '', text, count=1, flags=re.M | re.S)
            add(site, source, text)
    for source, site in EXAMPLES.items():
        add(site, source, _example_page(source))
    for source, site in SCHEMAS.items():
        add(site, source, _schema_page(source))
    add(SCHEMA_INDEX, f'{SPEC}/schemas', _schema_index())
    return files


# ------------------------------------------------------------------ links
_FENCE = re.compile(r'^(```|~~~).*?^\1[ \t]*$', re.M | re.S)
_LINK = re.compile(r'(\]\()([^)\s]+)((?:\s+"[^"]*")?\))')
_HTML = re.compile(r'((?:href|src)=")([^"]+)(")')


def on_page_markdown(markdown: str, page: Page, config: MkDocsConfig, files: Files) -> str:
    source = _SOURCES.get(page.file.src_uri)
    if source is None:
        return markdown
    site = page.file.src_uri

    def fix(match: re.Match[str]) -> str:
        return match.group(1) + _rewrite(match.group(2), source, site) + match.group(3)

    parts, last = [], 0
    for fence in _FENCE.finditer(markdown):  # leave code blocks alone
        parts.append(_HTML.sub(fix, _LINK.sub(fix, markdown[last : fence.start()])))
        parts.append(fence.group(0))
        last = fence.end()
    parts.append(_HTML.sub(fix, _LINK.sub(fix, markdown[last:])))
    return ''.join(parts)


def _rewrite(target: str, source: str, site: str) -> str:
    if re.match(r'^[a-z][a-z0-9+.-]*:', target) or target.startswith(('#', '/')):
        return target
    path, _, anchor = target.partition('#')
    if not path:
        return target
    resolved = posixpath.normpath(posixpath.join(posixpath.dirname(source), path))
    suffix = f'#{anchor}' if anchor else ''
    page = TARGETS.get(resolved.rstrip('/'))
    if page is None and resolved.startswith('docs/'):
        page = resolved.removeprefix('docs/') if (ROOT / resolved).is_file() else None
    if page is not None:
        return posixpath.relpath(page, posixpath.dirname(site) or '.') + suffix
    if (ROOT / resolved).is_dir():
        return f'{GITHUB}/tree/main/{resolved}'
    if (ROOT / resolved).exists():
        return f'{GITHUB}/blob/main/{resolved}{suffix}'
    return target  # let the strict build report it


# --------------------------------------------------------------- examples
def _json(value: Any) -> str:
    return '```json\n' + json.dumps(value, indent=2, ensure_ascii=False) + '\n```\n'


def _headers(headers: dict[str, str]) -> str:
    rows = ''.join(f'| `{k}` | `{v}` |\n' for k, v in headers.items())
    return '| Header | Value |\n| --- | --- |\n' + rows


def _request(request: dict[str, Any]) -> str:
    body = request.get('body', {})
    method = body.get('method') if isinstance(body, dict) else None
    head = f'`{request["method"]} {request["url"]}`'
    if method:
        head += f', JSON-RPC method `{method}`'
    return f'{head}\n\n{_headers(request.get("headers", {}))}\n{_json(body)}'


def _response(response: dict[str, Any]) -> str:
    return f'HTTP `{response["status"]}`\n\n{_headers(response.get("headers", {}))}\n{_json(response["body"])}'


def _example_page(source: str) -> str:
    data = json.loads((ROOT / source).read_text())
    name = posixpath.basename(source)
    out = [
        f'# {data["title"]}\n',
        f'{data["description"]}\n',
        f'Source: [`{name}`]({GITHUB}/blob/main/{source}). Non-normative; '
        f'[the conformance checks]({GITHUB}/tree/main/conformance) validate every Mem2A '
        f'payload in it against the [schemas]({posixpath.relpath(SCHEMA_INDEX, "specification/examples")}).\n',
    ]
    if 'agentCard' in data:
        out += ['## Agent Card\n', _json(data['agentCard'])]
    exchanges = data.get('exchanges') or ([data] if 'http' in data else [])
    for i, exchange in enumerate(exchanges, 1):
        http = exchange['http']
        label = f' {i}' if len(exchanges) > 1 else ''
        out += [f'## Request{label}\n', _request(http['request'])]
        out += [f'## Response{label}\n', _response(http['response'])]
    for i, push in enumerate(data.get('push', []), 1):
        out += [f'## Push notification {i}\n', _request(push)]
    if 'sse' in data:
        out += ['## Request\n', _request(data['sse']['request'])]
        for i, event in enumerate(data['sse']['events'], 1):
            out += [f'## Stream event {i}\n', _json(event)]
    return '\n'.join(out)


# ---------------------------------------------------------------- schemas
def _type(spec: dict[str, Any]) -> str:
    if '$ref' in spec:
        target = spec['$ref'].split('/')[-1]
        return f'`{target}`'
    if 'type' in spec:
        kind = spec['type']
        if kind == 'array' and isinstance(spec.get('items'), dict):
            return f'array of {_type(spec["items"])}'
        if 'enum' in spec:
            return ' \\| '.join(f'`{v}`' for v in spec['enum'])
        if 'const' in spec:
            return f'`{spec["const"]}`'
        return f'`{kind}`'
    if 'allOf' in spec:
        return ', '.join(_type(s) for s in spec['allOf'] if '$ref' in s or 'type' in s) or 'object'
    return 'any'


def _cell(text: str) -> str:
    return text.replace('|', '\\|').replace('\n', ' ')


_COMMON = json.loads((ROOT / SPEC / 'schemas' / 'common.schema.json').read_text())['$defs']


def _describe(spec: dict[str, Any]) -> str:
    if spec.get('description'):
        return str(spec['description'])
    ref = spec.get('$ref') or (spec.get('items') or {}).get('$ref', '')
    definition = _COMMON.get(ref.split('/')[-1], {}) if ref else {}
    return str(definition.get('description', ''))


def _properties(schema: dict[str, Any]) -> str:
    props = schema.get('properties', {})
    if not props:
        return ''
    required = set(schema.get('required', []))
    rows = [
        f'| `{name}` | {_type(spec)} | {"yes" if name in required else ""} | '
        f'{_cell(_describe(spec))} |'
        for name, spec in props.items()
    ]
    return '| Member | Type | Required | Description |\n| --- | --- | --- | --- |\n' + '\n'.join(rows) + '\n'


def _schema_page(source: str) -> str:
    schema = json.loads((ROOT / source).read_text())
    name = posixpath.basename(source)
    out = [f'# {schema.get("title", name)}\n', f'{schema.get("description", "")}\n']
    out.append(
        f'`$id`: `{schema.get("$id", "")}`. Source: [`{name}`]({GITHUB}/blob/main/{source}).\n'
    )
    if 'properties' in schema:
        out += ['## Members\n', _properties(schema)]
    for name_, definition in schema.get('$defs', {}).items():
        out.append(f'## `{name_}`\n')
        if definition.get('description'):
            out.append(f'{definition["description"]}\n')
        if 'properties' in definition:
            out.append(_properties(definition))
        else:
            out.append(f'Type: {_type(definition)}.\n')
    out += ['## Full schema\n', '??? example "Show the JSON Schema"\n\n' + _indent(_json(schema))]
    return '\n'.join(out)


def _indent(text: str) -> str:
    return ''.join(f'    {line}' if line.strip() else line for line in text.splitlines(True))


def _schema_index() -> str:
    rows = []
    for source, site in SCHEMAS.items():
        schema = json.loads((ROOT / source).read_text())
        page = posixpath.relpath(site, 'specification/schemas')
        first = schema.get('description', '').split('. ')[0].rstrip('.') + '.'
        rows.append(f'| [{schema.get("title", source)}]({page}) | `{posixpath.basename(source)}` | {_cell(first)} |')
    return (
        '# JSON Schemas\n\n'
        'The schemas define the syntax of every Mem2A payload, in JSON Schema draft 2020-12. '
        'They are normative for syntax; [the specification](../index.md) is normative for meaning '
        '([2.4](../index.md#2-conventions)). Validators must enforce the `format` keywords.\n\n'
        '| Schema | File | What it describes |\n| --- | --- | --- |\n' + '\n'.join(rows) + '\n\n'
        f'Download them from [`spec/v0.1/schemas`]({GITHUB}/tree/main/spec/v0.1/schemas). '
        'Their `$id`s use `https://w3id.org/mem2a/v0.1/schemas/`, which does not resolve yet '
        f'([#21]({GITHUB}/issues/21)): load `common.schema.json` into your validator yourself.\n'
    )
