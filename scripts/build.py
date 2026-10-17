#!/usr/bin/env python3
"""Build a native Linux AOSP Clang toolchain from pinned source revisions."""

import argparse
import ast
import hashlib
import http.client
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shlex
import shutil
import struct
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request


TOOLCHAIN_REPO = "aosp-mirror-neo/toolchain_llvm_android"
LLVM_REPO = "llvm/llvm-project"
PRESETS = {
    # clang-r487747c's manifest_10087095.xml (Android 14).
    "r487747c": "8443a75fcd5c80245b194f6510b98a11098fe7fe",
    # Android's manifest_12833971.xml; pins the patch set as well as the base.
    "r536225": "43ef7af1325b43e13d926d74a89741d4ace5fcf8",
}
COMPONENTS = [
    "clang", "clang-resource-headers", "lld", "llvm-ar", "llvm-nm",
    "llvm-objcopy", "llvm-objdump", "llvm-profdata", "llvm-readobj",
    "llvm-size", "llvm-strings", "llvm-strip", "llvm-symbolizer",
    "llvm-as", "llvm-dis", "llvm-link", "llc", "opt",
]
SMOKE_SOURCE = """typedef unsigned (*callback)(unsigned);
__attribute__((noinline)) unsigned increment(unsigned x) { return x + 1; }
__attribute__((noinline)) unsigned invoke(callback fn, unsigned x) {
    return fn(x);
}
void _start(void) {
    volatile unsigned result = invoke(increment, 41);
    (void)result;
    for (;;) {}
}
"""
# Mirrors AOSP's Soong cfiCflags/cfiLdflags: ThinLTO + cross-DSO CFI + hidden visibility.
# Clang 11 predates KCFI; this is the classic CFI configuration used by Android.
CFI_FLAGS = [
    "-flto=thin",
    "-fsanitize=cfi",
    "-fsanitize-cfi-cross-dso",
    "-fvisibility=hidden",
    "-fsanitize-trap=cfi",
]


class BuildError(RuntimeError):
    pass


class PatchResult(list):
    """List of applied patches, with skipped patches attached as .skipped."""

    def __init__(self, applied, skipped):
        super().__init__(applied)
        self.skipped = skipped


def run(command, *, cwd=None, capture=False, display=True, env=None):
    print("+ " + shlex.join(str(x) for x in command), flush=True)
    result = subprocess.run(
        [str(x) for x in command], cwd=cwd, check=True, text=True, env=env,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.STDOUT if capture else None,
    )
    if capture:
        if display:
            print(result.stdout.strip(), flush=True)
        return result.stdout
    return ""


def revision_number(value):
    value = value.removeprefix("clang-")
    match = re.fullmatch(r"r([1-9][0-9]*)([a-z][0-9]*)?", value)
    if not match:
        raise BuildError("Use a revision such as r536225 or clang-r536225.")
    return value, int(match.group(1))


def version_constants(path):
    """Read constants without executing scripts from the downloaded checkout."""
    values = {}
    for node in ast.parse(path.read_text()).body:
        if isinstance(node, ast.Assign):
            names, expression = node.targets, node.value
        elif isinstance(node, ast.AnnAssign):
            names, expression = [node.target], node.value
        else:
            continue
        try:
            value = ast.literal_eval(expression)
        except (ValueError, TypeError):
            continue
        for name in names:
            if isinstance(name, ast.Name):
                values[name.id] = value
    if not isinstance(values.get("_svn_revision"), str):
        raise BuildError(f"{path}: unsupported version metadata: _svn_revision")
    if "_git_sha" in values and not isinstance(values["_git_sha"], str):
        raise BuildError(f"{path}: unsupported version metadata: _git_sha")
    return values


