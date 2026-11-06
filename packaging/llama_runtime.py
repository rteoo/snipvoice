"""Build the pinned wrapper/native pair; never install an upstream binary wheel."""

import argparse
import base64
import csv
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
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
# Portable Windows runtime: every CPU microarchitecture variant and the
# vendor-neutral Vulkan GPU backend ship as loadable modules, and ggml picks
# what each machine supports at startup. The relative bin dir installs those
# modules beside llama.dll instead of outside the package.
PORTABLE_CMAKE_ARGS = ("-DGGML_BACKEND_DL=ON -DGGML_CPU_ALL_VARIANTS=ON -DGGML_VULKAN=ON"
                       " -DCMAKE_INSTALL_BINDIR=llama_cpp/lib")
BACKENDS = ("portable", "minimal")
# ceiling: MSBuild keeps the 260-character path limit. The Vulkan shader
# generator is a nested CMake project whose compiler probe sits about 200
# characters below the work dir, so short names are used inside it and longer
# work dirs are refused up front instead of failing deep in the build.
WINDOWS_WORK_DIR_LIMIT = 48


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


def default_backends():
    # macOS uses the wrapper's built-in Metal backend; other hosts are CI only.
    return "portable" if platform.system() == "Windows" else "minimal"


def cmake_args(backends):
    if backends not in BACKENDS:
        raise RuntimeError(f"Unknown backend set: {backends}")
    if backends == "minimal":
        return CMAKE_ARGS
    if platform.system() != "Windows":
        raise RuntimeError("The portable backend set is built for Windows releases only")
    return CMAKE_ARGS + " " + PORTABLE_CMAKE_ARGS


def build_environment(backends="minimal"):
    environment = os.environ.copy()
    environment["CMAKE_ARGS"] = cmake_args(backends)
    if backends == "portable" and not (environment.get("VULKAN_SDK")
                                       and shutil.which("glslc", path=environment.get("PATH"))):
        # Shaders compile at build time; the runtime needs only the GPU driver.
        raise RuntimeError("The portable build requires an existing Vulkan SDK (VULKAN_SDK and glslc)")
    environment["CMAKE_BUILD_PARALLEL_LEVEL"] = "4"
    # Existing platform tools only. Missing compilers/build tools are a host
    # prerequisite; this recipe never installs or updates global tooling.
    installation = None
    if platform.system() == "Windows":
        vswhere = Path(os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)")) / (
            "Microsoft Visual Studio/Installer/vswhere.exe")
        if not vswhere.is_file():
            raise RuntimeError("Visual Studio Installer with MSVC C++ tools is required")
        installations = json.loads(subprocess.check_output(
            [str(vswhere), "-latest", "-products", "*", "-requires",
             "Microsoft.VisualStudio.Component.VC.Tools.x86.x64", "-format", "json"], text=True))
        if not installations:
            raise RuntimeError("An existing Visual Studio installation with MSVC C++ tools is required")
        installation = installations[0]
        if not shutil.which("cmake", path=environment.get("PATH")):
            cmake = Path(installation["installationPath"]) / "Common7/IDE/CommonExtensions/Microsoft/CMake/CMake/bin"
            if (cmake / "cmake.exe").is_file():
                environment["PATH"] = str(cmake) + os.pathsep + environment.get("PATH", "")
    cmake = shutil.which("cmake", path=environment.get("PATH"))
    if not cmake:
        raise RuntimeError("An existing CMake installation is required")
    if installation is not None:
        # Runner images can change Visual Studio majors. Match CMake's advertised
        # generator to the discovered compiler, even when CMake is already on PATH.
        capabilities = json.loads(subprocess.check_output([cmake, "-E", "capabilities"], text=True))
        major = int(installation["installationVersion"].split(".")[0])
        generators = [item["name"] for item in capabilities["generators"]
                      if item["name"].startswith(f"Visual Studio {major} ")]
        if len(generators) != 1:
            raise RuntimeError(f"Existing CMake does not support installed Visual Studio major {major}")
        environment["CMAKE_GENERATOR"] = generators[0]
        environment["CMAKE_GENERATOR_INSTANCE"] = installation["installationPath"]
    return environment


