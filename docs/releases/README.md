# Release notes archive

Historical release notes for the KaroX 3.x and 4.x stable lines. The repository
root keeps only the notes for the currently shipping stable version (`VERSION`)
and for the KaroX 5 line in progress; everything older lives here so the root
stays readable.

The release workflow (`.github/workflows/release.yml`) reads
`RELEASE_NOTES_v<VERSION>.md` from the repository root. A new stable release adds
its notes at the root and moves the previous stable notes into this directory in
the same change.

| Line | Files |
| --- | --- |
| 3.11 - 3.16 | `RELEASE_NOTES_v3.11.0.md` ... `RELEASE_NOTES_v3.16.2.md` |
| 4.0 - 4.1.3 | `RELEASE_NOTES_v4.0.0.md` ... `RELEASE_NOTES_v4.1.3.md` |

Current stable notes: `../../RELEASE_NOTES_v4.1.4.md`.
KaroX 5 release candidate notes: `../../RELEASE_NOTES_v5.0.0rc1.md`.
