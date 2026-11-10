import pytest

from vecten.stack.images import REQUIRED_PLATFORMS, missing_platforms

AMD64 = {"os": "linux", "architecture": "amd64"}
ARM64 = {"os": "linux", "architecture": "arm64"}
ATTESTATION = {
    "mediaType": "application/vnd.oci.image.manifest.v1+json",
    "digest": "sha256:a5aef377a3e2ad63dd25ce50d7be799f27745ffbf1b5b314af804a43846d6fab",
    "annotations": {
        "vnd.docker.reference.digest": "sha256:28c0c86c",
        "vnd.docker.reference.type": "attestation-manifest",
    },
    "platform": {"architecture": "unknown", "os": "unknown"},
}


def index(*platforms, attestations=1):
    entries = [
        {"mediaType": "application/vnd.oci.image.manifest.v1+json", "platform": p}
        for p in platforms
    ]
    return {
        "mediaType": "application/vnd.oci.image.index.v1+json",
        "digest": "sha256:846561d1",
        "manifests": entries + [ATTESTATION] * attestations,
    }


# The descriptor of ghcr.io/vecten/vecten-mcp:0.1.0rc17, as imagetools printed it.
RC17 = index(AMD64)


def test_required_platforms():
    assert REQUIRED_PLATFORMS == ("linux/amd64", "linux/arm64")


def test_rc17_lacks_arm64():
    assert missing_platforms(RC17) == ["linux/arm64"]


def test_both_platforms_with_attestations_pass():
    assert missing_platforms(index(AMD64, ARM64, attestations=2)) == []


def test_arm64_v8_counts_as_arm64():
    assert missing_platforms(index(AMD64, {**ARM64, "variant": "v8"})) == []


def test_another_arm_variant_is_not_arm64():
    assert missing_platforms(index(AMD64, {**ARM64, "variant": "v7"})) == ["linux/arm64"]


def test_attestations_are_not_platforms():
    assert missing_platforms(index()) == ["linux/amd64", "linux/arm64"]


def test_single_image_is_refused():
    single = {"mediaType": "application/vnd.oci.image.manifest.v1+json", "digest": "sha256:28"}
    with pytest.raises(ValueError, match="single-platform image"):
        missing_platforms(single)