def seal_wheel(wheel, native_license, backends="minimal"):
    with zipfile.ZipFile(wheel) as archive:
        contents = {name: archive.read(name) for name in archive.namelist() if not name.endswith("/")}
    libraries = {name.removeprefix("llama_cpp/"): hashlib.sha256(value).hexdigest()
                 for name, value in contents.items() if name.startswith("llama_cpp/lib/")
                 and (name.endswith((".dll", ".dylib", ".so")) or ".so." in name)}
    if not libraries or not any("llama" in name for name in libraries):
        raise RuntimeError("Built wheel does not contain the native llama.cpp libraries")
    if backends == "portable":
        names = {PurePosixPath(name).name for name in libraries}
        cpu_variants = {name for name in names if name.startswith("ggml-cpu-")}
        if "ggml-vulkan.dll" not in names or len(cpu_variants) < 2:
            # The wrapper installs a fixed target list; a backend left in the
            # build tree would silently ship a CPU-only or GPU-less runtime.
            raise RuntimeError("Portable wheel is missing its Vulkan or CPU variant backends")
    records = [name for name in contents if name.endswith(".dist-info/RECORD")]
    if len(records) != 1:
        raise RuntimeError("Built wheel has an invalid RECORD")
    record = records[0]
    contents.pop(record)
    manifest = {"schema": 1, "wrapper_version": WRAPPER_VERSION,
                "wrapper_revision": WRAPPER_REVISION, "native_revision": NATIVE_REVISION,
                "sources": {name: {"url": url, "sha256": digest} for name, (url, digest) in SOURCES.items()},
                "cmake_args": cmake_args(backends), "backends": backends, "libraries": libraries}
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
    parser.add_argument("--backends", choices=BACKENDS, default=default_backends(),
                        help="portable: CPU variants and Vulkan, chosen per machine (Windows release default); "
                             "minimal: the single built-in backend (CPU, or Metal on macOS)")
    arguments = parser.parse_args()
    work, output = arguments.work_dir.resolve(), arguments.wheel_dir.resolve()
    if platform.system() == "Windows" and len(str(work)) > WINDOWS_WORK_DIR_LIMIT:
        raise RuntimeError(f"Use a --work-dir of at most {WINDOWS_WORK_DIR_LIMIT} characters on Windows "
                           f"(MSBuild path limit); {work} has {len(str(work))}")
    work.mkdir(parents=True, exist_ok=True)
    output.mkdir(parents=True, exist_ok=True)
    environment = build_environment(arguments.backends)
    with tempfile.TemporaryDirectory(prefix="lb-", dir=work) as temporary:
        root = Path(temporary)
        environment["TMP"] = environment["TEMP"] = str(root)
        extracted = {}
        for name, (url, digest) in SOURCES.items():
            archive = work / (name + ".tar.gz")
            acquire(url, archive, digest)
            # One-letter roots: the archive folders carry 40-character revisions.
            short = root / name[0]
            os.replace(extract(archive, root / ("x" + name[0])), short)
            extracted[name] = short
        wrapper, native = extracted["wrapper"], extracted["native"]
        shutil.copytree(native, wrapper / "vendor/llama.cpp", dirs_exist_ok=True)
        wheels = root / "wheels"
        subprocess.run([sys.executable, "-m", "pip", "wheel", "--no-deps", "--no-build-isolation",
                        "--config-settings=build-dir=" + str(root / "b"),
                        "--wheel-dir", str(wheels), str(wrapper)], env=environment, check=True)
        candidates = list(wheels.glob("llama_cpp_python-*.whl"))
        if len(candidates) != 1:
            raise RuntimeError("Expected exactly one custom llama.cpp wheel")
        wheel = candidates[0]
        seal_wheel(wheel, native / "LICENSE", arguments.backends)
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
