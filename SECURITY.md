# Security policy

## Reporting a vulnerability

Please **do not** open a public issue for security problems. Instead, use GitHub's private
vulnerability reporting: go to the repository's
[Security tab → Report a vulnerability](https://github.com/h11t-labs/firm/security/advisories/new).

You can expect an initial response within a few days. Please include a minimal reproduction if
you can — which module (queue / cache / channel / audit / ui), the database backend, and the
firm package versions involved.

## Scope notes

- firm executes **your own code** (job bodies, cache value coders). The cache's pickle coder is
  opt-in and documented as safe only when the database is fully trusted — reports that reduce
  to "unpickling untrusted data is dangerous" are working as documented.
- The `firm-ui` dashboard refuses to bind to non-loopback addresses without authentication
  configured. Bypasses of that guard, or of its authenticators, are definitely in scope.

## Supported versions

The latest release of each package receives security fixes.
