"""Fetch the newest stable sources in the supported release lines at image build time."""

import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import tarfile
from urllib.request import urlopen

RELEASES = {
    "ffmpeg": ("https://ffmpeg.org/releases/", r"ffmpeg-(9\.\d+(?:\.\d+)?)\.tar\.xz"),
    "curl": ("https://curl.se/download/", r"curl-(8\.\d+\.\d+)\.tar\.xz"),
    # Cairo uses odd minor versions for development releases.
    "cairo": ("https://cairographics.org/releases/", r"cairo-(1\.(?:[02468]|\d*[02468])\.\d+)\.tar\.xz"),
}


def download(url: str, destination: Path) -> None:
    with urlopen(url, timeout=120) as response, destination.open("wb") as output:
        while chunk := response.read(1024 * 1024):
            output.write(chunk)


def latest(index: str, pattern: str) -> str:
    with urlopen(index, timeout=60) as response:
        versions = set(re.findall(pattern, response.read().decode()))
    if not versions:
        raise RuntimeError(f"No supported release found at {index}")
    return max(versions, key=lambda value: tuple(map(int, value.split("."))))


def resolve_versions() -> dict[str, str]:
    return {name: latest(index, pattern) for name, (index, pattern) in RELEASES.items()}


def read_versions(manifest: Path) -> dict[str, str]:
    versions = json.loads(manifest.read_text())
    if not isinstance(versions, dict) or set(versions) != set(RELEASES):
        raise ValueError("Native release manifest must contain ffmpeg, curl and cairo")
    for name, version in versions.items():
        if not isinstance(version, str) or re.fullmatch(RELEASES[name][1], f"{name}-{version}.tar.xz") is None:
            raise ValueError(f"Unsupported {name} version in native release manifest")
    return versions


def sources(manifest: Path) -> None:
    versions = read_versions(manifest)
    destination = Path("/tmp/native")
    destination.mkdir()
    for name, (index, _) in RELEASES.items():
        version = versions[name]
        archive = destination / f"{name}-{version}.tar.xz"
        download(f"{index}{archive.name}", archive)
        if name == "cairo":
            checksum = archive.with_suffix(".xz.sha256sum")
            download(f"{index}{checksum.name}", checksum)
            expected = checksum.read_text().split()[0]
            with archive.open("rb") as source:
                if hashlib.file_digest(source, "sha256").hexdigest() != expected:
                    raise RuntimeError("Cairo source checksum mismatch")
        else:
            key = destination / f"{name}.asc"
            key_url = "https://ffmpeg.org/ffmpeg-devel.asc" if name == "ffmpeg" else "https://daniel.haxx.se/mykey.asc"
            download(key_url, key)
            subprocess.run(["gpg", "--batch", "--import", str(key)], check=True)
            signature = Path(f"{archive}.asc")
            download(f"{index}{signature.name}", signature)
            subprocess.run(["gpg", "--batch", "--verify", str(signature), str(archive)], check=True)
        with tarfile.open(archive) as source:
            source.extractall(destination, filter="data")
        (destination / name).symlink_to(f"{name}-{version}")
    Path("/usr/local/share/imghost-native-versions.json").write_text(json.dumps(versions, sort_keys=True) + "\n")


def garage(architecture: str) -> None:
    triples = {"amd64": "x86_64-unknown-linux-musl", "arm64": "aarch64-unknown-linux-musl"}
    with urlopen("https://git.deuxfleurs.fr/api/v1/repos/Deuxfleurs/garage/releases?limit=100", timeout=60) as response:
        releases = json.load(response)
    tags = [release["tag_name"] for release in releases if not release["prerelease"] and not release["draft"]
            and re.fullmatch(r"v2\.\d+\.\d+", release["tag_name"])]
    version = max(tags, key=lambda tag: tuple(map(int, tag[1:].split("."))))
    download(f"https://garagehq.deuxfleurs.fr/_releases/{version}/{triples[architecture]}/garage", Path("/garage"))
    Path("/garage").chmod(0o755)
    print(f"Selected Garage {version} ({architecture})", flush=True)


if __name__ == "__main__":
    if sys.argv[1:] == ["resolve"]:
        print(json.dumps(resolve_versions(), sort_keys=True))
    elif len(sys.argv) == 3 and sys.argv[1] == "fetch":
        sources(Path(sys.argv[2]))
    elif len(sys.argv) == 2 and sys.argv[1] in {"amd64", "arm64"}:
        garage(sys.argv[1])
    else:
        raise SystemExit("Usage: native_sources.py resolve | fetch MANIFEST | amd64 | arm64")
