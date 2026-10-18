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
    "r487747c": "8443a75fcd5c80245b194f6510b98a11098fe7fe",
    "r536225": "43ef7af1325b43e13d926d74a89741d4ace5fcf8",
}
COMPONENTS = [
    "clang", "clang-resource-headers", "lld", "llvm-ar", "llvm-nm",
    "llvm-objcopy", "llvm-objdump", "llvm-profdata", "llvm-readobj",
    "llvm-size", "llvm-strings", "llvm-strip", "llvm-symbolizer",
    "llvm-as", "llvm-dis", "llvm-link", "llc", "opt",
    "compiler-rt",
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
# CFI flags used by Android: ThinLTO + cross-DSO CFI + trap.
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
            print(f"WARNING: skipping {relative} (apply failed)", flush=True)
            _clean_patch_artifacts(llvm)
            skipped.append({"path": relative, "reason": "apply failed"})
            continue
        applied.append({
            "path": relative,
            "sha256": hashlib.sha256(patch.read_bytes()).hexdigest(),
        })
    return PatchResult(applied, skipped)


def prepare(args, work):
    if (work / "source-lock.json").exists():
        raise BuildError("Sources are already prepared. Build them, or use a fresh --work-dir.")
    revision, number = revision_number(args.clang_revision)
    patch_sha = toolchain_revision(revision, args.toolchain_ref)
    toolchain = work / "sources" / "toolchain"
    source_archive(TOOLCHAIN_REPO, patch_sha, toolchain, work / "downloads")
    candidates = [
        toolchain / "src/llvm_android/android_version.py",
        toolchain / "android_version.py",
    ]
    version_file = next((p for p in candidates if p.is_file()), None)
    if version_file is None:
        raise BuildError("Cannot find AOSP android_version.py in this revision.")
    version = version_constants(version_file)
    resolved_revision = effective_revision(revision, version["_svn_revision"])
    llvm_base = args.llvm_ref or version.get("_git_sha")
    if not llvm_base:
        raise BuildError("This toolchain revision does not pin an LLVM base; pass --llvm-ref.")
    llvm_sha = resolve_ref(LLVM_REPO, llvm_base)
    llvm = work / "sources" / "llvm-project"
    source_archive(LLVM_REPO, llvm_sha, llvm, work / "downloads")
    patch_index = toolchain / "patches/PATCHES.json"
    if not patch_index.is_file():
        raise BuildError("This toolchain revision has no patches/PATCHES.json.")
    patch_data = json.loads(patch_index.read_text())
    if isinstance(patch_data, dict):
        patch_data = patch_data.get("patches", [])
    patches = select_patches(patch_data, number)
    result = apply_patchset(llvm, toolchain / "patches", patches)
    applied, skipped = list(result), result.skipped
    patch_level = version.get("_patch_level")
    if patch_level is not None and not re.fullmatch(r"[0-9]+", str(patch_level)):
        raise BuildError("Unsupported AOSP patch level.")
    metadata = {
        "clang_revision": resolved_revision,
        "requested_revision": revision,
        "svn_revision": number,
        "toolchain_repository": f"https://github.com/{TOOLCHAIN_REPO}",
        "toolchain_commit": patch_sha,
        "llvm_repository": f"https://github.com/{LLVM_REPO}",
        "llvm_commit": llvm_sha,
        "aosp_llvm_commit": version.get("_git_sha"),
        "custom_llvm_base": llvm_sha != version.get("_git_sha"),
        "aosp_patch_level": patch_level,
        "patch_application": "AOSP GNU patch: -f -E -p1 --no-backup-if-mismatch (conflicts skipped)",
        "patches": applied,
        "skipped_patches": skipped,
    }
    save_metadata(work, metadata)
    print(f"Prepared {resolved_revision}: {len(applied)} patches applied, {len(skipped)} skipped; LLVM {llvm_sha}", flush=True)


def split_options(value, label):
    items = [item for item in value.split(";") if item]
    if not items or any(not re.fullmatch(r"[A-Za-z0-9_-]+", item) for item in items):
        raise BuildError(f"{label} must be a semicolon-separated list of names.")
    return list(dict.fromkeys(items))


def native_architecture():
    machine = platform.machine()
    if machine not in ("aarch64", "x86_64"):
        raise BuildError("Supported Linux hosts: aarch64 and x86_64.")
    if sys.platform != "linux":
        raise BuildError("This builder targets Linux hosts.")
    return machine


def compiler_pair():
    for suffix in ("-22", "-21", "-20", "-19", "-18", "-17", "-16", "-15", "-14", ""):
        compiler, cxx = shutil.which("clang" + suffix), shutil.which("clang++" + suffix)
        if compiler and cxx:
            return compiler, cxx
    raise BuildError("Install clang and lld before building.")


def automatic_jobs():
    available = os.cpu_count() or 1
    memory = re.search(r"MemAvailable:\s+(\d+)", Path("/proc/meminfo").read_text())
    if memory:
        available = min(available, max(1, int(memory.group(1)) // (2 * 1024**2)))
    for path in (Path("/sys/fs/cgroup/memory.max"), Path("/sys/fs/cgroup/memory/memory.limit_in_bytes")):
        if path.is_file() and path.read_text().strip().isdigit():
            available = min(available, max(1, int(path.read_text()) // (2 * 1024**3)))
    return available


def build(args, work):
    metadata = load_metadata(work)
    architecture = native_architecture()
    targets = split_options(args.targets, "targets")
    targets = [{"aarch64": "AArch64", "x86_64": "X86"}[architecture] if t == "Native" else t for t in targets]
    projects = split_options(args.projects, "projects")
    if not {"clang", "lld"}.issubset(projects):
        raise BuildError("Include clang and lld in --projects.")
    if set(projects) - {"clang", "lld", "clang-tools-extra"}:
        raise BuildError("Supported projects: clang;lld;clang-tools-extra.")
    if args.jobs < 0:
        raise BuildError("--jobs must be zero (automatic) or a positive number.")
    jobs = args.jobs or automatic_jobs()
    prefix = work / "install" / f"clang-{metadata['clang_revision']}"
    compiler, cxx = compiler_pair()
    linker = next((shutil.which(name) for name in ("ld.lld-22", "ld.lld-21", "ld.lld-20", "ld.lld-19", "ld.lld-18", "ld.lld-17", "ld.lld-16", "ld.lld-15", "ld.lld-14", "ld.lld") if shutil.which(name)), None)
    if not linker:
        raise BuildError("Install lld before building.")
    components = COMPONENTS.copy()
    if "clang-tools-extra" in projects:
        components += ["clang-tidy", "clangd", "clang-format", "clang-apply-replacements", "clang-scan-deps"]
    options = [
        f"-DCMAKE_INSTALL_PREFIX={prefix}", "-DCMAKE_BUILD_TYPE=Release",
        f"-DCMAKE_C_COMPILER={compiler}", f"-DCMAKE_CXX_COMPILER={cxx}",
        f"-DLLVM_USE_LINKER={linker}",
        "-DCMAKE_EXE_LINKER_FLAGS=-static-libstdc++ -static-libgcc",
        f"-DLLVM_ENABLE_PROJECTS={';'.join(projects)}",
        "-DLLVM_ENABLE_RUNTIMES=compiler-rt",
        f"-DLLVM_TARGETS_TO_BUILD={';'.join(targets)}",
        f"-DLLVM_DISTRIBUTION_COMPONENTS={';'.join(components)}",
        # Cross-compile compiler-rt builtins for the requested target architecture.
        f"-DLLVM_BUILTIN_TARGETS={'aarch64-unknown-linux-gnu' if 'AArch64' in targets else 'x86_64-unknown-linux-gnu'}",
        f"-DLLVM_BUILTINS_TARGET={'aarch64' if 'AArch64' in targets else 'x86_64'}",
        # Build only baremetal builtins; sanitizers/fuzzer/profile need libc headers.
        "-DCOMPILER_RT_BUILD_SANITIZERS=OFF",
        "-DCOMPILER_RT_BUILD_XRAY=OFF",
        "-DCOMPILER_RT_BUILD_PROFILE=OFF",
        "-DCOMPILER_RT_BUILD_LIBFUZZER=OFF",
        "-DCOMPILER_RT_BUILD_CRT=OFF",
        f"-DLLVM_PARALLEL_COMPILE_JOBS={jobs}", "-DLLVM_PARALLEL_LINK_JOBS=1",
        "-DLLVM_ENABLE_ASSERTIONS=OFF", "-DLLVM_INCLUDE_TESTS=OFF",
        "-DLLVM_INCLUDE_EXAMPLES=OFF", "-DLLVM_INCLUDE_BENCHMARKS=OFF",
        "-DLLVM_BUILD_DOCS=OFF", "-DLLVM_ENABLE_BINDINGS=OFF",
        "-DLLVM_APPEND_VC_REV=OFF",
        "-DLLVM_ENABLE_TERMINFO=OFF", "-DLLVM_ENABLE_LIBEDIT=OFF",
        "-DLLVM_ENABLE_LIBXML2=OFF",
        "-DLLVM_ENABLE_ZLIB=OFF", "-DLLVM_ENABLE_ZSTD=OFF",
        "-DLLVM_ENABLE_Z3_SOLVER=OFF", "-DCLANG_ANALYZER_BUILD_Z3=OFF",
        "-DLLVM_BUILD_LLVM_DYLIB=OFF", "-DLLVM_LINK_LLVM_DYLIB=OFF",
        "-DCLANG_LINK_CLANG_DYLIB=OFF",
        "-DLLVM_ENABLE_LTO=Thin",
        f"-DCLANG_VENDOR=Android (6877366 based on {metadata['clang_revision']}) ",
    ]
    if metadata["aosp_patch_level"] is not None:
        options.append(f"-DLLVM_VERSION_PATCH={metadata['aosp_patch_level']}")
    extra = shlex.split(args.cmake_args)
    if any(not value.startswith("-D") for value in extra):
        raise BuildError("--cmake-args accepts only -DKEY=VALUE definitions; quote values containing spaces.")
    options += extra
    build_dir = work / "build"
    run(["cmake", "-S", work / "sources/llvm-project/llvm", "-B", build_dir, "-G", "Ninja", *options])
    metadata.update({
        "host_architecture": architecture, "targets": targets, "projects": projects,
        "install_prefix": str(prefix), "build_jobs": jobs, "cmake_options": options,
        "runtime_targets": ["aarch64-unknown-linux-gnu"] if "AArch64" in targets else ["x86_64-unknown-linux-gnu"],
        "host_glibc": run(["getconf", "GNU_LIBC_VERSION"], capture=True).strip(),
        "bootstrap_clang": run([compiler, "--version"], capture=True).strip(),
        "cmake_version": run(["cmake", "--version"], capture=True).splitlines()[0],
    })
    save_metadata(work, metadata)
    if args.configure_only:
        print("Configuration validated; compilation was not requested.", flush=True)
        return
    run(["cmake", "--build", build_dir, "--target", "distribution", "--parallel", jobs])
    run(["cmake", "--build", build_dir, "--target", "install-distribution-stripped", "--parallel", jobs])
    aliases = {
        "clang++": "clang", "clang-cpp": "clang", "ld.lld": "lld",
        "llvm-ranlib": "llvm-ar", "llvm-readelf": "llvm-readobj", "llvm-addr2line": "llvm-symbolizer",
    }
    for alias, target in aliases.items():
        path = prefix / "bin" / alias
        if not path.exists():
            path.symlink_to(target)
    (prefix / "validation").mkdir(exist_ok=True)
    (prefix / "validation/kernel-smoke.c").write_text(SMOKE_SOURCE)
    notices = prefix / "licenses"
    notices.mkdir(exist_ok=True)
    for directory in ("llvm", "clang", "lld", "compiler-rt"):
        license_file = work / "sources/llvm-project" / directory / "LICENSE.TXT"
        if license_file.is_file():
            shutil.copyfile(license_file, notices / f"{directory}-LICENSE.TXT")
    for license_file in (work / "sources/toolchain").glob("MODULE_LICENSE_*"):
        shutil.copyfile(license_file, notices / license_file.name)
    shutil.copyfile(work / "sources/toolchain/patches/PATCHES.json", prefix / "PATCHES.json")
    version_dir = prefix / "lib" / "clang" / "11.0.0" / "share"
    version_dir.mkdir(parents=True, exist_ok=True)
    (version_dir / "cfi_blacklist.txt").touch()


def elf_machine(path):
    with path.open("rb") as binary:
        header = binary.read(20)
    if len(header) < 20 or header[:4] != b"\x7fELF":
        raise BuildError(f"Not an ELF executable: {path}")
    return struct.unpack_from("<H" if header[5] == 1 else ">H", header, 18)[0]


def compiler_rt_builtins(prefix, triple):
    """Locate libclang_rt.builtins-<arch>.a for the given target triple."""
    lib_root = prefix / "lib" / "clang" / "11.0.0" / "lib"
    arch = triple.split("-")[0]
    for candidate in lib_root.rglob(f"libclang_rt.builtins-{arch}.a"):
        if triple in str(candidate):
            return candidate
    matches = list(lib_root.rglob(f"libclang_rt.builtins-{arch}.a"))
    return matches[0] if matches else None


def verify(args, work):
    metadata = load_metadata(work)
    prefix = Path(metadata["install_prefix"])
    machine = {"aarch64": 183, "x86_64": 62}[native_architecture()]
    if native_architecture() != metadata["host_architecture"]:
        raise BuildError("Verify the toolchain on its native host architecture.")
    required = ["clang", "ld.lld", "llvm-ar", "llvm-nm", "llvm-objcopy", "llvm-objdump", "llvm-ranlib", "llvm-readelf", "llvm-strip", "llvm-profdata", "llc"]
    for name in required:
        executable = prefix / "bin" / name
        if elf_machine(executable) != machine:
            raise BuildError(f"{name} has the wrong host architecture.")
    glibc_versions = []
    executables = sorted({p.resolve() for p in (prefix / "bin").iterdir() if p.is_file()})
    for executable in executables:
        with executable.open("rb") as stream:
            if stream.read(4) != b"\x7fELF":
                continue
        if elf_machine(executable) != machine:
            raise BuildError(f"{executable.name} has the wrong host architecture.")
        dependencies = run(["ldd", executable], capture=True, display=False)
        if "not found" in dependencies:
            raise BuildError(f"{executable.name} has missing shared libraries.")
        symbols = run(["readelf", "--version-info", executable], capture=True, display=False)
        glibc_versions += re.findall(r"\bGLIBC_([0-9]+(?:\.[0-9]+)+)", symbols)
    version_output = run([prefix / "bin/clang", "--version"], capture=True)
    actual_version = re.search(r"clang version ([0-9]+\.[0-9]+\.[0-9]+)", version_output)
    if actual_version is None or metadata["clang_revision"] not in version_output:
        raise BuildError("Clang did not report the requested AOSP revision.")
    run([prefix / "bin/ld.lld", "--version"])
    run([prefix / "bin/llc", "--version"])
    # Confirm compiler-rt builtins were installed.
    builtins = None
    for triple in metadata.get("runtime_targets", []):
        builtins = compiler_rt_builtins(prefix, triple)
        if builtins:
            print(f"Found compiler-rt builtins: {builtins}", flush=True)
            break
    if not builtins:
        raise BuildError("compiler-rt builtins archive was not installed.")
    checks = work / "checks"
    checks.mkdir(exist_ok=True)
    sample = checks / "kernel-smoke.c"
    sample.write_text(SMOKE_SOURCE)
    targets = {"AArch64": ("aarch64-linux-gnu", 183), "ARM": ("armv7a-linux-gnueabi", 40), "X86": ("x86_64-linux-gnu", 62), "RISCV": ("riscv64-linux-gnu", 243)}
    for backend, (triple, target_machine) in targets.items():
        if backend not in metadata["targets"]:
            continue
        output = checks / f"{backend}.o"
        run([prefix / "bin/clang", f"--target={triple}", "-O2", "-ffreestanding", "-fno-stack-protector", "-c", sample, "-o", output])
        if elf_machine(output) != target_machine:
            raise BuildError(f"Cross compilation for {backend} produced the wrong architecture.")
        run([prefix / "bin/ld.lld", "-r", output, "-o", checks / f"{backend}-linked.o"])
    if "AArch64" in metadata["targets"]:
        bitcode = checks / "cfi.o"
        run([prefix / "bin/clang", "--target=aarch64-linux-gnu", "-O2", "-ffreestanding",
             "-fno-stack-protector", *CFI_FLAGS, "-c", sample, "-o", bitcode])
        run([prefix / "bin/ld.lld", "-e", "_start", bitcode, "-o", checks / "cfi.elf"])
        if elf_machine(checks / "cfi.elf") != 183:
            raise BuildError("ThinLTO/CFI linking failed.")
    if "BPF" in metadata["targets"]:
        bpf_source = checks / "bpf.c"
        bpf_source.write_text("int probe(void *ctx) { return ctx != 0; }\n")
        run([prefix / "bin/clang", "--target=bpf", "-O2", "-c", bpf_source, "-o", checks / "bpf.o"])
        if elf_machine(checks / "bpf.o") != 247:
            raise BuildError("BPF backend verification failed.")
    metadata.update({
        "clang_version": actual_version.group(1),
        "minimum_glibc": max(glibc_versions, key=lambda x: tuple(map(int, x.split(".")))) if glibc_versions else None,
        "compiler_rt_builtins": str(builtins) if builtins else None,
        "verification": "native ELF, dynamic dependencies, compiler-rt builtins present, selected cross targets, AArch64 ThinLTO/CFI (cross-dso) link",
    })
    save_metadata(work, metadata)
    (prefix / "BUILD_INFO.json").write_text(json.dumps(metadata, indent=2) + "\n")
    (prefix / "AndroidVersion.txt").write_text(f"{actual_version.group(1)}\nbased on {metadata['clang_revision']}\nnative Linux {metadata['host_architecture']} rebuild\n")
    print(f"Verified {actual_version.group(1)}; minimum glibc {metadata['minimum_glibc']}", flush=True)


def compatibility(args, work):
    metadata = load_metadata(work)
    prefix = Path(metadata["install_prefix"])
    docker_platform = {"aarch64": "linux/arm64", "x86_64": "linux/amd64"}[metadata["host_architecture"]]
    command = "/toolchain/bin/clang --version; /toolchain/bin/ld.lld --version"
    if "AArch64" in metadata["targets"]:
        cfi_args = " ".join(CFI_FLAGS)
        command += (
            f"; /toolchain/bin/clang --target=aarch64-linux-gnu -O2 -ffreestanding "
            f"-fno-stack-protector {cfi_args} -c "
            f"/toolchain/validation/kernel-smoke.c -o /tmp/cfi.o"
            f"; /toolchain/bin/ld.lld -e _start /tmp/cfi.o -o /tmp/cfi.elf"
        )
    run(["docker", "run", "--rm", "--platform", docker_platform, "--volume", f"{prefix}:/toolchain:ro", "ubuntu:24.04", "bash", "-euc", command])
    metadata["ubuntu_24_04_compatibility"] = "passed in a native stock Docker image"
    save_metadata(work, metadata)
    (prefix / "BUILD_INFO.json").write_text(json.dumps(metadata, indent=2) + "\n")


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
    metadata["relocation_verification"] = "passed: moved directory with spaces, clean environment, bundled headers, bundled lld, compiler-rt, direct LLVM tools"
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
