# Security and development status

Source Lifeboat is an early development preview with no production release. Keep it on loopback or behind authenticated access. Use HTTPS when accessing a remote installation. Do not expose the service directly to the public internet.

Owner login is required for operational APIs and settings. First-run owner creation is limited to direct loopback access or an operator-provided setup key; existing legacy-token installations require their existing key. Owner passwords use salted scrypt hashes, and failed login attempts are limited to five per minute per process. GitHub authentication only raises API access limits; it does not grant access to the application itself. Settings credentials are stored unencrypted in a mode-600 file inside a mode-700 directory. Protect the server account, disk and service-volume backups.

Downloads are never executed or extracted by the backup service. Checksums establish transfer integrity, not that upstream software is safe or authentic. GitHub source archives may omit Git LFS content and submodules. Archive only content you are entitled to preserve and respect its licenses.

Use a dedicated Forgejo instance and disposable test targets. The integration script requires an explicit installation sentinel and retains its test repository; trusted operators must verify installation identity and Compose project labels before cleanup.

If GitHub private vulnerability reporting is enabled for this repository, use its Security tab to report vulnerabilities privately. Do not put credentials or exploit details into public issues.
