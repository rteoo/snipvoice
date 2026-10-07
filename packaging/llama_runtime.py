"""Build the pinned wrapper/native pair; never install an upstream binary wheel."""

import argparse
import base64
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile


WRAPPER_VERSION = "0.3.36"
WRAPPER_REVISION = "1652066e0af45f2313b339670ef9555e8a54e545"
NATIVE_REVISION = "4fbc76dec51d0add466f0210855c0596589b60d4"
SOURCES = {
    "wrapper": (f"https://codeload.github.com/abetlen/llama-cpp-python/tar.gz/{WRAPPER_REVISION}",
                "232bf52f8219c82271de139a9134863fcd34b58d0c8c2c5da571225e1159b987"),
    "native": (f"https://codeload.github.com/ggml-org/llama.cpp/tar.gz/{NATIVE_REVISION}",
               "4ea37c28e9d6a74e6ffb34ff33a6555b33f0398e75814f0e2f362407d79ecb36"),
}
CMAKE_ARGS = "-DGGML_CUDA=OFF -DLLAMA_CURL=OFF -DGGML_NATIVE=OFF -DGGML_OPENMP=OFF"


def acquire(url, destination, digest):
    if not destination.exists():
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as output:
                temporary = Path(output.name)
                with urllib.request.urlopen(url, timeout=60) as response:
                    # ceiling: 128 MiB per public source archive; revisit if
                    # pinned sources legitimately grow beyond this size.
                    total = 0
                    while block := response.read(1024 * 1024):
                        total += len(block)
                        if total > 128 * 1024 * 1024:
                            raise RuntimeError("Runtime source archive exceeds the size limit")
                        output.write(block)
            with temporary.open("rb") as handle:
                if hashlib.file_digest(handle, "sha256").hexdigest() != digest:
                    raise RuntimeError("Runtime source archive hash mismatch")
            os.replace(temporary, destination)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    with destination.open("rb") as handle:
        if hashlib.file_digest(handle, "sha256").hexdigest() != digest:
            raise RuntimeError("Runtime source archive hash mismatch")


def extract(archive, destination):
    with tarfile.open(archive) as source:
        roots = {Path(item.name).parts[0] for item in source.getmembers() if item.name}
        if len(roots) != 1:
            raise RuntimeError("Invalid runtime source archive layout")
        source.extractall(destination, filter="data")
    return destination / roots.pop()


def build_environment():
    environment = os.environ.copy()
    environment["CMAKE_ARGS"] = CMAKE_ARGS
    environment["CMAKE_BUILD_PARALLEL_LEVEL"] = "4"
    # Existing platform tools only. Missing compilers/build tools are a host
    # prerequisite; this recipe never installs or updates global tooling.
    if not shutil.which("cmake") and platform.system() == "Windows":
        vswhere = Path(os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)")) / (
            "Microsoft Visual Studio/Installer/vswhere.exe")
        if vswhere.is_file():
            installation = subprocess.check_output(
                [str(vswhere), "-latest", "-products", "*", "-requires",
                 "Microsoft.VisualStudio.Component.VC.Tools.x86.x64", "-property", "installationPath"],
                text=True).strip()
            cmake = Path(installation) / "Common7/IDE/CommonExtensions/Microsoft/CMake/CMake/bin"
            if (cmake / "cmake.exe").is_file():
                environment["PATH"] = str(cmake) + os.pathsep + environment.get("PATH", "")
    if not shutil.which("cmake", path=environment.get("PATH")):
        raise RuntimeError("An existing CMake installation is required")
    if platform.system() == "Windows":
        environment["CMAKE_GENERATOR"] = "Visual Studio 17 2022"
    return environment


