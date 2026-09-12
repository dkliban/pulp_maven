"""Tests for the repair_metadata and repair_index_pages repository actions."""

import hashlib
import uuid
from urllib.parse import urljoin
from xml.etree import ElementTree

import pytest

from pulp_maven.tests.functional.utils import download_file

# ---------------------------------------------------------------------------
# Helper: check if the storage backend is S3-compatible (boto3 required)
# ---------------------------------------------------------------------------


def _s3_client(pulp_settings):
    """Return a boto3 S3 client configured from pulp_settings, or None."""
    try:
        import boto3
        from botocore.config import Config
    except ImportError:
        return None, None

    backend = pulp_settings.STORAGES.get("default", {}).get("BACKEND", "")
    if "s3" not in backend.lower():
        return None, None

    opts = pulp_settings.STORAGES["default"].get("OPTIONS", {})
    endpoint = getattr(pulp_settings, "AWS_S3_ENDPOINT_URL", opts.get("endpoint_url"))
    access = getattr(pulp_settings, "AWS_ACCESS_KEY_ID", opts.get("access_key"))
    secret = getattr(pulp_settings, "AWS_SECRET_ACCESS_KEY", opts.get("secret_key"))
    bucket = getattr(pulp_settings, "AWS_STORAGE_BUCKET_NAME", opts.get("bucket_name"))
    style = getattr(pulp_settings, "AWS_S3_ADDRESSING_STYLE", opts.get("addressing_style", "auto"))

    client = boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=access,
        aws_secret_access_key=secret,
        config=Config(signature_version="s3v4", s3={"addressing_style": style}),
    )
    return client, bucket


def _uid():
    return uuid.uuid4().hex[:8]


@pytest.fixture
def populated_repo(
    maven_repo_factory,
    maven_distribution_factory,
    maven_artifact_api_client,
    maven_metadata_api_client,
    maven_repo_api_client,
    random_artifact_factory,
    monitor_task,
    distribution_base_url,
):
    """Factory that creates a repo with artifacts, removes metadata, and returns context.

    Args:
        artifact_specs: list of (group_prefix, artifact_id, versions) tuples.
            e.g., ``[("com", "mylib", ["1.0.0", "2.0.0"])]``

    Returns:
        (repo, base_url, uid) after all metadata has been stripped so
        ``repair_metadata`` has to regenerate it from scratch.
    """

    def _factory(artifact_specs):
        repo = maven_repo_factory()
        distro = maven_distribution_factory(repository=repo.pulp_href)
        base_url = distribution_base_url(distro.base_url)
        # uid is embedded in the group path (e.g. "com/{uid}/mylib/...") so that
        # each test run produces unique group_ids and avoids collisions.
        uid = _uid()

        content_hrefs = []
        for group_prefix, artifact_id, versions in artifact_specs:
            for version in versions:
                artifact = random_artifact_factory(size=64)
                content = maven_artifact_api_client.upload(
                    artifact=artifact.pulp_href,
                    relative_path=(
                        f"{group_prefix}/{uid}/{artifact_id}/{version}/{artifact_id}-{version}.jar"
                    ),
                )
                content_hrefs.append(content.pulp_href)

        monitor_task(
            maven_repo_api_client.modify(repo.pulp_href, {"add_content_units": content_hrefs}).task
        )

        # Remove auto-generated metadata so repair_metadata has to recreate it.
        repo = maven_repo_api_client.read(repo.pulp_href)
        metadata_hrefs = [
            m.pulp_href
            for m in maven_metadata_api_client.list(
                repository_version=repo.latest_version_href
            ).results
        ]
        if metadata_hrefs:
            monitor_task(
                maven_repo_api_client.modify(
                    repo.pulp_href, {"remove_content_units": metadata_hrefs}
                ).task
            )
        repo = maven_repo_api_client.read(repo.pulp_href)
        assert (
            maven_metadata_api_client.list(repository_version=repo.latest_version_href).count == 0
        )

        return repo, base_url, uid

    return _factory


@pytest.mark.parallel
def test_repair_metadata_restores_missing_metadata(
    populated_repo,
    maven_repo_api_client,
    monitor_task,
):
    """repair_metadata regenerates metadata that was removed from the repo."""
    repo, base_url, uid = populated_repo([("com", "repairlib", ["1.0.0", "2.0.0", "3.0.0"])])

    response = maven_repo_api_client.repair_metadata(repo.pulp_href)
    monitor_task(response.task)

    metadata_url = urljoin(base_url, f"com/{uid}/repairlib/maven-metadata.xml")
    downloaded = download_file(metadata_url)
    assert downloaded.response_obj.status == 200

    root = ElementTree.fromstring(downloaded.body)
    assert root.findtext("groupId") == f"com.{uid}"
    assert root.findtext("artifactId") == "repairlib"

    versioning = root.find("versioning")
    assert versioning.findtext("latest") == "3.0.0"
    assert versioning.findtext("release") == "3.0.0"

    versions = sorted(v.text for v in versioning.findall("versions/version"))
    assert versions == ["1.0.0", "2.0.0", "3.0.0"]


