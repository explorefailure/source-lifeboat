# Source Lifeboat

**Keep software available after its source disappears.**

Source Lifeboat is a standalone owner-operated web app that preserves the current published stable release of a public GitHub repository in a dedicated Forgejo instance. It stores one source archive for the immutable release commit, every uploaded release file, and a versioned checksum manifest. It does not clone or reconstruct upstream Git history, extract source, run downloaded files, fall back to a branch, or schedule recurring work.

**Early development preview.** The source is public for development and review. There is no production release yet. Installation and Forgejo bootstrap are currently manual; do not rely on this as your only backup.

## What v1 does

- Accepts only exact `https://github.com/OWNER/REPO` URLs.
- Resolves GitHub's latest published, non-draft, non-prerelease release and peels annotated tags to a commit SHA.
- Streams the commit source archive and all release assets into bounded staging storage.
- Re-fetches release metadata after transfer and marks changed or missing releases stale.
- Publishes a private archive catalog repository and one Forgejo release per GitHub release identity.
- Streams every Forgejo attachment back and checks its name, byte count and SHA-256 before marking the backup complete. It never trusts digest metadata from the attachment list.
- Retains older completed backups when later checks run.

GitHub source archives may omit Git LFS objects and submodule content. Checksums establish transfer and local integrity, not publisher authenticity.

## Configuration and startup

Copy `.env.example` to an operator-controlled `.env` and replace every placeholder. The important settings are:

- `RB_STATE_DIR=/data`: ordinary app state and staged/completed files.
- `RB_FORGEJO_URL`: app-to-Forgejo URL on the private project network.
- `RB_FORGEJO_TOKEN`: token bootstrapped outside source for the dedicated archive owner.
- `RB_FORGEJO_OWNER`: dedicated user or organization namespace.
- `RB_PUBLIC_URL`: exact browser origin; requests with another Host or Origin are rejected.
- `RB_INSTALLATION_ID`: unique operator-created installation identity.
- `RB_LOGIN_TOKEN`: long random owner login token.
- `RB_MAX_FILE_BYTES` and `RB_MAX_TOTAL_BYTES`: app transfer quotas.
- `RB_FORGEJO_MAX_ATTACHMENT_MIB` and `RB_FORGEJO_MAX_ATTACHMENTS`: corresponding Forgejo release-attachment limits.

The service starts without the Forgejo or login token and shows a setup-needed state. It will not start its backup worker until all required operator configuration is present. Forgejo and owner-access secrets are configured outside source. Optional GitHub credentials can be configured through the authenticated Settings page.

```sh
docker compose config
docker compose up --build
```

Compose pins `codeberg.org/forgejo/forgejo:17.0.1`. Both services use one project-scoped bridge with outbound access for GitHub, isolated named volumes, no fixed container names and loopback-only published ports. Nothing mounts a Docker socket or data from another Forgejo. Forgejo Actions are disabled and no runner exists. The application image runs as UID/GID 10001 and imports directly from `/app/src`; its build does not invoke pip or require a network.

## Forgejo 17 publication behavior

The v17 REST contract creates a release tag from `target_commitish` when a tag does not already exist. Source Lifeboat therefore writes a small catalog commit containing the manifest, explicitly labels that commit as not upstream history, and targets the archive release tag at it. Release attachments are governed by Forgejo's `[attachment] MAX_SIZE` (MiB) and `MAX_FILES`; `MAX_SIZE=2048` is supported by the pinned version and an empty `ALLOWED_TYPES` permits all release file types. Compose exposes the size/count settings and the app refuses a file above its own configured limit before publication. A new release remains a draft through upload and byte-stream verification, so partial work is not presented as complete. Retry repairs that same release identity and only then publishes it.

The disposable integration harness tests the real clients against a separately provisioned Forgejo. Normal startup still requires initializing the dedicated Forgejo and configuring its archive owner and API token.

## Security model

The UI is for one owner. Login uses an HttpOnly, SameSite=Strict session cookie. Every state-changing request requires the exact configured Origin and a CSRF token; every request requires the configured Host. Responses include a restrictive CSP, frame denial, no-sniff and no-referrer headers. The browser can submit only GitHub repository identities—it cannot supply arbitrary network endpoints, commands, chat or agent requests.

Download redirects are followed manually with a fixed limit. Every hop must use HTTPS on an allowed GitHub host and resolve only to global addresses. Connections use validated numeric IPs while retaining TLS hostname checks; proxy environment variables are not used for downloads. GitHub credentials are sent only to the original `api.github.com` request and never forwarded to a redirect host. Files stream to `.partial` paths with per-file, total, time and free-space limits; safe unique names prevent traversal and control-character filenames.

## Tests

Run the dependency-free suite on Python 3.12 or 3.13:

```sh
PYTHONPATH=src python -m unittest discover -s tests -v
```

The fixture tests cover catalog migration and recovery, job states and cancellation, strict GitHub parsing and release handling, redirects and quotas, Forgejo create/repair/idempotency and streamed read-back hashing, HTTP authentication/CSRF/Host/Origin checks, inert DOM rendering, isolation packaging and restore hashing. They use fixed fixture responses and require no credentials or network.

`tools/integration_check.py` writes only inside an explicitly identified disposable installation. It requires all of:

