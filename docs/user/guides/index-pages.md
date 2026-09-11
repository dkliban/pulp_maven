# HTML Directory Index Pages

Pulp Maven pre-generates HTML directory index pages when a repository version is
finalized. Clients browsing a Maven repository at a directory URL (e.g.
`/pulp/maven/<base_path>/com/example/mylib/`) receive a pre-built listing page
served directly from storage, avoiding the more expensive on-demand query for
large repositories.

## How It Works

When `finalize_new_version` runs after content is added or removed, Pulp Maven
generates an `index.html` ContentArtifact at every ancestor directory path
touched by the change. The pulpcore content handler serves the pre-generated
page when it finds an `index.html` entry at the requested path; directories
unaffected by the version change continue to serve their existing pages.

Each page includes the name, size, and last-modified date of each direct child
entry (files and subdirectories), matching the information produced by the
on-demand fallback.

## Content Type

The generated pages are stored as `MavenIndexPage` content units (type
`maven.index-page`). They are visible through the standard Pulp content API
and are deduplication-keyed on `(path, sha256)` so identical pages are reused
across repository versions.