@pytest.mark.parallel
def test_repair_metadata_fixes_stale_metadata(
    maven_repo_factory,
    maven_distribution_factory,
    maven_artifact_api_client,
    maven_metadata_api_client,
    maven_repo_api_client,
    random_artifact_factory,
    monitor_task,
    distribution_base_url,
    tmp_path,
):
    """repair_metadata replaces stale metadata that only lists a subset of versions."""
    repo = maven_repo_factory()
    distro = maven_distribution_factory(repository=repo.pulp_href)
    base_url = distribution_base_url(distro.base_url)
    uid = _uid()

    content_hrefs = []
    for version in ["1.0.0", "2.0.0", "3.0.0"]:
        artifact = random_artifact_factory(size=64)
        content = maven_artifact_api_client.upload(
            artifact=artifact.pulp_href,
            relative_path=f"com/{uid}/stalelib/{version}/stalelib-{version}.jar",
        )
        content_hrefs.append(content.pulp_href)

    monitor_task(
        maven_repo_api_client.modify(repo.pulp_href, {"add_content_units": content_hrefs}).task
    )

    stale_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f"<metadata><groupId>com.{uid}</groupId>"
        "<artifactId>stalelib</artifactId>"
        "<versioning><latest>1.0.0</latest><release>1.0.0</release>"
        "<versions><version>1.0.0</version></versions>"
        "<lastUpdated>20200101000000</lastUpdated></versioning></metadata>\n"
    )
    stale_file = tmp_path / "maven-metadata.xml"
    stale_file.write_text(stale_xml)

    stale_metadata = maven_metadata_api_client.upload(
        relative_path=f"com/{uid}/stalelib/maven-metadata.xml",
        file=str(stale_file),
    )

    monitor_task(
        maven_repo_api_client.modify(
            repo.pulp_href, {"add_content_units": [stale_metadata.pulp_href]}
        ).task
    )

    metadata_url = urljoin(base_url, f"com/{uid}/stalelib/maven-metadata.xml")
    downloaded = download_file(metadata_url)
    root = ElementTree.fromstring(downloaded.body)
    versions = [v.text for v in root.findall(".//versions/version")]
    assert versions == ["1.0.0"], "Metadata should be stale before repair"

    response = maven_repo_api_client.repair_metadata(repo.pulp_href)
    monitor_task(response.task)

    downloaded = download_file(metadata_url)
    root = ElementTree.fromstring(downloaded.body)
    versions = sorted(v.text for v in root.findall(".//versions/version"))
    assert versions == ["1.0.0", "2.0.0", "3.0.0"]
    assert root.find("versioning").findtext("latest") == "3.0.0"


@pytest.mark.parallel
def test_repair_metadata_creates_new_version(
    populated_repo,
    maven_metadata_api_client,
    maven_repo_api_client,
    monitor_task,
):
    """repair_metadata creates a new repository version with metadata content."""
    repo, _, _ = populated_repo([("com", "verlib", ["1.0.0"])])
    version_before = repo.latest_version_href

    response = maven_repo_api_client.repair_metadata(repo.pulp_href)
    monitor_task(response.task)

    repo = maven_repo_api_client.read(repo.pulp_href)
    assert repo.latest_version_href != version_before

    metadata_list = maven_metadata_api_client.list(repository_version=repo.latest_version_href)
    assert metadata_list.count == 5  # xml + 3 checksums + prefixes.txt


@pytest.mark.parallel
def test_repair_metadata_checksums_match(
    populated_repo,
    maven_repo_api_client,
    monitor_task,
):
    """Checksum files match the repaired maven-metadata.xml content."""
    repo, base_url, uid = populated_repo([("com", "cksumlib", ["1.0.0"])])

    response = maven_repo_api_client.repair_metadata(repo.pulp_href)
    monitor_task(response.task)

    metadata_url = urljoin(base_url, f"com/{uid}/cksumlib/maven-metadata.xml")
    metadata_download = download_file(metadata_url)
    metadata_body = metadata_download.body

    for ext, hash_func in [
        (".md5", hashlib.md5),
        (".sha1", hashlib.sha1),
        (".sha256", hashlib.sha256),
    ]:
        checksum_url = urljoin(base_url, f"com/{uid}/cksumlib/maven-metadata.xml{ext}")
        checksum_download = download_file(checksum_url)
        assert checksum_download.response_obj.status == 200
        expected = hash_func(metadata_body).hexdigest()
        assert checksum_download.body.decode().strip() == expected, (
            f"Checksum mismatch for maven-metadata.xml{ext}"
        )