def effective_revision(requested, declared):
    requested, number = revision_number(requested)
    declared, declared_number = revision_number(declared)
    if number != declared_number:
        raise BuildError(f"Requested {requested}, but toolchain metadata declares {declared}.")
    requested_suffix = requested != f"r{number}"
    declared_suffix = declared != f"r{number}"
    if requested_suffix and declared_suffix and requested != declared:
        raise BuildError(f"Requested variant {requested}, but the manifest declares {declared}.")
    return requested if requested_suffix else declared


def select_patches(entries, svn_revision):
    """AOSP's half-open revision ranges, preserving PATCHES.json order."""
    selected = []
    for entry in entries:
        interval = entry.get("version_range", {})
        lower, upper = interval.get("from", 0), interval.get("until")
        if lower is None:
            lower = 0
        if not lower <= svn_revision or (upper is not None and svn_revision >= upper):
            continue
        relative = PurePosixPath(entry["rel_patch_path"])
        if relative.is_absolute() or ".." in relative.parts:
            raise BuildError(f"Invalid patch path: {relative}")
        selected.append(entry)
    return selected


def github_json(url):
    headers = {"User-Agent": "aosp-clang-builder", "Accept": "application/vnd.github+json"}
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30) as response:
        return json.load(response)


def resolve_ref(repo, ref):
    if re.fullmatch(r"[0-9a-f]{40}", ref):
        return ref
    url = f"https://api.github.com/repos/{repo}/commits/{urllib.parse.quote(ref, safe='')}"
    return github_json(url)["sha"]


def toolchain_revision(revision, explicit_ref):
    if explicit_ref:
        return resolve_ref(TOOLCHAIN_REPO, explicit_ref)
    if revision in PRESETS:
        return PRESETS[revision]
    _, number = revision_number(revision)
    if revision != f"r{number}":
        raise BuildError(
            "A suffixed revision needs --toolchain-ref from its official manifest; "
            "the r-number alone does not identify the patch set."
        )
    for ref in (f"llvm-r{number}-release", f"llvm-r{number}"):
        try:
            return resolve_ref(TOOLCHAIN_REPO, ref)
        except urllib.error.HTTPError as error:
            # GitHub's commits API uses 422 for an unknown branch/ref.
            if error.code not in (404, 422):
                raise
    raise BuildError("No release branch found. Supply --toolchain-ref explicitly.")


def download(url, destination):
    if destination.is_file():
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".part")
    for attempt in range(1, 4):
        try:
            print(f"Downloading {url} (attempt {attempt})", flush=True)
            request = urllib.request.Request(url, headers={"User-Agent": "aosp-clang-builder"})
            with urllib.request.urlopen(request, timeout=60) as response, temporary.open("wb") as output:
                shutil.copyfileobj(response, output, length=1024 * 1024)
            temporary.replace(destination)
            print(f"Downloaded {destination.stat().st_size // 1024**2} MiB", flush=True)
            return
        except (OSError, EOFError, http.client.HTTPException) as error:
            print(f"Download failed: {error}", flush=True)
            if attempt == 3:
                raise
            time.sleep(attempt * 2)


def source_archive(repo, sha, destination, cache):
    if destination.exists():
        raise BuildError(f"Source directory already exists: {destination}. Use a fresh --work-dir.")
    archive = cache / f"{repo.split('/')[-1]}-{sha}.tar.gz"
    download(f"https://codeload.github.com/{repo}/tar.gz/{sha}", archive)
    destination.parent.mkdir(parents=True, exist_ok=True)
    unpack = destination.parent / f".unpack-{destination.name}"
    unpack.mkdir()
    with tarfile.open(archive) as contents:
        # Python >= 3.12 rejects archives that escape the extraction directory.
        contents.extractall(unpack, filter="data")
    children = list(unpack.iterdir())
    if len(children) != 1 or not children[0].is_dir():
        raise BuildError(f"Unexpected source archive layout: {archive}")
    children[0].rename(destination)
    unpack.rmdir()


def save_metadata(work, metadata):
    (work / "source-lock.json").write_text(json.dumps(metadata, indent=2) + "\n")


