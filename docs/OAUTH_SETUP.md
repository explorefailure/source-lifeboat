# GitHub OAuth setup

Status: registration and implementation in progress. The current application uses anonymous requests or a personal access token. It does not yet implement device authorization or token refresh.

## Register the project OAuth application

Using the GitHub account or organization that owns your registration, open https://github.com/settings/applications/new and enter:

- Application name: **Source Lifeboat**
- Homepage URL: **https://github.com/explorefailure/source-lifeboat**
- Description: **Self-hosted preservation of GitHub release source archives and ready-made downloads.**
- Authorization callback URL: **https://github.com/explorefailure/source-lifeboat** (a public placeholder; device flow does not use a callback)
- Enable **Device Flow**.

GitHub may show token-expiration options. Keep a record of the selection: the implementation must explicitly support the selected token lifetime and refresh behavior before enabling sign-in.

After registration, copy the **Client ID**. This is a public identifier, not an account credential. Do not distribute a client secret, a GitHub personal token or a user access token. The device-code exchange does not require a client secret.

The planned user flow is Settings → Connect with GitHub → approve the displayed short code at https://github.com/login/device → return to Settings. The server must honor polling intervals, slow-down responses, expiry, cancellation and denial. Access will be restricted to public information (no OAuth scope requested). User authorization must never replace owner access to this installation.

## Independent installations

Each installation will be able to configure its own public client ID. A project-provided default can be added after registration. Using that default depends on the continued availability of its GitHub OAuth registration, not on access to the maintainer's personal credentials. Anonymous mode and personal-token configuration remain available.

Official documentation: https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/creating-an-oauth-app and https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/authorizing-oauth-apps#device-flow.