@pytest.mark.parallel
def test_repair_metadata_multiple_groups(
    populated_repo,
    maven_repo_api_client,
    monitor_task,
):
    """repair_metadata generates separate metadata for each (group_id, artifact_id) pair."""
    repo, base_url, uid = populated_repo([("com", "lib-x", ["1.0.0"]), ("org", "lib-y", ["2.0.0"])])

    response = maven_repo_api_client.repair_metadata(repo.pulp_href)
    monitor_task(response.task)

    dl_x = download_file(urljoin(base_url, f"com/{uid}/lib-x/maven-metadata.xml"))
    root_x = ElementTree.fromstring(dl_x.body)
    assert root_x.findtext("groupId") == f"com.{uid}"
    assert root_x.findtext("artifactId") == "lib-x"
    assert [v.text for v in root_x.findall(".//versions/version")] == ["1.0.0"]

    dl_y = download_file(urljoin(base_url, f"org/{uid}/lib-y/maven-metadata.xml"))
    root_y = ElementTree.fromstring(dl_y.body)
    assert root_y.findtext("groupId") == f"org.{uid}"
    assert root_y.findtext("artifactId") == "lib-y"
    assert [v.text for v in root_y.findall(".//versions/version")] == ["2.0.0"]


@pytest.mark.parallel
def test_repair_metadata_snapshot_handling(
    populated_repo,
    maven_repo_api_client,
    monitor_task,
):
    """repair_metadata sets <release> to the latest non-SNAPSHOT version."""
    repo, base_url, uid = populated_repo([("com", "snaplib", ["1.0.0", "2.0.0-SNAPSHOT"])])

    response = maven_repo_api_client.repair_metadata(repo.pulp_href)
    monitor_task(response.task)

    metadata_url = urljoin(base_url, f"com/{uid}/snaplib/maven-metadata.xml")
    downloaded = download_file(metadata_url)
    root = ElementTree.fromstring(downloaded.body)

    versioning = root.find("versioning")
    assert versioning.findtext("latest") == "2.0.0-SNAPSHOT"
    assert versioning.findtext("release") == "1.0.0"

    versions = sorted(v.text for v in versioning.findall("versions/version"))
    assert versions == ["1.0.0", "2.0.0-SNAPSHOT"]


@pytest.mark.parallel
def test_repair_metadata_with_existing_metadata(
    maven_repo_factory,
    maven_distribution_factory,
    maven_artifact_api_client,
    maven_metadata_api_client,
    maven_repo_api_client,
    random_artifact_factory,
    monitor_task,
    distribution_base_url,
):
    """repair_metadata succeeds when the repo already has auto-generated metadata.

    Regression test for https://github.com/pulp/pulp_maven/issues/395:
    finalize_new_version called _generate_metadata redundantly after
    repair_metadata had already placed metadata in the version, causing an
    AssertionError in _compute_counts.
    """
    repo = maven_repo_factory()
    distro = maven_distribution_factory(repository=repo.pulp_href)
    base_url = distribution_base_url(distro.base_url)
    uid = _uid()

    content_hrefs = []
    for version in ["1.0.0", "2.0.0"]:
        artifact = random_artifact_factory(size=64)
        content = maven_artifact_api_client.upload(
            artifact=artifact.pulp_href,
            relative_path=f"com/{uid}/existlib/{version}/existlib-{version}.jar",
        )
        content_hrefs.append(content.pulp_href)

    monitor_task(
        maven_repo_api_client.modify(repo.pulp_href, {"add_content_units": content_hrefs}).task
    )

    # Verify metadata was auto-generated by finalize_new_version.
    repo = maven_repo_api_client.read(repo.pulp_href)
    meta_count = maven_metadata_api_client.list(repository_version=repo.latest_version_href).count
    assert meta_count > 0, "Expected auto-generated metadata before repair"

    # Call repair_metadata — this must not raise AssertionError.
    response = maven_repo_api_client.repair_metadata(repo.pulp_href)
    monitor_task(response.task)

    repo = maven_repo_api_client.read(repo.pulp_href)
    metadata_url = urljoin(base_url, f"com/{uid}/existlib/maven-metadata.xml")
    downloaded = download_file(metadata_url)
    assert downloaded.response_obj.status == 200

    root = ElementTree.fromstring(downloaded.body)
    assert root.findtext("groupId") == f"com.{uid}"
    assert root.findtext("artifactId") == "existlib"

    versions = sorted(v.text for v in root.findall(".//versions/version"))
    assert versions == ["1.0.0", "2.0.0"]