def load_metadata(work):
    path = work / "source-lock.json"
    if not path.is_file():
        raise BuildError("Run the prepare step first.")
    return json.loads(path.read_text())


def _clean_patch_artifacts(llvm):
    """Remove .rej/.orig files left behind by a failed patch attempt."""
    removed = 0
    for path in llvm.rglob("*"):
        if path.suffix in (".rej", ".orig"):
            path.unlink()
            removed += 1
    if removed:
        print(f"Cleaned {removed} patch artifact(s) (*.rej, *.orig)", flush=True)


def apply_patchset(llvm, patch_directory, patches):
    applied = []
    skipped = []
    for index, entry in enumerate(patches, 1):
        relative = entry["rel_patch_path"]
        patch = patch_directory / relative
        print(f"Patch {index}/{len(patches)}: {relative}", flush=True)
        # Match AOSP toolchain-utils' GNU patch invocation, including its
        # context matching. Some official backports require GNU patch fuzz.
        command = ["patch", "-d", llvm, "-f", "-E", "-p1", "--no-backup-if-mismatch", "-i", patch]
        try:
            run([*command, "--dry-run"])
        except subprocess.CalledProcessError:
            print(f"WARNING: skipping {relative} (dry-run rejected the patch)", flush=True)
            _clean_patch_artifacts(llvm)
            skipped.append({"path": relative, "reason": "dry-run failed"})
            continue
        try:
            run(command)
        except subprocess.CalledProcessError:
            print(f"WARNING: skipping {relative} (apply failed; source tree may be partially patched)", flush=True)
            _clean_patch_artifacts(llvm)


def write_usage_files(prefix, metadata):
    (prefix / "install.sh").unlink(missing_ok=True)
    (prefix / "README.txt").write_text(
        f"AOSP Clang {metadata['clang_revision']} / Linux {metadata['host_architecture']}\n\n"
        "在同一 CPU 架构的 Ubuntu 22.04 / 24.04（含手机 chroot / proot）中使用。\n"
        "解压后进入本目录即可运行，无需设置头文件、资源目录或动态库路径：\n\n"
        "  ./bin/clang --version\n"
        "  ./bin/ld.lld --version\n\n"
        "保持 bin、lib 等目录的相对位置，整个工具链文件夹可直接移动或重命名。\n"
        "Clang 会自动查找包内的内置头文件和同目录下的 LLVM 工具。\n\n"
        "编译内核可使用 make LLVM=/工具链所在目录/bin/ ARCH=arm64。\n"
        "普通 Ubuntu C/C++ 程序所需的系统头文件和运行库由 build-essential 提供。\n"
    )


