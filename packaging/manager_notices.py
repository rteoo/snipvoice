"""Write the third-party notices for the Tauri manager window.

Lists every Rust crate linked into ``snipvoice-manager`` (normal dependencies
of the binary for the target platform, from ``cargo metadata --locked``) and
every npm package bundled into its page, each with version, license, source
and the license files the package itself ships. Any license outside the
allow-list fails the build, as the clean FFmpeg gate does for its runtime.

Usage: python packaging/manager_notices.py --output build/manager-notices.txt
"""

import argparse
import json
import os
import re
import subprocess
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DESKTOP = os.path.join(ROOT, "desktop")
CARGO_DIR = os.path.join(DESKTOP, "src-tauri")
DEFAULT_TARGET = {"win32": "x86_64-pc-windows-msvc", "darwin": "aarch64-apple-darwin"}.get(sys.platform)
# Permissive licenses, plus MPL-2.0: file-level copyleft satisfied by naming each
# crate's source repository below. Anything else needs a human decision first.
ALLOWED = {
    "MIT", "MIT-0", "Apache-2.0", "BSD-2-Clause", "BSD-3-Clause", "ISC", "Zlib", "Unicode-3.0",
    "Unicode-DFS-2016", "MPL-2.0", "CC0-1.0", "0BSD", "Unlicense", "BSL-1.0",
}
# SPDX exceptions that only add permissions to an allowed license.
ALLOWED_EXCEPTIONS = {"LLVM-exception"}
LICENSE_FILE = re.compile(r"^(LICEN[CS]E|COPYING|NOTICE|COPYRIGHT|UNLICENSE)", re.IGNORECASE)
MAX_LICENSE_BYTES = 256 * 1024


class NoticeError(RuntimeError):
    pass


def license_allowed(expression):
    """Whether an SPDX expression (with legacy ``/`` for OR) can be satisfied by allowed licenses."""
    tokens = re.findall(r"\(|\)|[^\s()]+", expression.replace("/", " OR "))
    position = 0

    def parse_or():
        nonlocal position
        result = parse_and()
        while position < len(tokens) and tokens[position].upper() == "OR":
            position += 1
            right = parse_and()
            result = result or right
        return result

    def parse_and():
        nonlocal position
        result = parse_atom()
        while position < len(tokens) and tokens[position].upper() == "AND":
            position += 1
            right = parse_atom()
            result = result and right
        return result

    def parse_atom():
        nonlocal position
        if position >= len(tokens):
            raise NoticeError(f"incomplete license expression: {expression!r}")
        token = tokens[position]
        position += 1
        if token == "(":
            result = parse_or()
            if position >= len(tokens) or tokens[position] != ")":
                raise NoticeError(f"unbalanced license expression: {expression!r}")
            position += 1
            return result
        allowed = token in ALLOWED
        if position < len(tokens) and tokens[position].upper() == "WITH":
            if position + 1 >= len(tokens):
                raise NoticeError(f"incomplete license expression: {expression!r}")
            allowed = allowed and tokens[position + 1] in ALLOWED_EXCEPTIONS
            position += 2
        return allowed

    allowed = parse_or()
    if position != len(tokens):
        raise NoticeError(f"unparsed license expression: {expression!r}")
    return allowed


def license_texts(directory, declared_file=None):
    """The license files a package ships, read as text; declared ``license-file`` first."""
    names = []
    if declared_file:
        names.append(declared_file)
    try:
        names += sorted(name for name in os.listdir(directory)
                        if LICENSE_FILE.match(name) and name not in names)
    except OSError:
        return []
    texts = []
    for name in names:
        path = os.path.join(directory, name)
        if not os.path.isfile(path) or os.path.getsize(path) > MAX_LICENSE_BYTES:
            continue
        with open(path, encoding="utf-8", errors="replace") as handle:
            texts.append((os.path.basename(name), handle.read().strip()))
    return texts