@pytest.mark.parallel
def test_repair_metadata_removes_orphaned_metadata(
    maven_repo_factory,
    maven_metadata_api_client,
    maven_repo_api_client,
    monitor_task,
    tmp_path,
):
    """repair_metadata removes metadata when no artifacts exist for the pair.

    Regression test for https://github.com/pulp/pulp_maven/issues/394:
    repair_metadata returned early when all_pairs was empty, leaving
    orphaned metadata in the repository.
    """
    repo = maven_repo_factory()
    uid = _uid()

    # Upload metadata directly without any corresponding artifact.
    orphan_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f"<metadata><groupId>com.{uid}</groupId>"
        "<artifactId>orphanlib</artifactId>"
        "<versioning><latest>1.0.0</latest><release>1.0.0</release>"
        "<versions><version>1.0.0</version></versions>"
        "<lastUpdated>20200101000000</lastUpdated></versioning></metadata>\n"
    )
    orphan_file = tmp_path / "maven-metadata.xml"
    orphan_file.write_text(orphan_xml)

    orphan_metadata = maven_metadata_api_client.upload(
        relative_path=f"com/{uid}/orphanlib/maven-metadata.xml",
        file=str(orphan_file),
    )

    monitor_task(
        maven_repo_api_client.modify(
            repo.pulp_href, {"add_content_units": [orphan_metadata.pulp_href]}
        ).task
    )

    repo = maven_repo_api_client.read(repo.pulp_href)
    assert maven_metadata_api_client.list(repository_version=repo.latest_version_href).count > 0, (
        "Orphaned metadata should be present before repair"
    )

    response = maven_repo_api_client.repair_metadata(repo.pulp_href)
    monitor_task(response.task)

    repo = maven_repo_api_client.read(repo.pulp_href)
    assert maven_metadata_api_client.list(repository_version=repo.latest_version_href).count == 0, (
        "Orphaned metadata should be removed after repair"
    )


@pytest.mark.parallel
def test_repair_metadata_removes_orphaned_version_level_metadata(
    maven_repo_factory,
    maven_artifact_api_client,
    maven_metadata_api_client,
    maven_repo_api_client,
    random_artifact_factory,
    monitor_task,
    tmp_path,
):
    """repair_metadata removes version-level metadata for orphaned pairs.

    Regression test for https://github.com/pulp/pulp_maven/issues/394:
    the stale filter only matched version=None, leaving version-level
    metadata behind when its artifacts were removed.
    """
    repo = maven_repo_factory()
    uid = _uid()

    artifact = random_artifact_factory(size=64)
    content = maven_artifact_api_client.upload(
        artifact=artifact.pulp_href,
        relative_path=f"com/{uid}/verorphan/1.0.0/verorphan-1.0.0.jar",
    )

    # Upload version-level maven-metadata.xml (version != None).
    ver_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f"<metadata><groupId>com.{uid}</groupId>"
        "<artifactId>verorphan</artifactId>"
        "<version>1.0.0</version></metadata>\n"
    )
    ver_file = tmp_path / "maven-metadata.xml"
    ver_file.write_text(ver_xml)

    ver_metadata = maven_metadata_api_client.upload(
        relative_path=f"com/{uid}/verorphan/1.0.0/maven-metadata.xml",
        file=str(ver_file),
    )

    # Add both artifact and version-level metadata.
    monitor_task(
        maven_repo_api_client.modify(
            repo.pulp_href,
            {"add_content_units": [content.pulp_href, ver_metadata.pulp_href]},
        ).task
    )

    # Remove the artifact. finalize_new_version cleans up version=None
    # metadata but leaves version-level metadata behind.
    monitor_task(
        maven_repo_api_client.modify(
            repo.pulp_href, {"remove_content_units": [content.pulp_href]}
        ).task
    )

    repo = maven_repo_api_client.read(repo.pulp_href)
    assert maven_metadata_api_client.list(repository_version=repo.latest_version_href).count > 0, (
        "Version-level metadata should survive artifact removal"
    )

    response = maven_repo_api_client.repair_metadata(repo.pulp_href)
    monitor_task(response.task)

    repo = maven_repo_api_client.read(repo.pulp_href)
    assert maven_metadata_api_client.list(repository_version=repo.latest_version_href).count == 0, (
        "All orphaned metadata (including version-level) should be removed"
    )


@pytest.mark.parallel
def test_repair_metadata_generates_version_level_metadata(
    populated_repo,
    maven_metadata_api_client,
    maven_repo_api_client,
    monitor_task,
):
    """repair_metadata regenerates version-level SNAPSHOT metadata."""
    repo, base_url, uid = populated_repo([("com", "snaprepair", ["1.0.0", "2.0.0-SNAPSHOT"])])

    response = maven_repo_api_client.repair_metadata(repo.pulp_href)
    monitor_task(response.task)

    repo = maven_repo_api_client.read(repo.pulp_href)

    # 4 repo-level + 4 version-level + prefixes.txt = 9
    metadata_list = maven_metadata_api_client.list(repository_version=repo.latest_version_href)
    assert metadata_list.count == 9

    ver_url = urljoin(base_url, f"com/{uid}/snaprepair/2.0.0-SNAPSHOT/maven-metadata.xml")
    downloaded = download_file(ver_url)
    assert downloaded.response_obj.status == 200

    root = ElementTree.fromstring(downloaded.body)
    assert root.findtext("groupId") == f"com.{uid}"
    assert root.findtext("artifactId") == "snaprepair"
    assert root.findtext("version") == "2.0.0-SNAPSHOT"

    versioning = root.find("versioning")
    assert versioning.find("snapshot").findtext("localCopy") == "true"

    sv_list = versioning.findall("snapshotVersions/snapshotVersion")
    assert len(sv_list) == 1
    assert sv_list[0].findtext("extension") == "jar"
    assert sv_list[0].findtext("value") == "2.0.0-SNAPSHOT"


