"""Export tested images and publish those exact image contents without rebuilding."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tarfile

IMAGES = {"app": "imghost:verify", "garage": "imghost-garage:verify"}
ARCHITECTURES = {"amd64", "arm64"}


def docker(*args: str) -> str:
    return subprocess.check_output(["docker", *args], text=True).strip()


def inspect(reference: str) -> dict:
    return json.loads(docker("image", "inspect", reference))[0]


def checksum(path: Path) -> str:
    with path.open("rb") as archive:
        return hashlib.file_digest(archive, "sha256").hexdigest()


def archive_config_digest(path: Path) -> str:
    with tarfile.open(path) as archive:
        manifests = json.load(archive.extractfile("manifest.json"))
        if len(manifests) != 1:
            raise ValueError("Each verified archive must contain one platform image")
        with archive.extractfile(manifests[0]["Config"]) as configuration:
            return "sha256:" + hashlib.file_digest(configuration, "sha256").hexdigest()


def config_digest(info: dict) -> str:
    # The containerd store exposes a manifest ID; the classic store exposes a config ID.
    return (info.get("Descriptor") or {}).get("annotations", {}).get("config.digest", info["Id"])


def export(directory: Path, architecture: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    manifest = {"architecture": architecture, "images": {}}
    for name, reference in IMAGES.items():
        info = inspect(reference)
        if info["Architecture"] != architecture or info["Os"] != "linux":
            raise ValueError(f"Unexpected platform for {reference}")
        archive = directory / f"{name}.tar"
        docker("image", "save", "--platform", f"linux/{architecture}", "--output", str(archive), reference)
        manifest["images"][name] = {"config_digest": archive_config_digest(archive), "sha256": checksum(archive)}
    (directory / "images.json").write_text(json.dumps(manifest, sort_keys=True) + "\n")


def load_verified(directory: Path) -> dict[str, dict[str, dict[str, str]]]:
    images = {}
    manifests = sorted(directory.glob("*/images.json"))
    for path in manifests:
        manifest = json.loads(path.read_text())
        architecture = manifest["architecture"]
        if architecture not in ARCHITECTURES or architecture in images or set(manifest["images"]) != set(IMAGES):
            raise ValueError(f"Invalid image manifest: {path}")
        images[architecture] = {}
        for name, reference in IMAGES.items():
            expected = manifest["images"][name]
            archive = path.parent / f"{name}.tar"
            if checksum(archive) != expected["sha256"]:
                raise ValueError(f"Image archive checksum mismatch: {archive}")
            docker("image", "load", "--input", str(archive))
            info = inspect(reference)
            if (config_digest(info), info["Architecture"], info["Os"]) != (expected["config_digest"], architecture, "linux"):
                raise ValueError(f"Loaded image does not match the tested image: {archive}")
            images[architecture][name] = {"id": info["Id"], "config_digest": expected["config_digest"]}
    if set(images) != ARCHITECTURES:
        raise ValueError("Verified images are required for both amd64 and arm64")
    return images


def publish(directory: Path, run_id: str, app_tags: list[str], garage_tags: list[str]) -> None:
    tags_by_image = {"app": app_tags, "garage": garage_tags}
    repositories = {}
    for name, tags in tags_by_image.items():
        if not tags or len({tag.rsplit(":", 1)[0] for tag in tags}) != 1:
            raise ValueError(f"Release tags must share one repository for {name}")
        repositories[name] = tags[0].rsplit(":", 1)[0]
    # Validate every artifact before making any registry changes.
    images = load_verified(directory)
    verified_digests = {}
    for name in tags_by_image:
        repository = repositories[name]
        digests = []
        for architecture, tested in sorted(images.items()):
            reference = f"{repository}:candidate-{run_id}-{architecture}"
            docker("image", "tag", tested[name]["id"], reference)
            docker("image", "push", "--platform", f"linux/{architecture}", reference)
            descriptor = json.loads(docker("buildx", "imagetools", "inspect", reference, "--format", "{{json .Manifest}}"))
            digest = f"{repository}@{descriptor['digest']}"
            remote = json.loads(docker("buildx", "imagetools", "inspect", digest, "--raw"))
            if remote["config"]["digest"] != tested[name]["config_digest"]:
                raise ValueError(f"Registry image does not match the tested image: {reference}")
            digests.append(digest)
        verified_digests[name] = digests
    # Check all candidates before advancing any release tags.
    for name, tags in tags_by_image.items():
        digests = verified_digests[name]
        arguments = ["buildx", "imagetools", "create"]
        for tag in tags:
            arguments.extend(["--tag", tag])
        docker(*arguments, *digests)
        print(f"Published {name} from verified digests: {', '.join(digests)}", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)
    save = subcommands.add_parser("export")
    save.add_argument("directory", type=Path)
    save.add_argument("architecture", choices=sorted(ARCHITECTURES))
    promote = subcommands.add_parser("publish")
    promote.add_argument("directory", type=Path)
    promote.add_argument("run_id")
    args = parser.parse_args()
    if args.command == "export":
        export(args.directory, args.architecture)
    else:
        publish(args.directory, args.run_id,
                os.environ["APP_TAGS"].splitlines(), os.environ["GARAGE_TAGS"].splitlines())
