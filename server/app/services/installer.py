"""Builds the downloadable installer, and the bundle agents update from.

The script is self-contained: the agent source is embedded as a base64 tar.gz
so the administrator downloads one file, copies it to the device, and runs it.
The same bundle bytes are served to already-enrolled machines when they update,
so an installed agent and an updated one are byte-identical.
"""

import base64
import gzip
import io
import tarfile
from functools import lru_cache
from pathlib import Path

BUNDLE_MEMBERS = ("sentinel_agent",)

# The stable launcher is installed from the installer and deliberately left out
# of the update bundle: updates must never replace the one piece that can put a
# broken version back.
LAUNCHER_MEMBERS = ("bin",)


def _deterministic(info: tarfile.TarInfo) -> tarfile.TarInfo:
    """Strip host-specific metadata so identical source yields identical bytes."""
    info.uid = 0
    info.gid = 0
    info.uname = "root"
    info.gname = "root"
    info.mtime = 0
    return info


def sorted_paths(top: Path) -> list[Path]:
    """Walk in a stable order. os.scandir order is filesystem-dependent, which
    would otherwise make the bundle bytes differ between machines."""
    paths = [top]
    if top.is_dir():
        for child in sorted(top.rglob("*"), key=lambda p: str(p)):
            if "__pycache__" in child.parts or child.suffix == ".pyc":
                continue
            paths.append(child)
    return paths


def source_fingerprint(root: Path) -> tuple:
    """Newest mtime and total size across the agent source.

    Used as a cache key so that editing the agent invalidates cached bundles on
    its own. In production the source is baked into the image and never
    changes, so this costs one stat sweep per request; in development it is
    what stops a downloaded installer carrying stale code.
    """
    newest = 0.0
    total = 0
    for name in BUNDLE_MEMBERS + LAUNCHER_MEMBERS:
        for path in sorted_paths(root / name):
            try:
                stat = path.stat()
            except OSError:
                continue
            newest = max(newest, stat.st_mtime)
            total += stat.st_size
    return (newest, total)


def _pack(root: Path, members: tuple[str, ...]) -> bytes:
    missing = [name for name in members if not (root / name).exists()]
    if missing:
        raise FileNotFoundError(
            f"agent source incomplete under {root}: missing {', '.join(missing)}"
        )

    buf = io.BytesIO()
    # GzipFile(mtime=0) rather than tarfile's "w:gz", because the latter stamps
    # the current time into the gzip header and there is no way to override it.
    with gzip.GzipFile(fileobj=buf, mode="wb", compresslevel=9, mtime=0) as gz:
        with tarfile.open(fileobj=gz, mode="w") as tar:
            for name in members:
                for path in sorted_paths(root / name):
                    tar.add(
                        path,
                        arcname=str(path.relative_to(root)),
                        filter=_deterministic,
                        recursive=False,
                    )
    return buf.getvalue()


@lru_cache(maxsize=4)
def _bundle_cached(agent_source_dir: str, _fingerprint: tuple) -> bytes:
    return _pack(Path(agent_source_dir), BUNDLE_MEMBERS)


@lru_cache(maxsize=4)
def _launcher_cached(agent_source_dir: str, _fingerprint: tuple) -> bytes:
    return _pack(Path(agent_source_dir), LAUNCHER_MEMBERS)


def bundle_bytes(agent_source_dir: str) -> bytes:
    """The agent code an updating machine downloads. Excludes the launcher."""
    root = Path(agent_source_dir)
    return _bundle_cached(agent_source_dir, source_fingerprint(root))


def launcher_bytes(agent_source_dir: str) -> bytes:
    """The stable launcher, shipped only by the installer."""
    root = Path(agent_source_dir)
    return _launcher_cached(agent_source_dir, source_fingerprint(root))


def _template(agent_source_dir: str) -> str:
    """Read every time: it is one small file, and a stale installer template is
    exactly the kind of bug that is invisible until a device fails to enroll."""
    return (Path(agent_source_dir) / "install.sh").read_text(encoding="utf-8")


def _shell_single_quote(value: str) -> str:
    """Quote a value for safe inclusion in a single-quoted shell assignment."""
    return value.replace("'", "'\\''")


def build_installer(
    agent_source_dir: str,
    *,
    version: str,
    server_url: str | None = None,
    api_key: str | None = None,
    insecure: bool = False,
) -> str:
    """Render install.sh, optionally with credentials pre-filled.

    Omitted credentials leave the script prompting interactively, which is what
    the generic download does.
    """
    script = _template(agent_source_dir)
    replacements = {
        "@@SERVER_URL@@": _shell_single_quote(server_url or ""),
        "@@API_KEY@@": _shell_single_quote(api_key or ""),
        "@@INSECURE@@": "1" if insecure else "",
        "@@AGENT_VERSION@@": _shell_single_quote(version),
        "@@BUNDLE@@": base64.b64encode(bundle_bytes(agent_source_dir)).decode("ascii"),
        "@@LAUNCHER@@": base64.b64encode(launcher_bytes(agent_source_dir)).decode("ascii"),
    }
    for token, value in replacements.items():
        if token not in script:
            raise ValueError(f"installer template is missing the {token} placeholder")
        script = script.replace(token, value)
    return script


def clear_caches() -> None:
    """Used by tests. Normal operation needs no manual invalidation, because
    the cache key includes the source fingerprint."""
    _bundle_cached.cache_clear()
    _launcher_cached.cache_clear()
