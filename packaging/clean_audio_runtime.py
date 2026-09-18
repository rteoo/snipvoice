"""Build PyAV against a minimal, shared, LGPL FFmpeg audio runtime.

This script is intentionally the only release path for PyAV.  It downloads
hash-pinned upstream source archives, builds FFmpeg without external codec
libraries, builds PyAV against those shared libraries, repairs the wheel, and
writes the compliance material consumed by the desktop bundle jobs.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tarfile
import urllib.request
import zipfile


FFMPEG_VERSION = "8.1.2"
FFMPEG_URL = f"https://ffmpeg.org/releases/ffmpeg-{FFMPEG_VERSION}.tar.xz"
FFMPEG_SHA256 = "464beb5e7bf0c311e68b45ae2f04e9cc2af88851abb4082231742a74d97b524c"
PYAV_VERSION = "18.1.0"
PYAV_URL = (
    "https://files.pythonhosted.org/packages/8d/f4/"
    "f22114d30d3435e38c6af2b4870f37b864403dca6ae7af747a289ce0a18e/"
    f"av-{PYAV_VERSION}.tar.gz"
)
PYAV_SHA256 = "47bfc286e1bc9de7ab4681fc2b575cd2460a66919d31ffe1bd5aa54fae531a28"

DEMUXERS = "aac,ac3,aiff,ape,asf,caf,flac,matroska,mov,mp3,ogg,w64,wav,wv"
DECODERS = ",".join(
    (
        "aac",
        "aac_fixed",
        "ac3",
        "eac3",
        "alac",
        "ape",
        "flac",
        "mp3",
        "mp3float",
        "mp3adu",
        "mp3adufloat",
        "mp3on4",
        "mp3on4float",
        "opus",
        "vorbis",
        "wavpack",
        "pcm_alaw",
        "pcm_f32be",
        "pcm_f32le",
        "pcm_f64be",
        "pcm_f64le",
        "pcm_mulaw",
        "pcm_s8",
        "pcm_s16be",
        "pcm_s16le",
        "pcm_s24be",
        "pcm_s24le",
        "pcm_s32be",
        "pcm_s32le",
        "pcm_u8",
    )
)
PARSERS = "aac,aac_latm,ac3,flac,mpegaudio,opus,vorbis"

CONFIGURE_FLAGS = (
    "--disable-static",
    "--enable-shared",
    "--enable-pic",
    "--disable-gpl",
    "--disable-nonfree",
    "--disable-version3",
    "--disable-programs",
    "--disable-doc",
    "--disable-debug",
    "--disable-network",
    "--disable-autodetect",
    "--disable-everything",
    "--enable-avcodec",
    "--enable-avdevice",
    "--enable-avfilter",
    "--enable-avformat",
    "--enable-swresample",
    "--enable-swscale",
    "--enable-protocol=file",
    f"--enable-demuxer={DEMUXERS}",
    f"--enable-decoder={DECODERS}",
    f"--enable-parser={PARSERS}",
    "--enable-filter=anull,aresample",
)


def run(
    command: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> None:
    print("+", subprocess.list2cmdline(command), flush=True)
    subprocess.run(command, cwd=cwd, env=env, check=True)


def download(url: str, destination: Path, sha256: str) -> None:
    if not destination.exists():
        with urllib.request.urlopen(url) as response, destination.open("wb") as output:
            shutil.copyfileobj(response, output)
    digest = hashlib.sha256(destination.read_bytes()).hexdigest()
    if digest != sha256:
        raise RuntimeError(f"SHA-256 mismatch for {destination.name}: {digest}")


def extract(archive: Path, destination: Path) -> Path:
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive) as source:
        roots = {Path(member.name).parts[0] for member in source.getmembers() if member.name}
        if len(roots) != 1:
            raise RuntimeError(f"Unexpected archive layout: {archive}")
        source.extractall(destination, filter="data")
    return destination / roots.pop()


def shell_path(path: Path, env: dict[str, str]) -> str:
    if platform.system() != "Windows":
        return str(path)
    result = subprocess.run(
        ["cygpath", "-u", str(path)],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    return result.stdout.strip()


def build_runtime_environment() -> dict[str, str]:
    """Return an environment that can run the native audio build.

    Git Bash provides the POSIX shell but not the MINGW64 compiler toolchain.
    When launched from PowerShell, locate a normal MSYS2 installation and
    expose its MINGW64 and MSYS binaries to every child process. CI already
    supplies the same tools in its MSYS2 shell, so this remains compatible
    with the existing release workflow.
    """

    environment = os.environ.copy()
    if platform.system() != "Windows":
        return environment

    required_tools = ("sh", "make", "gcc", "cygpath", "nasm", "pkg-config")
    inherited_path = environment.get("PATH", "")
    if all(shutil.which(tool, path=inherited_path) for tool in required_tools):
        return environment

    roots: list[Path] = []
    configured_root = environment.get("SNIPVOICE_MSYS2_ROOT")
    if configured_root:
        roots.append(Path(configured_root))
    roots.append(Path(r"C:\msys64"))

    for root in roots:
        msys_bin = root / "usr" / "bin"
        mingw_bin = root / "mingw64" / "bin"
        required_files = (
            msys_bin / "sh.exe",
            msys_bin / "make.exe",
            msys_bin / "cygpath.exe",
            mingw_bin / "gcc.exe",
            mingw_bin / "nasm.exe",
            mingw_bin / "pkg-config.exe",
        )
        if not all(path.is_file() for path in required_files):
            continue

        environment["MSYSTEM"] = "MINGW64"
        environment["PATH"] = os.pathsep.join(
            (str(mingw_bin), str(msys_bin), inherited_path)
        )
        return environment

    raise RuntimeError(
        "Windows clean audio builds require an MSYS2 MINGW64 toolchain. "
        "Install MSYS2 with sh, make, cygpath, gcc, nasm, and pkgconf, or "
        "set SNIPVOICE_MSYS2_ROOT to its installation directory. "
        "Git Bash alone does not provide gcc."
    )


def copy_windows_runtime(prefix: Path, env: dict[str, str]) -> None:
    libraries = (
        "avcodec",
        "avdevice",
        "avfilter",
        "avformat",
        "avutil",
        "swresample",
        "swscale",
    )
    for name in libraries:
        source = prefix / "bin" / f"{name}.lib"
        if source.exists():
            shutil.move(source, prefix / "lib" / source.name)
    missing = [name for name in libraries if not (prefix / "lib" / f"{name}.lib").is_file()]
    if missing:
        raise RuntimeError(f"FFmpeg did not produce MSVC import libraries: {missing}")
    gcc = shutil.which("gcc", path=env.get("PATH"))
    if not gcc:
        raise RuntimeError("gcc not found after the FFmpeg build")
    compiler_bin = Path(gcc).parent
    for name in ("libgcc_s_seh-1.dll", "libwinpthread-1.dll"):
        source = compiler_bin / name
        if source.exists():
            shutil.copy2(source, prefix / "bin" / name)


def write_compliance(
    output: Path,
    ffmpeg_source: Path,
    ffmpeg_build: Path,
    pyav_source: Path,
    prefix: Path,
    env: dict[str, str],
) -> None:
    output.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ffmpeg_source / "COPYING.LGPLv2.1", output / "FFmpeg-COPYING.LGPLv2.1.txt")
    shutil.copy2(pyav_source / "LICENSE.txt", output / "PyAV-LICENSE.txt")
    shutil.copy2(Path(__file__), output / "clean_audio_runtime.py")
    shutil.copy2(ffmpeg_build / "ffbuild" / "config.log", output / "FFmpeg-config.log")
    shutil.copy2(ffmpeg_build / "config.h", output / "FFmpeg-config.h")
    (output / "FFmpeg-source-changes.patch").write_text("", encoding="utf-8")
    (output / "FFmpeg-configure.txt").write_text(
        " ".join(("./configure", *CONFIGURE_FLAGS)) + "\n", encoding="utf-8"
    )
    manifest = {
        "ffmpeg": {
            "version": FFMPEG_VERSION,
            "source_url": FFMPEG_URL,
            "sha256": FFMPEG_SHA256,
            "license": "LGPL-2.1-or-later",
            "configure_flags": list(CONFIGURE_FLAGS),
        },
        "pyav": {
            "version": PYAV_VERSION,
            "source_url": PYAV_URL,
            "sha256": PYAV_SHA256,
            "license": "BSD-3-Clause",
        },
        "platform": platform.platform(),
        "python": sys.version,
        "runner_image": os.environ.get("ImageOS", "local"),
        "repository_commit": os.environ.get("GITHUB_SHA", "local"),
        "compiler": subprocess.run(
            ["gcc" if platform.system() == "Windows" else "clang", "--version"],
            check=True,
            capture_output=True,
            text=True,
            env=env,
        ).stdout.splitlines()[0],
    }
    (output / "runtime-manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    libraries = prefix / ("bin" if platform.system() == "Windows" else "lib")
    hashes = []
    for path in sorted(libraries.iterdir()):
        if path.is_file() and path.suffix.lower() in {".dll", ".dylib", ".so"}:
            hashes.append(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}")
    (output / "shared-library-sha256.txt").write_text(
        "\n".join(hashes) + "\n", encoding="ascii"
    )


def verify_wheel(wheel: Path) -> None:
    expected = ("avcodec", "avdevice", "avfilter", "avformat", "avutil", "swresample", "swscale")
    forbidden = ("x264", "x265", "xvid", "fdk", "rubberband", "vidstab")
    with zipfile.ZipFile(wheel) as archive:
        names = [name.lower() for name in archive.namelist()]
    missing = [library for library in expected if not any(library in name for name in names)]
    found_forbidden = [library for library in forbidden if any(library in name for name in names)]
    if missing or found_forbidden:
        raise RuntimeError(
            f"Unapproved repaired wheel; missing={missing}, forbidden={found_forbidden}"
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--wheel-dir", type=Path, required=True)
    parser.add_argument("--compliance-dir", type=Path, required=True)
    args = parser.parse_args()

    if platform.system() not in {"Windows", "Darwin"}:
        raise RuntimeError("Release builds support Windows and macOS only")

    runtime_environment = build_runtime_environment()

    work = args.work_dir.resolve()
    prefix = work / "ffmpeg-prefix"
    downloads = work / "downloads"
    sources = work / "sources"
    raw_wheels = work / "raw-wheels"
    for directory in (prefix, downloads, sources, raw_wheels, args.wheel_dir):
        directory.mkdir(parents=True, exist_ok=True)

    ffmpeg_archive = downloads / f"ffmpeg-{FFMPEG_VERSION}.tar.xz"
    pyav_archive = downloads / f"av-{PYAV_VERSION}.tar.gz"
    download(FFMPEG_URL, ffmpeg_archive, FFMPEG_SHA256)
    download(PYAV_URL, pyav_archive, PYAV_SHA256)
    ffmpeg_source = extract(ffmpeg_archive, sources / "ffmpeg")
    pyav_source = extract(pyav_archive, sources / "pyav")

    configure = [
        "sh",
        shell_path(ffmpeg_source / "configure", runtime_environment),
        f"--prefix={shell_path(prefix, runtime_environment)}",
        f"--libdir={shell_path(prefix / 'lib', runtime_environment)}",
        f"--shlibdir={shell_path(prefix / ('bin' if platform.system() == 'Windows' else 'lib'), runtime_environment)}",
        *CONFIGURE_FLAGS,
    ]
    build_dir = work / "ffmpeg-build"
    build_dir.mkdir(exist_ok=True)
    run(configure, cwd=build_dir, env=runtime_environment)
    run(["make", "-j", str(os.cpu_count() or 2)], cwd=build_dir, env=runtime_environment)
    run(["make", "install"], cwd=build_dir, env=runtime_environment)
    if platform.system() == "Windows":
        copy_windows_runtime(prefix, runtime_environment)

    run(
        [
            sys.executable,
            "setup.py",
            "bdist_wheel",
            f"--dist-dir={raw_wheels}",
            f"--ffmpeg-dir={prefix}",
        ],
        cwd=pyav_source,
        env=runtime_environment,
    )
    wheels = list(raw_wheels.glob("av-*.whl"))
    if len(wheels) != 1:
        raise RuntimeError(f"Expected exactly one PyAV wheel, found {len(wheels)}")
    if platform.system() == "Windows":
        run(
            [
                sys.executable,
                "-m",
                "delvewheel",
                "repair",
                "--add-path",
                str(prefix / "bin"),
                "--wheel-dir",
                str(args.wheel_dir),
                str(wheels[0]),
            ],
            env=runtime_environment,
        )
    elif platform.system() == "Darwin":
        run(
            [
                "delocate-wheel",
                "--wheel-dir",
                str(args.wheel_dir),
                str(wheels[0]),
            ],
            env=runtime_environment,
        )
    else:  # pragma: no cover - guarded before any build work
        raise AssertionError("unsupported platform passed the early guard")

    repaired_wheels = list(args.wheel_dir.glob("av-*.whl"))
    if len(repaired_wheels) != 1:
        raise RuntimeError(f"Expected one repaired wheel, found {len(repaired_wheels)}")
    verify_wheel(repaired_wheels[0])
    write_compliance(
        args.compliance_dir.resolve(),
        ffmpeg_source,
        build_dir,
        pyav_source,
        prefix,
        runtime_environment,
    )
    shutil.copy2(ffmpeg_archive, args.compliance_dir / ffmpeg_archive.name)
    (args.compliance_dir / "PyAV-wheel-sha256.txt").write_text(
        f"{hashlib.sha256(repaired_wheels[0].read_bytes()).hexdigest()}  {repaired_wheels[0].name}\n",
        encoding="ascii",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
