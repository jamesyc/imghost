import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("verified_images", Path(__file__).parents[1] / "docker/verified_images.py")
promotion = importlib.util.module_from_spec(spec)
spec.loader.exec_module(promotion)


def artifacts(directory):
    expected = {}
    for arch in sorted(promotion.ARCHITECTURES):
        folder = directory / arch
        folder.mkdir()
        manifest = {"architecture": arch, "images": {}}
        for name in promotion.IMAGES:
            archive = folder / f"{name}.tar"
            archive.write_bytes(f"verified {name} {arch}".encode())
            image_id = "sha256:" + hashlib.sha256(f"{name} {arch}".encode()).hexdigest()
            expected[(arch, name)] = image_id
            manifest["images"][name] = {"config_digest": image_id, "sha256": promotion.checksum(archive)}
        (folder / "images.json").write_text(json.dumps(manifest))
    return expected


def store_id(config_id, containerd):
    return "sha256:" + hashlib.sha256(config_id.encode()).hexdigest() if containerd else config_id


def mock_docker(monkeypatch, expected, *, corrupt_image=False, containerd=False, corrupt_registry=False):
    commands = []
    loaded = {}
    pushed = {}
    configurations = {}

    def command(*args):
        commands.append(args)
        if args[:2] == ("image", "load"):
            archive = Path(args[-1])
            name, arch = archive.stem, archive.parent.name
            config_id = expected[(arch, name)]
            local_id = store_id(config_id, containerd)
            info = {"Id": local_id, "Architecture": arch, "Os": "linux"}
            if containerd:
                info["Descriptor"] = {"annotations": {"config.digest": config_id}}
            loaded[promotion.IMAGES[name]] = info
            configurations[local_id] = config_id
            return "loaded"
        if args[:2] == ("image", "inspect"):
            info = dict(loaded[args[2]])
            if corrupt_image:
                info["Id"] = "sha256:unexpected"
            return json.dumps([info])
        if args[:2] == ("image", "tag"):
            pushed[args[3]] = args[2]
        if args[:3] == ("buildx", "imagetools", "inspect"):
            if args[-1] == "--raw":
                reference = next(ref for ref in pushed if hashlib.sha256(ref.encode()).hexdigest() == args[3].split("@sha256:")[1])
                digest = "sha256:unexpected" if corrupt_registry else configurations[pushed[reference]]
                return json.dumps({"config": {"digest": digest}})
            return json.dumps({"digest": "sha256:" + hashlib.sha256(args[3].encode()).hexdigest()})
        return ""

    monkeypatch.setattr(promotion, "docker", command)
    return commands


@pytest.mark.parametrize("containerd", [False, True])
def test_promotion_pushes_tested_image_ids_and_merges_only_their_digests(tmp_path, monkeypatch, containerd) -> None:
    expected = artifacts(tmp_path)
    commands = mock_docker(monkeypatch, expected, containerd=containerd)
    promotion.publish(tmp_path, "123-1", ["registry/app:latest", "registry/app:release"], ["registry/garage:latest"])
    tagged_ids = {args[2] for args in commands if args[:2] == ("image", "tag")}
    assert tagged_ids == {store_id(value, containerd) for value in expected.values()}
    merges = [args for args in commands if args[:3] == ("buildx", "imagetools", "create")]
    assert len(merges) == 2
    assert all("@sha256:" in ref for args in merges for ref in args[-2:])
    assert not any("build" in args for args in commands)
    last_push = max(index for index, args in enumerate(commands) if args[:2] == ("image", "push"))
    assert all(commands.index(merge) > last_push for merge in merges)


def test_registry_config_mismatch_prevents_publishing_release_tags(tmp_path, monkeypatch) -> None:
    expected = artifacts(tmp_path)
    commands = mock_docker(monkeypatch, expected, corrupt_registry=True)
    with pytest.raises(ValueError, match="Registry image does not match"):
        promotion.publish(tmp_path, "123-1", ["registry/app:latest"], ["registry/garage:latest"])
    assert not any(args[:3] == ("buildx", "imagetools", "create") for args in commands)


@pytest.mark.parametrize("failure", ["checksum", "image_id", "missing_architecture"])
def test_invalid_artifacts_fail_before_any_registry_writes(tmp_path, monkeypatch, failure) -> None:
    expected = artifacts(tmp_path)
    commands = mock_docker(monkeypatch, expected, corrupt_image=failure == "image_id")
    if failure == "checksum":
        (tmp_path / "arm64" / "garage.tar").write_bytes(b"changed after verification")
    elif failure == "missing_architecture":
        (tmp_path / "arm64" / "images.json").unlink()
    with pytest.raises(ValueError):
        promotion.publish(tmp_path, "123-1", ["registry/app:latest"], ["registry/garage:latest"])
    assert not any(args[:2] in {("image", "tag"), ("image", "push")} for args in commands)
    assert not any(args[:3] == ("buildx", "imagetools", "create") for args in commands)