@pytest.mark.parallel
def test_repair_metadata_replaces_stale_version_level_metadata(
    maven_repo_factory,
    maven_distribution_factory,
    maven_artifact_api_client,
    maven_metadata_api_client,
    maven_repo_api_client,
    random_artifact_factory,
    monitor_task,
    distribution_base_url,
    tmp_path,
):
    """repair_metadata replaces stale version-level SNAPSHOT metadata."""
    repo = maven_repo_factory()
    distro = maven_distribution_factory(repository=repo.pulp_href)
    base_url = distribution_base_url(distro.base_url)
    uid = _uid()

    content_hrefs = []
    for name in ["snapstale-1.0-SNAPSHOT.jar", "snapstale-1.0-SNAPSHOT.pom"]:
        artifact = random_artifact_factory(size=64)
        content = maven_artifact_api_client.upload(
            artifact=artifact.pulp_href,
            relative_path=f"com/{uid}/snapstale/1.0-SNAPSHOT/{name}",
        )
        content_hrefs.append(content.pulp_href)

    monitor_task(
        maven_repo_api_client.modify(repo.pulp_href, {"add_content_units": content_hrefs}).task
    )

    # Upload stale version-level metadata that only lists jar (missing pom)
    stale_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f"<metadata><groupId>com.{uid}</groupId>"
        "<artifactId>snapstale</artifactId>"
        "<version>1.0-SNAPSHOT</version>"
        "<versioning><snapshot><localCopy>true</localCopy></snapshot>"
        "<snapshotVersions>"
        "<snapshotVersion><extension>jar</extension>"
        "<value>1.0-SNAPSHOT</value><updated>20200101000000</updated>"
        "</snapshotVersion>"
        "</snapshotVersions>"
        "<lastUpdated>20200101000000</lastUpdated></versioning></metadata>\n"
    )
    stale_file = tmp_path / "maven-metadata.xml"
    stale_file.write_text(stale_xml)

    stale_metadata = maven_metadata_api_client.upload(
        relative_path=f"com/{uid}/snapstale/1.0-SNAPSHOT/maven-metadata.xml",
        file=str(stale_file),
    )
    monitor_task(
        maven_repo_api_client.modify(
            repo.pulp_href, {"add_content_units": [stale_metadata.pulp_href]}
        ).task
    )

    # Verify the stale metadata is served
    ver_url = urljoin(base_url, f"com/{uid}/snapstale/1.0-SNAPSHOT/maven-metadata.xml")
    downloaded = download_file(ver_url)
    root = ElementTree.fromstring(downloaded.body)
    sv_list = root.findall(".//snapshotVersions/snapshotVersion")
    assert len(sv_list) == 1, "Should have stale metadata with only jar"

    # Run repair
    response = maven_repo_api_client.repair_metadata(repo.pulp_href)
    monitor_task(response.task)

    # Verify the repaired metadata has both jar and pom
    downloaded = download_file(ver_url)
    root = ElementTree.fromstring(downloaded.body)
    sv_list = root.findall(".//snapshotVersions/snapshotVersion")
    extensions = sorted(sv.findtext("extension") for sv in sv_list)
    assert extensions == ["jar", "pom"]


@pytest.mark.parallel
def test_repair_metadata_empty_repo_noop(
    maven_repo_factory,
    maven_repo_api_client,
    monitor_task,
):
    """repair_metadata on an empty repo does not create a new version."""
    repo = maven_repo_factory()
    version_before = repo.latest_version_href

    response = maven_repo_api_client.repair_metadata(repo.pulp_href)
    monitor_task(response.task)

    repo = maven_repo_api_client.read(repo.pulp_href)
    assert repo.latest_version_href == version_before


