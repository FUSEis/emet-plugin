# Security

## Reporting a vulnerability

Please report vulnerabilities privately through GitHub: on this repository, open **Security**, then **Report a vulnerability**. Do not open a public issue.

Include the plugin version (`plugin.json`), the Claude Code version, your operating system, and the steps to reproduce. We acknowledge reports within three business days and keep you updated until a fix is released.

## Scope

In scope: anything in this repository, for example a way to make the plugin send data somewhere other than Emet, send content in `metadata` mode, leak the Emet key, read files outside Claude Code's project folder, or write outside its own data folder.

Out of scope: secrets that the `sanitized` redaction patterns do not recognize. Redaction is best effort and documented as such; use `metadata` mode where that matters. Suggestions for new patterns are welcome as ordinary issues, without the secret.

## Supported versions

Security fixes go into the latest release. Pin a tag in managed settings and move it forward when a release notes a security fix.
