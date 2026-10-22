import hashlib
import importlib.util
from io import BytesIO
import json
from pathlib import Path
import tarfile

import pytest

spec = importlib.util.spec_from_file_location("native_sources", Path(__file__).parents[1] / "docker/native_sources.py")
native = importlib.util.module_from_spec(spec)
spec.loader.exec_module(native)

VERSIONS = {"ffmpeg": "9.0.2", "curl": "8.23.0", "cairo": "1.18.6"}


def test_native_resolution_chooses_numeric_stable_versions_in_supported_lines(monkeypatch) -> None:
    indexes = {
        "https://ffmpeg.org/releases/": b"ffmpeg-9.9.1.tar.xz ffmpeg-9.10.0.tar.xz ffmpeg-10.0.tar.xz ffmpeg-9.11-rc1.tar.xz",
        "https://curl.se/download/": b"curl-8.9.0.tar.xz curl-8.10.1.tar.xz curl-9.0.0.tar.xz",
        "https://cairographics.org/releases/": b"cairo-1.18.6.tar.xz cairo-1.19.0.tar.xz cairo-1.20.1.tar.xz",
    }
    monkeypatch.setattr(native, "urlopen", lambda url, **kwargs: BytesIO(indexes[url]))
    assert native.resolve_versions() == {"ffmpeg": "9.10.0", "curl": "8.10.1", "cairo": "1.20.1"}


@pytest.mark.parametrize("versions", [
    [], {"ffmpeg": "9.0.2"}, {**VERSIONS, "curl": "9.0.0"},
    {**VERSIONS, "cairo": "1.19.0"}, {**VERSIONS, "ffmpeg": "9.1-rc1"},
    {**VERSIONS, "curl": "8.23.0/../../other"},
])
def test_native_manifest_rejects_invalid_or_unsupported_releases(tmp_path, versions) -> None:
    manifest = tmp_path / "versions.json"
    manifest.write_text(json.dumps(versions))
    with pytest.raises(ValueError):
        native.read_versions(manifest)


def test_native_fetch_uses_only_resolved_versions_and_verifies_archives(monkeypatch, tmp_path) -> None:
    manifest = tmp_path / "versions.json"
    manifest.write_text(json.dumps(VERSIONS))
    archives = {}
    for name, version in VERSIONS.items():
        buffer = BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:xz") as archive:
            entry = tarfile.TarInfo(f"{name}-{version}/source.txt")
            entry.size = len(name)
            archive.addfile(entry, BytesIO(name.encode()))
        archives[name] = buffer.getvalue()
    downloads = []
    commands = []

    def download(url, destination):
        downloads.append(url)
        if url.endswith(".tar.xz"):
            name = destination.name.split("-", 1)[0]
            destination.write_bytes(archives[name])
        elif url.endswith(".sha256sum"):
            destination.write_text(hashlib.sha256(archives["cairo"]).hexdigest())
        else:
            destination.write_bytes(b"signature fixture")

    def forbid_resolution(*args):
        raise AssertionError("Compilation must not discover new versions")

    output = tmp_path / "installed.json"

    def mapped_path(value):
        if value == "/tmp/native":
            return tmp_path / "native"
        if value == "/usr/local/share/imghost-native-versions.json":
            return output
        return Path(value)

    monkeypatch.setattr(native, "Path", mapped_path)
    monkeypatch.setattr(native, "latest", forbid_resolution)
    monkeypatch.setattr(native, "download", download)
    monkeypatch.setattr(native.subprocess, "run", lambda args, **kwargs: commands.append(args))
    native.sources(manifest)
    assert json.loads(output.read_text()) == VERSIONS
    assert len([command for command in commands if "--verify" in command]) == 2
    for name, version in VERSIONS.items():
        assert (tmp_path / "native" / name / "source.txt").read_text() == name
        assert any(url.endswith(f"{name}-{version}.tar.xz") for url in downloads)