@pytest.mark.parallel
def test_repair_index_pages_generates_correct_listings(
    pulpcore_bindings,
    maven_repo_factory,
    maven_distribution_factory,
    maven_artifact_api_client,
    maven_repo_api_client,
    random_artifact_factory,
    monitor_task,
    distribution_base_url,
):
    """repair_index_pages produces correct HTML listings at every directory level.

    Uses artifacts at two version directories under the same artifact_id to
    exercise the single-query grouping path: multiple ContentArtifacts map to
    the same directory entry name (e.g. ``1.0.0/`` and ``2.0.0/`` both contribute
    to the ``rip-lib/`` listing) and multiple files per version directory map to
    distinct file entries.
    """
    repo = maven_repo_factory()
    distro = maven_distribution_factory(repository=repo.pulp_href)
    base_url = distribution_base_url(distro.base_url)
    uid = uuid.uuid4().hex[:8]

    # Two versions, two files each — exercises multi-CA directory grouping.
    content_hrefs = []
    for version in ["1.0.0", "2.0.0"]:
        for ext in ["jar", "pom"]:
            a = random_artifact_factory(size=64)
            c = maven_artifact_api_client.upload(
                artifact=a.pulp_href,
                relative_path=f"com/{uid}/rip-lib/{version}/rip-lib-{version}.{ext}",
            )
            content_hrefs.append(c.pulp_href)

    monitor_task(
        maven_repo_api_client.modify(repo.pulp_href, {"add_content_units": content_hrefs}).task
    )
    repo = maven_repo_api_client.read(repo.pulp_href)

    # Capture the reference HTML generated by finalize_new_version.
    parent_ref = download_file(urljoin(base_url, f"com/{uid}/rip-lib/")).body
    v1_ref = download_file(urljoin(base_url, f"com/{uid}/rip-lib/1.0.0/")).body
    v2_ref = download_file(urljoin(base_url, f"com/{uid}/rip-lib/2.0.0/")).body

    # Run repair_index_pages — this exercises the refactored single-query path.
    response = maven_repo_api_client.repair_index_pages(repo.pulp_href)
    monitor_task(response.task)
    repo = maven_repo_api_client.read(repo.pulp_href)

    # Index pages must still exist after repair.
    pages = pulpcore_bindings.ContentApi.list(
        repository_version=repo.latest_version_href,
        pulp_type__in=["maven.index-page"],
        limit=100,
    )
    assert pages.count >= 5, f"Expected ≥5 index pages after repair, got {pages.count}"

    # The HTML produced by repair must match the HTML finalize_new_version produced.
    # Any correctness regression in the single-query grouping path would cause a mismatch.
    parent_after = download_file(urljoin(base_url, f"com/{uid}/rip-lib/")).body
    v1_after = download_file(urljoin(base_url, f"com/{uid}/rip-lib/1.0.0/")).body
    v2_after = download_file(urljoin(base_url, f"com/{uid}/rip-lib/2.0.0/")).body

    assert parent_after == parent_ref, (
        "rip-lib/ HTML after repair differs from finalize_new_version output — "
        "the single-query grouping path produced a different listing"
    )
    assert v1_after == v1_ref, (
        "rip-lib/1.0.0/ HTML after repair differs from finalize_new_version output"
    )
    assert v2_after == v2_ref, (
        "rip-lib/2.0.0/ HTML after repair differs from finalize_new_version output"
    )

    # Sanity-check the content of the regenerated pages.
    parent_html = parent_after.decode()
    assert "1.0.0/" in parent_html, "1.0.0/ missing from rip-lib/ page after repair"
    assert "2.0.0/" in parent_html, "2.0.0/ missing from rip-lib/ page after repair"
    assert "rip-lib-1.0.0.jar" not in parent_html, (
        "File from child directory appears in parent — incorrect grouping"
    )

    v1_html = v1_after.decode()
    assert "rip-lib-1.0.0.jar" in v1_html, "jar missing from 1.0.0/ page after repair"
    assert "rip-lib-1.0.0.pom" in v1_html, "pom missing from 1.0.0/ page after repair"
    assert "2.0.0/" not in v1_html, "sibling version directory appears in 1.0.0/ page"