def rust_packages(target):
    command = ["cargo", "metadata", "--format-version", "1", "--locked", "--filter-platform", target]
    result = subprocess.run(command, cwd=CARGO_DIR, capture_output=True, text=True, encoding="utf-8")
    if result.returncode != 0:
        raise NoticeError(f"cargo metadata failed: {result.stderr.strip()[:2000]}")
    metadata = json.loads(result.stdout)
    packages = {package["id"]: package for package in metadata["packages"]}
    nodes = {node["id"]: node for node in metadata["resolve"]["nodes"]}
    root = metadata["resolve"]["root"]
    # Normal dependencies only: build scripts and dev tools are not shipped.
    seen, stack = set(), [root]
    while stack:
        identifier = stack.pop()
        if identifier in seen:
            continue
        seen.add(identifier)
        for dependency in nodes[identifier]["deps"]:
            if any(kind["kind"] is None for kind in dependency["dep_kinds"]):
                stack.append(dependency["pkg"])
    seen.discard(root)
    entries = []
    for identifier in seen:
        package = packages[identifier]
        directory = os.path.dirname(package["manifest_path"])
        entries.append({
            "name": package["name"], "version": package["version"],
            "license": package.get("license") or "",
            "source": package.get("repository") or package.get("homepage") or f"https://crates.io/crates/{package['name']}",
            "texts": license_texts(directory, package.get("license_file")),
        })
    return entries


def npm_packages():
    """Runtime npm dependencies only; build tools (Vite, TypeScript) are not bundled."""
    with open(os.path.join(DESKTOP, "package.json"), encoding="utf-8") as handle:
        manifest = json.load(handle)
    entries = []
    for name in sorted(manifest.get("dependencies", {})):
        directory = os.path.join(DESKTOP, "node_modules", *name.split("/"))
        package_file = os.path.join(directory, "package.json")
        if not os.path.isfile(package_file):
            raise NoticeError(f"{name} is not installed; run npm ci in desktop/ first")
        with open(package_file, encoding="utf-8") as handle:
            package = json.load(handle)
        repository = package.get("repository")
        source = repository.get("url") if isinstance(repository, dict) else repository
        entries.append({
            "name": name, "version": package.get("version", ""), "license": package.get("license") or "",
            "source": source or f"https://www.npmjs.com/package/{name}",
            "texts": license_texts(directory),
        })
    return entries


def render(rust, npm):
    lines = [
        "Snipvoice manager window: third-party notices",
        "",
        "The manager window (snipvoice-manager) is built with Tauri and renders with the",
        "operating system's web view (Microsoft Edge WebView2 on Windows), which is part of",
        "the system and not distributed with Snipvoice. It links the Rust crates and",
        "bundles the npm packages below. Each entry names its license and source; the",
        "license files the package ships follow it.",
        "",
    ]
    for title, entries in (("Rust crates", rust), ("npm packages", npm)):
        lines += [f"== {title} ({len(entries)}) ==", ""]
        for entry in sorted(entries, key=lambda item: (item["name"].lower(), item["version"])):
            lines += [f"--- {entry['name']} {entry['version']}", f"License: {entry['license']}",
                      f"Source: {entry['source']}", ""]
            for name, text in entry["texts"]:
                lines += [f"[{name}]", text, ""]
            if not entry["texts"]:
                lines += ["(The published package ships no license file; its source repository above holds it.)", ""]
    return "\n".join(lines) + "\n"


def check(entries):
    refused = [f"{entry['name']} {entry['version']}: {entry['license'] or 'no license declared'}"
               for entry in entries if not entry["license"] or not license_allowed(entry["license"])]
    if refused:
        raise NoticeError("licenses outside the allow-list:\n  " + "\n  ".join(refused))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--output", required=True)
    parser.add_argument("--target", default=DEFAULT_TARGET)
    args = parser.parse_args(argv)
    if not args.target:
        parser.error("--target is required on this platform")
    try:
        rust, npm = rust_packages(args.target), npm_packages()
        check(rust + npm)
    except NoticeError as error:
        print(f"manager notices: {error}", file=sys.stderr)
        return 1
    os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)
    temporary = args.output + ".tmp"
    with open(temporary, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(render(rust, npm))
    os.replace(temporary, args.output)
    print(f"manager notices: {len(rust)} crates, {len(npm)} npm packages -> {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