def relocation(args, work):
    """Check direct tool use and resource/linker discovery after relocation."""
    metadata = load_metadata(work)
    prefix = Path(metadata["install_prefix"])
    write_usage_files(prefix, metadata)
    checks = work / "checks"
    checks.mkdir(exist_ok=True)
    environment = {key: value for key, value in os.environ.items() if key not in {
        "LD_LIBRARY_PATH", "LIBRARY_PATH", "COMPILER_PATH", "GCC_EXEC_PREFIX",
        "CPATH", "C_INCLUDE_PATH", "CPLUS_INCLUDE_PATH", "OBJC_INCLUDE_PATH",
    }}
    environment["PATH"] = "/usr/bin:/bin"
    relocation_root = checks / "relocated toolchain"
    if relocation_root.exists():
        shutil.rmtree(relocation_root)
    moved = relocation_root / f"clang-{metadata['clang_revision']}"
    shutil.copytree(prefix, moved, symlinks=True)
    try:
        compiler = moved / "bin/clang"
        run([compiler, "--version"], env=environment)
        resource = Path(run([compiler, "-print-resource-dir"], capture=True, env=environment).strip())
        if not resource.resolve().is_relative_to(moved.resolve()) or not (resource / "include/stddef.h").is_file():
            raise BuildError("Relocated Clang did not find its bundled resource headers.")
        sample = relocation_root / "headers.c"
        sample.write_text("#include <stddef.h>\n#include <stdint.h>\n#include <stdarg.h>\n" + SMOKE_SOURCE)
        run([compiler, "-ffreestanding", "-fsyntax-only", sample], env=environment)
        for backend, triple, machine in (
            ("AArch64", "aarch64-linux-gnu", 183), ("ARM", "armv7a-linux-gnueabi", 40),
            ("X86", "x86_64-linux-gnu", 62), ("RISCV", "riscv64-linux-gnu", 243),
        ):
            if backend not in metadata["targets"]:
                continue
            linked = relocation_root / "smoke.elf"
            flags = CFI_FLAGS if backend == "AArch64" else []
            command = [
                compiler, f"--target={triple}", "-O2", "-ffreestanding", "-fno-stack-protector",
                "-nostdlib", "-fuse-ld=lld", "-Wl,-e,_start", *flags, sample, "-o", linked,
            ]
            plan = run([*command, "-###"], capture=True, display=False, env=environment)
            if str(moved / "bin/ld.lld") not in plan:
                raise BuildError("Relocated Clang did not select its bundled lld.")
            run(command, env=environment)
            if elf_machine(linked) != machine:
                raise BuildError("Relocated Clang/lld produced the wrong target architecture.")
            break
        for name in ("clang++", "ld.lld", "llvm-ar"):
            run([moved / "bin" / name, "--version"], env=environment)
    finally:
        shutil.rmtree(relocation_root)
    metadata["relocation_verification"] = "passed: moved directory with spaces, clean environment, bundled headers, bundled lld, direct LLVM tools"
    save_metadata(work, metadata)
    (prefix / "BUILD_INFO.json").write_text(json.dumps(metadata, indent=2) + "\n")


def package(args, work):
    metadata = load_metadata(work)
    if "verification" not in metadata:
        raise BuildError("Run verify before packaging.")
    prefix = Path(metadata["install_prefix"])
    write_usage_files(prefix, metadata)
    output = Path(args.output_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    archive = output / f"clang-{metadata['clang_revision']}-linux-{metadata['host_architecture']}.tar.xz"
    run(["tar", "-C", prefix.parent, "-I", "xz -T2 -6", "-cf", archive, prefix.name])
    digest = hashlib.sha256()
    with archive.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    archive.with_name(archive.name + ".sha256").write_text(f"{digest.hexdigest()}  {archive.name}\n")
    shutil.copyfile(prefix / "BUILD_INFO.json", output / "BUILD_INFO.json")
    print(f"Artifact: {archive}", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=["prepare", "build", "verify", "relocation", "compatibility", "package", "all"])
    parser.add_argument("--clang-revision", default="r536225")
    parser.add_argument("--toolchain-ref", default="")
    parser.add_argument("--llvm-ref", default="")
    parser.add_argument("--targets", default="AArch64;ARM;X86;BPF")
    parser.add_argument("--projects", default="clang;lld")
    parser.add_argument("--jobs", type=int, default=0)
    parser.add_argument("--cmake-args", default="")
    parser.add_argument("--configure-only", action="store_true")
    parser.add_argument("--work-dir", default=".build")
    parser.add_argument("--output-dir", default="dist")
    args = parser.parse_args()
    work = Path(args.work_dir).resolve()
    work.mkdir(parents=True, exist_ok=True)
    stages = ["prepare", "build", "verify", "relocation", "package"] if args.stage == "all" else [args.stage]
    if args.configure_only and args.stage != "build":
        parser.error("--configure-only is only available for the build stage.")
    for stage in stages:
        globals()[stage](args, work)


if __name__ == "__main__":
    try:
        main()
    except (BuildError, subprocess.CalledProcessError, OSError, urllib.error.URLError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        sys.exit(1)