@pytest.mark.parallel
def test_repair_index_pages_idempotent(
    pulpcore_bindings,
    maven_repo_factory,
    maven_distribution_factory,
    maven_artifact_api_client,
    maven_repo_api_client,
    random_artifact_factory,
    monitor_task,
    distribution_base_url,
):
    """repair_index_pages is idempotent: a second run produces identical HTML.

    This exercises the batch sha256 existence-check path in _save_artifacts_batch:
    - First run: all artifacts are new → parallel uploads fire, pages generated.
    - Second run: all artifacts already exist → batch IN query returns everything,
      no uploads needed, but pages must still be correctly attached to the new version.

    If the batch existence-check is broken (e.g. wrong domain filter, sha256 mismatch)
    the second run would regenerate content with different sha256s, causing the HTML
    comparison to fail.
    """
    repo = maven_repo_factory()
    distro = maven_distribution_factory(repository=repo.pulp_href)
    base_url = distribution_base_url(distro.base_url)
    uid = uuid.uuid4().hex[:8]

    content_hrefs = []
    for version in ["1.0.0", "2.0.0"]:
        for ext in ["jar", "pom"]:
            a = random_artifact_factory(size=64)
            c = maven_artifact_api_client.upload(
                artifact=a.pulp_href,
                relative_path=f"com/{uid}/idem-lib/{version}/idem-lib-{version}.{ext}",
            )
            content_hrefs.append(c.pulp_href)

    monitor_task(
        maven_repo_api_client.modify(repo.pulp_href, {"add_content_units": content_hrefs}).task
    )

    # First repair run — all artifacts are new, parallel uploads fire.
    monitor_task(maven_repo_api_client.repair_index_pages(repo.pulp_href).task)
    repo = maven_repo_api_client.read(repo.pulp_href)

    html_after_first = {
        path: download_file(urljoin(base_url, path)).body
        for path in [
            f"com/{uid}/idem-lib/",
            f"com/{uid}/idem-lib/1.0.0/",
            f"com/{uid}/idem-lib/2.0.0/",
        ]
    }

    pages_after_first = pulpcore_bindings.ContentApi.list(
        repository_version=repo.latest_version_href,
        pulp_type__in=["maven.index-page"],
        limit=100,
    )
    assert pages_after_first.count > 0

    # Second repair run — all artifacts already exist, batch existence-check must
    # find them and attach them without uploading.
    monitor_task(maven_repo_api_client.repair_index_pages(repo.pulp_href).task)
    repo = maven_repo_api_client.read(repo.pulp_href)

    for path, html_first in html_after_first.items():
        html_second = download_file(urljoin(base_url, path)).body
        assert html_second == html_first, (
            f"{path} HTML changed between first and second repair — "
            "batch existence-check may be returning wrong artifacts"
        )


@pytest.mark.parallel
def test_repair_index_pages_after_stripping(
    pulpcore_bindings,
    maven_repo_factory,
    maven_distribution_factory,
    maven_artifact_api_client,
    maven_repo_api_client,
    random_artifact_factory,
    monitor_task,
    distribution_base_url,
):
    """repair_index_pages re-creates pages that were removed from the version.

    Simulates a repository whose pre-generated index pages were stripped (e.g. by
    the operator, or because they were created with a buggy code path).  After
    stripping, repair must re-attach or regenerate the pages so they are served
    correctly from the distribution.

    This test guards against the ContextVar domain context bug (issue #465): when
    repair uploads artifacts in parallel via ThreadPoolExecutor, worker threads must
    use the correct domain so files land in the right S3 bucket.  A broken
    implementation stores files in the default domain's bucket, causing
    FileNotFoundError when the content app tries to serve them from the
    repository's domain bucket.
    """
    repo = maven_repo_factory()
    distro = maven_distribution_factory(repository=repo.pulp_href)
    base_url = distribution_base_url(distro.base_url)
    uid = uuid.uuid4().hex[:8]

    content_hrefs = []
    for version in ["1.0.0", "2.0.0"]:
        for ext in ["jar", "pom"]:
            a = random_artifact_factory(size=64)
            c = maven_artifact_api_client.upload(
                artifact=a.pulp_href,
                relative_path=f"com/{uid}/strip-lib/{version}/strip-lib-{version}.{ext}",
            )
            content_hrefs.append(c.pulp_href)

    monitor_task(
        maven_repo_api_client.modify(repo.pulp_href, {"add_content_units": content_hrefs}).task
    )
    repo = maven_repo_api_client.read(repo.pulp_href)

    # Strip all pre-generated index pages from the version, simulating a repo
    # that predates the pre-generation feature or had its pages deleted.
    index_pages = pulpcore_bindings.ContentApi.list(
        repository_version=repo.latest_version_href,
        pulp_type__in=["maven.index-page"],
        limit=100,
    )
    if index_pages.count > 0:
        index_hrefs = [p.pulp_href for p in index_pages.results]
        monitor_task(
            maven_repo_api_client.modify(repo.pulp_href, {"remove_content_units": index_hrefs}).task
        )
        repo = maven_repo_api_client.read(repo.pulp_href)

    # Confirm pages are gone from the version.
    pages_after_strip = pulpcore_bindings.ContentApi.list(
        repository_version=repo.latest_version_href,
        pulp_type__in=["maven.index-page"],
    )
    assert pages_after_strip.count == 0, "Index pages still present after stripping"

    # Run repair — must regenerate pages and attach them to a new version.
    monitor_task(maven_repo_api_client.repair_index_pages(repo.pulp_href).task)
    repo = maven_repo_api_client.read(repo.pulp_href)

    pages_after_repair = pulpcore_bindings.ContentApi.list(
        repository_version=repo.latest_version_href,
        pulp_type__in=["maven.index-page"],
        limit=100,
    )
    assert pages_after_repair.count > 0, "No index pages after repair"

    # Pages must be downloadable and contain the expected entries.
    # If artifacts were stored in the wrong domain (issue #465), this would
    # raise FileNotFoundError and the test would fail with a 500 or exception.
    parent_html = download_file(urljoin(base_url, f"com/{uid}/strip-lib/")).body.decode()
    assert "1.0.0/" in parent_html, "1.0.0/ missing from strip-lib/ page after repair"
    assert "2.0.0/" in parent_html, "2.0.0/ missing from strip-lib/ page after repair"

    v1_html = download_file(urljoin(base_url, f"com/{uid}/strip-lib/1.0.0/")).body.decode()
    assert "strip-lib-1.0.0.jar" in v1_html, "jar missing from 1.0.0/ page after repair"
    assert "strip-lib-1.0.0.pom" in v1_html, "pom missing from 1.0.0/ page after repair"


