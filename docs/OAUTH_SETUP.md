# GitHub OAuth setup

Status: device authorization and expiring-token refresh are implemented. The project OAuth app is registered with public Client ID `Ov23lid8jIzZrxu2Ye6d`. Anonymous requests and personal access tokens remain available.

## Register the project OAuth application

Using the GitHub account or organization that owns your registration, open https://github.com/settings/applications/new and enter:

- Application name: **Source Lifeboat**
- Homepage URL: **https://github.com/explorefailure/source-lifeboat**
- Description: **Self-hosted preservation of GitHub release source archives and ready-made downloads.**
- Authorization callback URL: **https://github.com/explorefailure/source-lifeboat** (a public placeholder; device flow does not use a callback)
- Enable **Device Flow**.

GitHub may show token-expiration options. Keep a record of the selection: the implementation must explicitly support the selected token lifetime and refresh behavior before enabling sign-in.

After registration, copy the **Client ID**. This is a public identifier, not an account credential. Do not distribute a client secret, a GitHub personal token or a user access token. The device-code exchange does not require a client secret.

The user flow is Settings → Connect with GitHub → approve the displayed short code at https://github.com/login/device → return to Settings. The server honors polling intervals, slow-down responses, expiry, cancellation and denial. Access is restricted to public information (no OAuth scope requested). User authorization must never replace owner access to this installation.

## Independent installations

Each installation can configure its own public client ID in Settings → Use your own OAuth application, or set `RB_GITHUB_CLIENT_ID`. The registered project Client ID is the default. Using that default depends on the continued availability of its GitHub OAuth registration, not on access to the maintainer's personal credentials. Anonymous mode and personal-token configuration remain available.

Official documentation: https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/creating-an-oauth-app and https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/authorizing-oauth-apps#device-flow.

## Operational behavior

Pending codes remain in server memory, belong to the owner session that requested them, and expire after GitHub's deadline. Reloading Settings and clicking Connect resumes that session's pending code; a server restart requires a new code. Cancelling or changing credentials invalidates the local pending flow. GitHub approval alone cannot create an owner session.

Access and refresh tokens are saved atomically under the state directory's `private/github.json` with mode 0600. They are never returned to the GUI. Expiring device tokens refresh automatically before API use, using the original Client ID and rotating both tokens. Invalid or expired refresh credentials require reconnecting. Each backup uses one credential snapshot; an exceptionally long download can require retrying after token expiry.

Disconnect removes the locally saved credential and cancels pending sign-in. It does not revoke the app remotely; revoke authorization at https://github.com/settings/applications when needed. The existing environment-token fallback remains suppressed after disconnect until a credential is explicitly saved again.