def seal_wheel(wheel, native_license):
    with zipfile.ZipFile(wheel) as archive:
        contents = {name: archive.read(name) for name in archive.namelist() if not name.endswith("/")}
    libraries = {name.removeprefix("llama_cpp/"): hashlib.sha256(value).hexdigest()
                 for name, value in contents.items() if name.startswith("llama_cpp/lib/")
                 and (name.endswith((".dll", ".dylib", ".so")) or ".so." in name)}
    if not libraries or not any("llama" in name for name in libraries):
        raise RuntimeError("Built wheel does not contain the native llama.cpp libraries")
    records = [name for name in contents if name.endswith(".dist-info/RECORD")]
    if len(records) != 1:
        raise RuntimeError("Built wheel has an invalid RECORD")
    record = records[0]
    contents.pop(record)
    manifest = {"schema": 1, "wrapper_version": WRAPPER_VERSION,
                "wrapper_revision": WRAPPER_REVISION, "native_revision": NATIVE_REVISION,
                "sources": {name: {"url": url, "sha256": digest} for name, (url, digest) in SOURCES.items()},
                "cmake_args": CMAKE_ARGS, "libraries": libraries}
    contents["llama_cpp/snipvoice-native.json"] = json.dumps(manifest, sort_keys=True, indent=2).encode()
    contents["llama_cpp/native-LICENSE.txt"] = native_license.read_bytes()
    rows = io.StringIO(newline="")
    writer = csv.writer(rows, lineterminator="\n")
    for name, value in sorted(contents.items()):
        digest = base64.urlsafe_b64encode(hashlib.sha256(value).digest()).rstrip(b"=").decode("ascii")
        writer.writerow((name, "sha256=" + digest, len(value)))
    writer.writerow((record, "", ""))
    contents[record] = rows.getvalue().encode()
    temporary = wheel.with_suffix(".sealed")
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, value in sorted(contents.items()):
                archive.writestr(name, value)
        os.replace(temporary, wheel)
    finally:
        temporary.unlink(missing_ok=True)
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--wheel-dir", type=Path, required=True)
    parser.add_argument("--install", action="store_true", help="Explicitly install into the selected Python environment")
    arguments = parser.parse_args()
    work, output = arguments.work_dir.resolve(), arguments.wheel_dir.resolve()
    work.mkdir(parents=True, exist_ok=True)
    output.mkdir(parents=True, exist_ok=True)
    environment = build_environment()
    with tempfile.TemporaryDirectory(prefix="llama-build-", dir=work) as temporary:
        root = Path(temporary)
        environment["TMP"] = environment["TEMP"] = str(root)
        extracted = {}
        for name, (url, digest) in SOURCES.items():
            archive = work / (name + ".tar.gz")
            acquire(url, archive, digest)
            extracted[name] = extract(archive, root / name)
        wrapper, native = extracted["wrapper"], extracted["native"]
        shutil.copytree(native, wrapper / "vendor/llama.cpp", dirs_exist_ok=True)
        wheels = root / "wheels"
        subprocess.run([sys.executable, "-m", "pip", "wheel", "--no-deps", "--no-build-isolation",
                        "--wheel-dir", str(wheels), str(wrapper)], env=environment, check=True)
        candidates = list(wheels.glob("llama_cpp_python-*.whl"))
        if len(candidates) != 1:
            raise RuntimeError("Expected exactly one custom llama.cpp wheel")
        wheel = candidates[0]
        seal_wheel(wheel, native / "LICENSE")
        destination = output / wheel.name
        if destination.exists():
            raise RuntimeError("Output wheel already exists; choose a fresh wheel directory")
        shutil.copy2(wheel, destination)
    digest = hashlib.sha256(destination.read_bytes()).hexdigest()
    requirement = output / "requirements-llama.lock"
    if requirement.exists():
        raise RuntimeError("Output requirements already exist; choose a fresh wheel directory")
    requirement.write_text(f"{destination.name} --hash=sha256:{digest}\n", encoding="ascii")
    if arguments.install:
        subprocess.run([sys.executable, "-m", "pip", "install", "--no-index", "--no-deps",
                        "--force-reinstall", "--require-hashes", "-r", requirement.name],
                       cwd=output, check=True)
    print("Custom llama.cpp wheel built and hash-pinned", flush=True)


if __name__ == "__main__":
    main()