@pytest.mark.parallel
def test_repair_index_pages_domain_context(
    pulpcore_bindings,
    pulp_settings,
    domain_factory,
    maven_repo_factory,
    maven_distribution_factory,
    maven_artifact_api_client,
    maven_repo_api_client,
    random_artifact_factory,
    monitor_task,
    distribution_base_url,
):
    """repair_index_pages stores artifacts in the correct domain's S3 bucket.

    When repair uploads artifacts in parallel via ThreadPoolExecutor, each worker
    thread must have the correct domain context so get_artifact_path() generates
    a path that includes the domain UUID (e.g. artifact/<uuid>/ab/cd...) rather
    than the default-domain path (artifact/ab/cd...).

    This test is only meaningful with DOMAIN_ENABLED=True and S3 storage.
    domain_factory auto-skips if domains are disabled; _s3_client skips if S3
    is not configured.

    Failure mode of the bug (issue #465):
        Artifacts are stored at artifact/<sha256[:2]>/<sha256[2:]> (default
        domain path, no UUID) instead of artifact/<domain-uuid>/...  The content
        app then looks for the file in the correct domain's bucket and raises
        FileNotFoundError.
    """
    s3, bucket = _s3_client(pulp_settings)
    if s3 is None:
        pytest.skip("S3 storage not configured — cannot verify S3 object path")

    # Create a non-default domain.  domain_factory auto-skips if DOMAIN_ENABLED=False.
    domain = domain_factory()
    domain_uuid = domain.pulp_href.rstrip("/").split("/")[-1]

    uid = uuid.uuid4().hex[:8]
    repo = maven_repo_factory(pulp_domain=domain.name)
    distro = maven_distribution_factory(repository=repo.pulp_href, pulp_domain=domain.name)
    base_url = distribution_base_url(distro.base_url)

    content_hrefs = []
    for version in ["1.0.0", "2.0.0"]:
        for ext in ["jar", "pom"]:
            a = random_artifact_factory(size=64)
            c = maven_artifact_api_client.upload(
                artifact=a.pulp_href,
                relative_path=f"com/{uid}/ctx-lib/{version}/ctx-lib-{version}.{ext}",
                pulp_domain=domain.name,
            )
            content_hrefs.append(c.pulp_href)

    monitor_task(
        maven_repo_api_client.modify(repo.pulp_href, {"add_content_units": content_hrefs}).task
    )

    # Run repair — this is where the ContextVar domain context propagation matters.
    monitor_task(maven_repo_api_client.repair_index_pages(repo.pulp_href).task)

    # Download a directory listing — if the artifact was stored in the wrong domain
    # this would raise FileNotFoundError (500 error).
    parent_html = download_file(urljoin(base_url, f"com/{uid}/ctx-lib/")).body
    assert b"1.0.0/" in parent_html and b"2.0.0/" in parent_html

    # Compute the sha256 of the generated HTML to derive the expected S3 key.
    sha256 = hashlib.sha256(parent_html).hexdigest()
    correct_key = f"artifact/{domain_uuid}/{sha256[:2]}/{sha256[2:]}"
    wrong_key = f"artifact/{sha256[:2]}/{sha256[2:]}"

    # The artifact MUST be at the domain-specific path (contains the UUID).
    try:
        s3.head_object(Bucket=bucket, Key=correct_key)
    except Exception:
        pytest.fail(
            f"Artifact not found at domain-specific S3 key '{correct_key}'. "
            f"Domain context was not propagated to ThreadPoolExecutor workers (issue #465)."
        )

    # The artifact must NOT be at the default-domain path (no UUID prefix).
    # If the bug were present the file would be here instead of the domain path.
    try:
        s3.head_object(Bucket=bucket, Key=wrong_key)
        pytest.fail(
            f"Artifact found at default-domain path '{wrong_key}' — "
            f"it should be at the domain-specific path '{correct_key}'."
        )
    except Exception:
        pass  # Expected: file should not be in the default-domain location.