```text
RB_TEST_FORGEJO_URL
RB_TEST_FORGEJO_TOKEN
RB_TEST_FORGEJO_OWNER
RB_TEST_INSTALLATION_ID
RB_TEST_ALLOW_DISPOSABLE=1
RB_TEST_REAL_REPOSITORY=sharkdp/hexyl
```

If the disposable Forgejo advertises a different external root URL than its container-network API URL, also set `RB_TEST_FORGEJO_PUBLIC_URL` to that exact operator-configured public origin; it receives the same strict alias treatment as the application setting.

Before any write it requires a private `rb-installation-sentinel` repository under the configured owner whose description is exactly `repository-backup-disposable:<RB_TEST_INSTALLATION_ID>`. The exact caller URL and this independently created identity must both match; URL matching alone is not treated as ownership proof. It refuses to reuse an existing integration target, runs fixture paths, downloads the current `sharkdp/hexyl` source archive and every release asset within configurable quotas, publishes through the real application clients, restores and hashes every file, rechecks the sentinel, and retains the repository for independent verification. It emits concise JSON and never prints the token.

The non-root integration container intentionally has no Docker socket or CLI. Consequently the trusted operator—not the script—must verify Compose project labels and prove a sentinel second Forgejo's containers, volumes, data and project labels are unchanged before and after the run.

The script always retains its test repository. `RB_TEST_KEEP_RESULT` remains accepted for compatibility but never enables deletion. Cleanup belongs to the trusted operator after both installation-sentinel and Compose-project-label checks; the unprivileged script cannot establish the latter.

## Development status

The initial candidate passes 64 dependency-free unit tests. GUI checks cover desktop/mobile layout, owner access in Settings, token handling, download-folder changes and backup submission. Browser submission tests use fixture responses; they are not proof of a full backup. Real disposable Forgejo backup/restore checks were also run during development, but those earlier results are not a production acceptance guarantee for every later change.

OAuth device-code sign-in, polling limits and expiring-token refresh are implemented. The account approval still requires a user on GitHub. See [OAuth setup](docs/OAUTH_SETUP.md). Personal tokens and anonymous mode remain available. See [Security](SECURITY.md) for deployment boundaries.

Source Lifeboat is independent software and is not affiliated with GitHub or Forgejo. The HWB visual foundation attribution is in `NOTICE`.

Verification cleanup is operator-only: the integration script retains its disposable repository because it cannot inspect trusted Compose project labels. Before cleanup, the operator must verify both the installation sentinel and the exact Compose project labels. Completed Forgejo releases are immutable; a conflicting repeat is refused without modifying existing files. HTTP checks resolve release identity before queueing and coalesce repeated requests for that identity.

## GitHub settings

Sign into the owner GUI and open **Settings → GitHub**. Anonymous mode requires no GitHub account. Choose **Connect with GitHub**, enter the displayed short code at GitHub, approve and return to Settings. The app requests public information only and stores the resulting credentials on your server. Expiring tokens refresh automatically; **Disconnect** removes local credentials. Alternatively, create a fine-grained personal access token with public repository access, an expiry and no extra write permissions. **Save & test connection** validates it before replacing the current token. **Test connection** displays the account and API quota. **Remove token** returns to anonymous access, overriding any environment fallback. Saved tokens are never returned to the browser. New requests use changed settings without a restart; in-flight downloads may still finish with the credential they already loaded.

Credentials are installation-local, stored unencrypted in `RB_STATE_DIR/private/github.json` with a mode-700 directory and mode-600 file and atomic replacement. They are outside the archive catalog and manifests. Protect the server directory and full service-volume backups. Use HTTPS for remote access. Removal from this app does not revoke the token on GitHub.

Unattended deployments may set `RB_GITHUB_TOKEN`; Compose forwards it, and explicit GUI settings take precedence. Never commit tokens or real `.env` files. GitHub passwords are never collected. The default OAuth Client ID is public; installations can supply their own in Settings or `RB_GITHUB_CLIENT_ID`. No client secret or maintainer account credential is required.

## Download folder

In **Settings → Download folder**, enter an absolute path on the application server and choose **Save download folder**. The service creates the folder if necessary and tests writing to its `staging` and `snapshots` subdirectories before saving the setting. The default is the existing `RB_STATE_DIR` location; **Use default** restores it. Existing backups stay at their original recorded paths. A running job retains the folder it started with. New jobs use the current selection. The SQLite catalog and private credentials always remain under `RB_STATE_DIR`, independent of the download folder.

For Docker deployments, the selected path is inside the container: bind-mount the desired host disk/folder into the app service and select that container path. Give the service user (UID 10001) write permission. Keep the mount persistent. This setting does not select the browser computer’s Downloads directory or change Forgejo storage.

The public project name is Source Lifeboat. Existing `RB_*` settings, Python module `repository_backup`, Compose project names, storage volumes remain compatible; renaming the product does not create a different storage installation.

Owner sign-in is located on Settings. The backup page contains only project entry and backup results. If owner access is needed when starting a backup, the app opens Settings and preserves the entered repository URL for the return trip.

Archive catalog names use a length-prefixed GitHub owner to distinguish identities such as `a-b/c` and `a/b-c`. New archives use this convention; prior development snapshots and links are retained without migration.
